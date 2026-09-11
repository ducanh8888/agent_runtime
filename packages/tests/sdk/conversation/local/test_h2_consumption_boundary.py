"""H2: a run records which input it consumed, and the answer stays scoped to it.

Deterministic and LLM-free: a scripted agent emits one message per step and
marks the run finished, so the run loop terminates after a known number of them.
"""

from __future__ import annotations

import pytest
from pydantic import PrivateAttr, SecretStr

from agentrt.sdk.agent.base import AgentBase
from agentrt.sdk.conversation import Conversation, LocalConversation
from agentrt.sdk.conversation.response_utils import get_agent_final_response
from agentrt.sdk.conversation.state import ConversationExecutionStatus
from agentrt.sdk.conversation.types import (
    ConversationCallbackType,
    ConversationTokenCallbackType,
)
from agentrt.sdk.event.llm_convertible import MessageEvent, SystemPromptEvent
from agentrt.sdk.llm import LLM, Message, TextContent


class ScriptedAgent(AgentBase):
    """Emits a scripted reply per step, then marks the run finished."""

    # AgentBase is frozen, so per-instance bookkeeping lives in private attrs.
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

    def init_state(
        self, state, on_event: ConversationCallbackType
    ) -> None:  # noqa: ARG002
        on_event(
            SystemPromptEvent(
                source="agent", system_prompt=TextContent(text="dummy"), tools=[]
            )
        )

    def step(
        self,
        conversation: LocalConversation,
        on_event: ConversationCallbackType,
        on_token: ConversationTokenCallbackType | None = None,
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


def test_run_records_the_consumed_boundary() -> None:
    conversation = Conversation(agent=ScriptedAgent(["answer one"]))
    conversation.send_message("question one")

    conversation.run()

    state = conversation.state
    assert state.consumed_user_message_id == state.last_user_message_id
    assert state.iterations_used == 1


@pytest.mark.asyncio
async def test_arun_records_the_consumed_boundary() -> None:
    """The server runs conversations through arun, so it must set it too."""
    conversation = Conversation(agent=ScriptedAgent(["answer one"]))
    conversation.send_message("question one")

    await conversation.arun()

    state = conversation.state
    assert state.consumed_user_message_id == state.last_user_message_id
    assert state.iterations_used == 1


def test_boundary_points_at_the_input_not_the_answer() -> None:
    """The consumed boundary is the user input, so its own answer is included."""
    conversation = Conversation(agent=ScriptedAgent(["answer one"]))
    conversation.send_message("question one")
    conversation.run()

    state = conversation.state
    boundary_message = next(
        event for event in state.events if event.id == state.consumed_user_message_id
    )
    assert boundary_message.source == "user"

    assert (
        get_agent_final_response(state.events, after_id=state.consumed_user_message_id)
        == "answer one"
    )


def test_a_new_input_is_not_answered_by_the_previous_run() -> None:
    """The defect: after a second send, the first answer must not be returned."""
    conversation = Conversation(agent=ScriptedAgent(["answer one"]))
    conversation.send_message("question one")
    conversation.run()

    first_answer_boundary = conversation.state.consumed_user_message_id

    conversation.send_message("question two")
    state = conversation.state

    # The new input is accepted but not consumed, so it is pending: the previous
    # answer (which sits before this input) is not its result.
    assert state.last_user_message_id is not None
    assert state.consumed_user_message_id == first_answer_boundary
    assert state.consumed_user_message_id != state.last_user_message_id
    assert (
        get_agent_final_response(conversation.state.events, after_id=state.last_user_message_id)
        == ""
    )


def test_second_run_advances_the_boundary_and_resets_the_counter() -> None:
    conversation = Conversation(agent=ScriptedAgent(["answer one", "answer two"]))
    conversation.send_message("question one")
    conversation.run()
    first = conversation.state.consumed_user_message_id

    conversation.send_message("question two")
    conversation.run()
    state = conversation.state

    assert state.consumed_user_message_id != first
    assert state.consumed_user_message_id == state.last_user_message_id
    assert state.iterations_used == 1
    assert (
        get_agent_final_response(conversation.state.events, after_id=first)
        == "answer two"
    )
