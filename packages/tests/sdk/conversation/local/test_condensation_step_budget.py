"""Condensation retries do not consume the action-step budget."""

import pytest
from pydantic import PrivateAttr, SecretStr

from agentrt.sdk.agent.base import AgentBase
from agentrt.sdk.conversation import Conversation, LocalConversation
from agentrt.sdk.conversation.state import ConversationExecutionStatus
from agentrt.sdk.event import CondensationRequest, MessageEvent
from agentrt.sdk.llm import LLM, Message, TextContent


class ScriptedCondensationAgent(AgentBase):
    _steps: list[str] = PrivateAttr(default_factory=list)
    _index: int = PrivateAttr(default=0)

    def __init__(self, steps: list[str]):
        super().__init__(
            llm=LLM(
                model="gpt-4o-mini", api_key=SecretStr("test-key"), usage_id="test-llm"
            ),
            tools=[],
        )
        self._steps = steps

    def step(self, conversation: LocalConversation, on_event, on_token=None) -> None:
        step = self._steps[self._index]
        self._index += 1
        if step == "condense":
            on_event(CondensationRequest())
        else:
            on_event(
                MessageEvent(
                    source="agent",
                    llm_message=Message(
                        role="assistant", content=[TextContent(text="done")]
                    ),
                )
            )
            if step == "finish":
                conversation.state.execution_status = (
                    ConversationExecutionStatus.FINISHED
                )


@pytest.mark.parametrize("async_run", [False, True])
@pytest.mark.asyncio
async def test_condensation_does_not_use_iteration_budget(async_run: bool) -> None:
    conv = Conversation(
        agent=ScriptedCondensationAgent(["condense", "finish"]), max_iteration_per_run=1
    )
    conv.send_message("question")
    if async_run:
        await conv.arun()
    else:
        conv.run()
    assert conv.state.execution_status == ConversationExecutionStatus.FINISHED
    assert conv.state.iterations_used == 1
    assert conv.state.condensation_steps == 1


@pytest.mark.parametrize("async_run", [False, True])
@pytest.mark.asyncio
async def test_three_consecutive_condensations_go_stuck(async_run: bool) -> None:
    conv = Conversation(
        agent=ScriptedCondensationAgent(["condense"] * 3), max_iteration_per_run=1
    )
    conv.send_message("question")
    if async_run:
        await conv.arun()
    else:
        conv.run()
    assert conv.state.execution_status == ConversationExecutionStatus.STUCK
    assert conv.state.iterations_used == 0
    assert conv.state.condensation_steps == 3


@pytest.mark.parametrize("async_run", [False, True])
@pytest.mark.asyncio
async def test_normal_step_resets_condensation_streak(async_run: bool) -> None:
    conv = Conversation(
        agent=ScriptedCondensationAgent(
            ["condense", "condense", "continue", "condense", "finish"]
        ),
        max_iteration_per_run=2,
    )
    conv.send_message("question")
    if async_run:
        await conv.arun()
    else:
        conv.run()
    assert conv.state.execution_status == ConversationExecutionStatus.FINISHED
    assert conv.state.iterations_used == 2
    assert conv.state.condensation_steps == 3
