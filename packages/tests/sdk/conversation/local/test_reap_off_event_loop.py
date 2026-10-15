import asyncio
import tempfile
import time
from unittest.mock import Mock

import pytest
from pydantic import PrivateAttr, SecretStr

from agentrt.sdk import Conversation
from agentrt.sdk.agent.base import AgentBase
from agentrt.sdk.conversation.state import ConversationExecutionStatus
from agentrt.sdk.conversation.types import ConversationCallbackType
from agentrt.sdk.event.llm_convertible import MessageEvent, SystemPromptEvent
from agentrt.sdk.llm import LLM, Message, TextContent


def _scripted_agent():
    class _ScriptedAgent(AgentBase):
        _replies: list[str] = PrivateAttr(default_factory=list)
        _calls: int = PrivateAttr(default=0)

        def __init__(self, replies: list[str]) -> None:
            super().__init__(
                llm=LLM(
                    model="gpt-4o-mini",
                    api_key=SecretStr("test-key"),
                    usage_id="test-llm",
                ),
                tools=[],
            )
            self._replies = replies

        def init_state(self, state, on_event: ConversationCallbackType) -> None:  # noqa: ARG002
            on_event(
                SystemPromptEvent(
                    source="agent", system_prompt=TextContent(text="dummy"), tools=[]
                )
            )

        def step(
            self,
            conversation,
            on_event: ConversationCallbackType,
            on_token=None,
        ) -> None:
            reply = self._replies[min(self._calls, len(self._replies) - 1)]
            self._calls += 1
            on_event(
                MessageEvent(
                    source="agent",
                    llm_message=Message(
                        role="assistant", content=[TextContent(text=reply)]
                    ),
                )
            )
            conversation.state.execution_status = ConversationExecutionStatus.FINISHED

    return _ScriptedAgent(["done"])


@pytest.mark.asyncio
async def test_blocking_reap_runs_off_event_loop(monkeypatch):
    monkeypatch.setenv("AGENTRT_REAP_TIMEOUT_SECONDS", "0.5")

    with tempfile.TemporaryDirectory() as temp_dir:
        conversation = Conversation(
            agent=_scripted_agent(),
            workspace=temp_dir,
        )

        def blocking_reap():
            time.sleep(3)

        monkeypatch.setattr(conversation, "_reap_run_scoped_tools", blocking_reap)

        counter = 0
        stop_ticker = asyncio.Event()

        async def ticker():
            nonlocal counter
            while not stop_ticker.is_set():
                counter += 1
                await asyncio.sleep(0.05)

        ticker_task = asyncio.create_task(ticker())

        t0 = time.monotonic()
        try:
            await conversation._reap_run_scoped_tools_off_loop()
        finally:
            stop_ticker.set()
            await ticker_task

        elapsed = time.monotonic() - t0

        assert elapsed < 1.5, f"expected < 1.5s but took {elapsed:.2f}s"
        assert counter >= 5, f"counter was only {counter}, event loop was blocked"


@pytest.mark.asyncio
async def test_fast_reap_called_exactly_once(monkeypatch):
    monkeypatch.setenv("AGENTRT_REAP_TIMEOUT_SECONDS", "30")

    with tempfile.TemporaryDirectory() as temp_dir:
        conversation = Conversation(
            agent=_scripted_agent(),
            workspace=temp_dir,
        )

        reap_mock = Mock()
        monkeypatch.setattr(conversation, "_reap_run_scoped_tools", reap_mock)

        await conversation._reap_run_scoped_tools_off_loop()

        reap_mock.assert_called_once()
