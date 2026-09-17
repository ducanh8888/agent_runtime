"""H8 item 12: a run that never starts is stopped and named.

Every other terminal outcome is a bare `error`. This one gets a code that says
what happened, so an orchestrator can tell "the provider never answered" from
"the agent failed" without reading the transcript -- and, crucially, without a
new `ConversationExecutionStatus` value, which is persisted and read by clients
that would meet an unknown one.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from pydantic import SecretStr

from agentrt.agent_server import config as server_config
from agentrt.agent_server.event_service import (
    START_DEADLINE_ERROR_CODE,
    EventService,
)
from agentrt.sdk import Agent
from agentrt.sdk.conversation.state import (
    ConversationExecutionStatus,
    ConversationState,
)
from agentrt.sdk.event import ActionEvent, MessageEvent
from agentrt.sdk.event.conversation_error import ConversationErrorEvent
from agentrt.sdk.event.error_classification import FailureKind
from agentrt.sdk.llm import LLM, Message, MessageToolCall, TextContent
from agentrt.sdk.tool.schema import Action
from agentrt.sdk.workspace import LocalWorkspace


class _DeadlineProbeAction(Action):
    """Unique name: pydantic registers Action subclasses by name process-wide."""

    command: str


def _state(tmp_path, *, status=ConversationExecutionStatus.RUNNING):
    state = ConversationState.create(
        id=uuid4(),
        agent=Agent(
            llm=LLM(
                model="gpt-4o-mini",
                api_key=SecretStr("test-key"),
                usage_id="test-llm",
            ),
            tools=[],
        ),
        workspace=LocalWorkspace(working_dir=str(tmp_path / "ws")),
        max_iterations=500,
    )
    state.execution_status = status
    return state


def _service(state, tmp_path) -> EventService:
    conversation = SimpleNamespace(
        _state=state,
        _on_event=lambda event: state.append_event(event),
        pause=lambda: None,
    )
    service = EventService(
        stored=SimpleNamespace(id=state.id), conversations_dir=tmp_path
    )
    service._conversation = conversation
    # The real interrupt waits on the run task and publishes state; the
    # watchdog's contract is that it asks for a stop, not how that is done.
    service.interrupt = AsyncMock()
    return service


def _action() -> ActionEvent:
    return ActionEvent(
        source="agent",
        thought=[TextContent(text="working")],
        action=_DeadlineProbeAction(command="test"),
        tool_call=MessageToolCall(
            id="tc-1", name="terminal", arguments="{}", origin="completion"
        ),
        tool_name="terminal",
        tool_call_id="tc-1",
        llm_response_id="resp-1",
    )


# --- what counts as "started" -------------------------------------------


def test_a_run_that_produced_nothing_has_not_started(tmp_path) -> None:
    state = _state(tmp_path)
    service = _service(state, tmp_path)

    assert service._run_has_started(0, 0) is False


def test_an_action_counts_as_started(tmp_path) -> None:
    state = _state(tmp_path)
    service = _service(state, tmp_path)
    state.append_event(_action())

    assert service._run_has_started(0, 0) is True


def test_history_from_an_earlier_run_does_not_count(tmp_path) -> None:
    """The baselines are what make this about *this* run.

    A resumed conversation already carries actions from before; judging them
    would mean the deadline never fires for exactly the sessions most likely to
    need it.
    """
    state = _state(tmp_path)
    service = _service(state, tmp_path)
    state.append_event(_action())
    baseline = len(state.events)

    assert service._run_has_started(baseline, 0) is False


def test_a_completed_step_counts_even_without_an_action(tmp_path) -> None:
    """A step can finish an LLM call before there is an action to show."""
    state = _state(tmp_path)
    service = _service(state, tmp_path)
    state.iterations_used = 1

    assert service._run_has_started(0, 0) is True


def test_a_plain_message_alone_does_not_count(tmp_path) -> None:
    """A message is not an action or an observation; the item names those two."""
    state = _state(tmp_path)
    service = _service(state, tmp_path)
    state.append_event(
        MessageEvent(
            source="agent",
            llm_message=Message(role="assistant", content=[TextContent(text="hi")]),
        )
    )

    assert service._run_has_started(0, 0) is False


# --- what happens when it fires -----------------------------------------


@pytest.mark.asyncio
async def test_the_watchdog_stops_the_run_and_names_why(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(server_config.START_DEADLINE_ENV, "0.05")
    state = _state(tmp_path)
    service = _service(state, tmp_path)

    await service._watch_start_deadline(0, 0)

    service.interrupt.assert_awaited_once()
    errors = [e for e in state.events if isinstance(e, ConversationErrorEvent)]
    assert len(errors) == 1
    assert errors[0].code == START_DEADLINE_ERROR_CODE
    assert errors[0].classification is not None
    assert errors[0].classification.kind is FailureKind.TRANSIENT
    assert state.execution_status == ConversationExecutionStatus.ERROR


@pytest.mark.asyncio
async def test_the_watchdog_leaves_a_started_run_alone(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(server_config.START_DEADLINE_ENV, "0.05")
    state = _state(tmp_path)
    service = _service(state, tmp_path)
    state.append_event(_action())
    baseline = len(state.events) - 1  # the action belongs to this run

    await service._watch_start_deadline(baseline, 0)

    service.interrupt.assert_not_awaited()
    assert not [e for e in state.events if isinstance(e, ConversationErrorEvent)]


@pytest.mark.asyncio
async def test_zero_disables_the_watchdog(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(server_config.START_DEADLINE_ENV, "0")
    state = _state(tmp_path, status=ConversationExecutionStatus.RUNNING)
    service = _service(state, tmp_path)

    await service._watch_start_deadline(0, 0)

    service.interrupt.assert_not_awaited()
    assert state.execution_status == ConversationExecutionStatus.RUNNING


# --- the derived deadline ------------------------------------------------


def test_the_deadline_derives_from_the_profile_retry_budget(tmp_path) -> None:
    state_dir = tmp_path / "state"
    (state_dir / "profiles").mkdir(parents=True)
    (state_dir / "profiles" / "default.json").write_text(
        '{"timeout": 300, "num_retries": 5}'
    )
    conversations = state_dir / "conversations"

    assert server_config.start_deadline_seconds(conversations) == 300 * 6 * 1.5


def test_an_unreadable_profile_returns_the_constant_unscaled(tmp_path) -> None:
    """The constant is the answer for "unknown", not a base to scale.

    It was scaled once, and 1800 * 1.5 is exactly what a 300s/5-retry profile
    derives to, so the two were indistinguishable until the arithmetic was
    checked.
    """
    conversations = tmp_path / "state" / "conversations"

    assert (
        server_config.start_deadline_seconds(conversations)
        == server_config.FALLBACK_START_DEADLINE_SECONDS
    )
    assert (
        server_config.start_deadline_seconds(None)
        == server_config.FALLBACK_START_DEADLINE_SECONDS
    )


def test_the_environment_overrides_the_derivation(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv(server_config.START_DEADLINE_ENV, "42")
    state_dir = tmp_path / "state"
    (state_dir / "profiles").mkdir(parents=True)
    (state_dir / "profiles" / "default.json").write_text(
        '{"timeout": 300, "num_retries": 5}'
    )

    assert server_config.start_deadline_seconds(state_dir / "conversations") == 42.0


# --- the integration the unit tests could not see ------------------------------


@pytest.mark.asyncio
async def test_a_run_that_never_starts_is_recorded_through_run_itself(
    tmp_path, monkeypatch
) -> None:
    """The whole path, not the watchdog called directly.

    Written after the end-to-end probe found what these unit tests could not:
    the run's own `finally` cancelled the watchdog while it was inside
    `interrupt()` -- which is waiting on the run task -- so the watchdog died
    before recording anything and the session ended PAUSED with no reason at
    all. Every test above still passed, because none of them went through
    `run()`.
    """
    monkeypatch.setenv(server_config.START_DEADLINE_ENV, "0.2")
    # IDLE, as a dispatch leaves it: `run()` refuses to start on RUNNING.
    state = _state(tmp_path, status=ConversationExecutionStatus.IDLE)
    holder: dict = {}

    class _Agent:
        """`run()` inspects type(...).astep, so this has to be a real class."""

        async def astep(self, *args, **kwargs) -> None:  # pragma: no cover
            raise AssertionError("the run should never reach a step")

    class _Conversation:
        """Mirrors LocalConversation in the two ways this path inspects it."""

        _state = state

        def __init__(self) -> None:
            self.agent = _Agent()

        async def arun(self) -> None:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                # LocalConversation settles PAUSED, records the interrupt, and
                # deliberately does *not* re-raise -- the documented contract
                # whose whole point is that the task ends cleanly. A stub that
                # re-raised instead made this test fail for the wrong reason:
                # `interrupt()` awaits this task, so the CancelledError
                # travelled back up and cancelled the watchdog mid-sequence,
                # and the recording never happened. Faithful here or the test
                # measures the stub.
                state.execution_status = ConversationExecutionStatus.PAUSED

        def interrupt(self) -> None:
            task = holder.get("task")
            if task is not None and not task.done():
                task.cancel()

        def pause(self) -> None:
            state.execution_status = ConversationExecutionStatus.PAUSED

        def _on_event(self, event) -> None:
            state.append_event(event)

    service = EventService(
        stored=SimpleNamespace(id=state.id), conversations_dir=tmp_path
    )
    service._conversation = _Conversation()

    await service.run()
    holder["task"] = service._run_task
    assert holder["task"] is not None

    # Wait for the *recording*, not just for the run to end: it happens on an
    # executor thread after the run task has settled, so a check that stopped at
    # "the run is done" would read PAUSED and miss it. With the bug this test
    # was written for, this poll is what fails -- nothing ever appears.
    errors: list[ConversationErrorEvent] = []
    for _ in range(100):
        errors = [e for e in state.events if isinstance(e, ConversationErrorEvent)]
        if errors:
            break
        await asyncio.sleep(0.05)

    assert len(errors) == 1, [type(e).__name__ for e in state.events]
    assert errors[0].code == START_DEADLINE_ERROR_CODE
    assert state.execution_status == ConversationExecutionStatus.ERROR
