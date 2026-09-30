"""SDK run-loop fixes from the 2026-09-21/25 orchestrator feedback."""

from __future__ import annotations

from collections.abc import Sequence
from typing import ClassVar

from agentrt.sdk.agent import Agent
from agentrt.sdk.conversation import Conversation
from agentrt.sdk.conversation.state import ConversationExecutionStatus
from agentrt.sdk.event import MessageEvent
from agentrt.sdk.llm import ImageContent, Message, MessageToolCall, TextContent
from agentrt.sdk.testing import TestLLM
from agentrt.sdk.tool import (
    Action,
    Observation,
    Tool,
    ToolDefinition,
    ToolExecutor,
    register_tool,
)


TOOL_NAME = "feedback_0925_probe"


class _ProbeAction(Action):
    command: str


class _ProbeObservation(Observation):
    result: str

    @property
    def to_llm_content(self) -> Sequence[TextContent | ImageContent]:
        return [TextContent(text=self.result)]


class _ProbeExecutor(ToolExecutor[_ProbeAction, _ProbeObservation]):
    def __call__(self, action: _ProbeAction, conversation=None) -> _ProbeObservation:
        return _ProbeObservation(result=f"ran {action.command}")


class _ProbeTool(ToolDefinition[_ProbeAction, _ProbeObservation]):
    name: ClassVar[str] = TOOL_NAME

    @classmethod
    def create(cls, conv_state=None, **params) -> Sequence[_ProbeTool]:
        return [
            cls(
                description="probe",
                action_type=_ProbeAction,
                observation_type=_ProbeObservation,
                executor=_ProbeExecutor(),
            )
        ]


def _tool_call(call_id: str, command: str = "look") -> Message:
    return Message(
        role="assistant",
        content=[TextContent(text="")],
        tool_calls=[
            MessageToolCall(
                id=call_id,
                name=TOOL_NAME,
                arguments=f'{{"command": "{command}"}}',
                origin="completion",
            )
        ],
    )


def _register_tool() -> None:
    register_tool(TOOL_NAME, _ProbeTool)


def _budget_notices(conversation) -> list[MessageEvent]:
    return [
        e
        for e in conversation.state.events
        if isinstance(e, MessageEvent)
        and e.source == "environment"
        and "[Step budget]"
        in "".join(c.text for c in e.llm_message.content if isinstance(c, TextContent))
    ]


def test_agent_is_told_once_to_wrap_up_before_the_iteration_cap() -> None:
    """25/09 #4: a read-only session hit MaxIterationsReached with its
    findings unwritten. It now gets one notice three steps before the cap."""
    _register_tool()
    llm = TestLLM.from_messages([_tool_call(f"c{i}", f"cmd{i}") for i in range(8)])
    conversation = Conversation(
        agent=Agent(llm=llm, tools=[Tool(name=TOOL_NAME)]),
        max_iteration_per_run=6,
    )
    conversation.send_message(
        Message(role="user", content=[TextContent(text="investigate")])
    )
    conversation.run()

    notices = _budget_notices(conversation)
    assert len(notices) == 1
    text = notices[0].llm_message.content[0]
    assert isinstance(text, TextContent)
    assert "3 step(s)" in text.text
    # The cap itself is unchanged.
    assert conversation.state.execution_status == ConversationExecutionStatus.ERROR


async def test_budget_notice_also_on_the_async_run_path() -> None:
    _register_tool()
    llm = TestLLM.from_messages([_tool_call(f"c{i}", f"cmd{i}") for i in range(8)])
    conversation = Conversation(
        agent=Agent(llm=llm, tools=[Tool(name=TOOL_NAME)]),
        max_iteration_per_run=5,
    )
    conversation.send_message(
        Message(role="user", content=[TextContent(text="investigate")])
    )
    await conversation.arun()
    assert len(_budget_notices(conversation)) == 1


def test_resuming_a_stuck_session_gives_the_agent_another_step() -> None:
    """21/09: interrupt+resume did not unstick a `stuck` session. The resumed
    run re-checked stuck patterns before its first step, against the same
    trailing history, and went straight back to STUCK without calling the
    model. A resume now starts the detector's window afresh."""
    _register_tool()
    repeat = [_tool_call(f"c{i}", "same") for i in range(4)]
    llm = TestLLM.from_messages(
        [*repeat, Message(role="assistant", content=[TextContent(text="ok now")])]
    )
    conversation = Conversation(
        agent=Agent(llm=llm, tools=[Tool(name=TOOL_NAME)]),
        max_iteration_per_run=50,
    )
    conversation.send_message(Message(role="user", content=[TextContent(text="go")]))
    conversation.run()
    assert conversation.state.execution_status == ConversationExecutionStatus.STUCK
    calls_before = llm.call_count

    conversation.run()

    assert llm.call_count == calls_before + 1
    assert conversation.state.execution_status == ConversationExecutionStatus.FINISHED


def test_no_budget_notice_when_the_agent_finishes_early() -> None:
    _register_tool()
    llm = TestLLM.from_messages(
        [
            _tool_call("c0"),
            Message(role="assistant", content=[TextContent(text="done")]),
        ]
    )
    conversation = Conversation(
        agent=Agent(llm=llm, tools=[Tool(name=TOOL_NAME)]),
        max_iteration_per_run=20,
    )
    conversation.send_message(Message(role="user", content=[TextContent(text="go")]))
    conversation.run()
    assert _budget_notices(conversation) == []
