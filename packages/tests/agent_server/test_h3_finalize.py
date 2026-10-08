"""H3: finalization is a barrier with an optional tools-disabled summary."""

from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic import SecretStr

from agentrt.agent_server import event_service as event_service_mod
from agentrt.agent_server.config import finalize_summary_enabled
from agentrt.agent_server.event_service import EventService
from agentrt.sdk import Agent
from agentrt.sdk.conversation.state import (
    ConversationExecutionStatus,
    ConversationState,
)
from agentrt.sdk.event import MessageEvent
from agentrt.sdk.llm import LLM, Message, TextContent
from agentrt.sdk.workspace import LocalWorkspace


def _state(
    tmp_path,
    *,
    max_iterations: int = 500,
    execution_status: ConversationExecutionStatus = (
        ConversationExecutionStatus.RUNNING
    ),
) -> ConversationState:
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
        max_iterations=max_iterations,
    )
    state.execution_status = execution_status
    return state


def _user(state: ConversationState, text: str) -> MessageEvent:
    event = MessageEvent(
        source="user",
        llm_message=Message(role="user", content=[TextContent(text=text)]),
    )
    state.append_event(event)
    state.last_user_message_id = event.id
    return event


def _agent(state: ConversationState, text: str) -> MessageEvent:
    event = MessageEvent(
        source="agent",
        llm_message=Message(role="assistant", content=[TextContent(text=text)]),
    )
    state.append_event(event)
    return event


def _service(state: ConversationState, llm=None) -> EventService:
    agent = SimpleNamespace(llm=llm) if llm is not None else None

    def _pause() -> None:
        # The real pause reaches a safe boundary and leaves the session PAUSED.
        state.execution_status = ConversationExecutionStatus.PAUSED

    conversation = SimpleNamespace(
        _state=state, pause=MagicMock(side_effect=_pause), agent=agent
    )
    service = EventService(stored=MagicMock(), conversations_dir=Path("test_dir"))
    service._conversation = conversation
    return service


class _FakeLLM:
    """Records the messages it was given; tools are never passed."""

    stream = False

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.calls: list = []

    def completion(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(
            message=SimpleNamespace(content=[TextContent(text=self.reply)])
        )


@pytest.mark.asyncio
async def test_finalize_pauses_and_records_the_request(tmp_path) -> None:
    state = _state(tmp_path)
    user = _user(state, "do the thing")
    _agent(state, "partial work")
    state.consumed_user_message_id = user.id
    state.iterations_used = 2
    service = _service(state)

    result = await service.finalize()

    conversation = service._conversation
    assert conversation.pause.called
    assert state.finalized_request_id == user.id
    assert state.finalized_at is not None
    assert result.state.value == "partial"
    assert result.response == "partial work"
    assert result.summary is None


@pytest.mark.asyncio
async def test_finalize_without_allowance_claims_no_summary(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(event_service_mod, "finalize_summary_enabled", lambda: True)
    state = _state(tmp_path, max_iterations=3)
    user = _user(state, "q")
    _agent(state, "work")
    state.consumed_user_message_id = user.id
    state.iterations_used = 3  # no allowance left

    llm = _FakeLLM("should not be called")
    service = _service(state, llm=llm)

    result = await service.finalize(summary=True)

    assert llm.calls == []
    assert result.summary is None
    assert state.finalized_at is not None


@pytest.mark.asyncio
async def test_finalize_summary_runs_when_enabled(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(event_service_mod, "finalize_summary_enabled", lambda: True)
    state = _state(tmp_path, max_iterations=5)
    user = _user(state, "q")
    _agent(state, "work so far")
    state.consumed_user_message_id = user.id
    state.iterations_used = 1

    llm = _FakeLLM("did X; Y remains")
    service = _service(state, llm=llm)

    result = await service.finalize(summary=True)

    assert len(llm.calls) == 1
    assert result.summary == "did X; Y remains"
    assert state.iterations_used == 2  # charged to the run's allowance


@pytest.mark.asyncio
async def test_repeated_finalize_does_not_run_again(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(event_service_mod, "finalize_summary_enabled", lambda: True)
    state = _state(tmp_path)
    user = _user(state, "q")
    _agent(state, "work")
    state.consumed_user_message_id = user.id
    state.iterations_used = 1

    llm = _FakeLLM("once")
    service = _service(state, llm=llm)

    first = await service.finalize(summary=True)
    second = await service.finalize(summary=True)

    assert len(llm.calls) == 1
    assert first.summary == second.summary == "once"


@pytest.mark.asyncio
async def test_summary_is_scoped_to_the_input_it_finalized(
    tmp_path, monkeypatch
) -> None:
    """A later request must not inherit the previous request's summary."""
    monkeypatch.setattr(event_service_mod, "finalize_summary_enabled", lambda: True)
    state = _state(tmp_path)
    first = _user(state, "q1")
    _agent(state, "work one")
    state.consumed_user_message_id = first.id
    state.iterations_used = 1

    llm = _FakeLLM("summary one")
    service = _service(state, llm=llm)
    finalized = await service.finalize(summary=True)
    assert finalized.summary == "summary one"

    # A second request runs to completion without finalizing.
    second = _user(state, "q2")
    _agent(state, "work two")
    state.consumed_user_message_id = second.id
    state.execution_status = ConversationExecutionStatus.FINISHED

    later = service._get_agent_response_result_sync()
    assert later.state.value == "final"
    assert later.summary is None


def test_summary_flag_is_off_by_default(monkeypatch) -> None:
    monkeypatch.delenv("AGENTRT_FINALIZE_SUMMARY", raising=False)
    assert finalize_summary_enabled() is False
    for value in ("1", "true", "YES", "on"):
        monkeypatch.setenv("AGENTRT_FINALIZE_SUMMARY", value)
        assert finalize_summary_enabled() is True
    monkeypatch.setenv("AGENTRT_FINALIZE_SUMMARY", "no")
    assert finalize_summary_enabled() is False


class _StepHoldingLock:
    """Hold the conversation lock from another thread, as a long step does."""

    def __init__(self, state: ConversationState) -> None:
        self._state = state
        self._held = threading.Event()
        self._release = threading.Event()
        self._thread = threading.Thread(target=self._hold, daemon=True)

    def _hold(self) -> None:
        with self._state:
            self._held.set()
            self._release.wait(10)

    def __enter__(self) -> _StepHoldingLock:
        self._thread.start()
        assert self._held.wait(5)
        return self

    def __exit__(self, *exc: object) -> None:
        self._release.set()
        self._thread.join(5)


@pytest.mark.asyncio
async def test_run_on_a_running_session_answers_at_once(tmp_path) -> None:
    """#5: resume on a running session waited for the step's lock, so it
    timed out after 120 s like a daemon outage instead of saying so."""
    state = _state(tmp_path)
    service = _service(state)
    service._run_task = asyncio.create_task(asyncio.sleep(10))
    try:
        with _StepHoldingLock(state):
            with pytest.raises(ValueError, match="conversation_already_running"):
                await asyncio.wait_for(service.run(), timeout=2)
    finally:
        service._run_task.cancel()


@pytest.mark.asyncio
async def test_finalize_gives_up_on_a_held_step_without_pausing_later(
    tmp_path, monkeypatch
) -> None:
    """#5: finalize on a run stuck inside a step blocked for 120 s, and the
    abandoned call could still pause the session once the lock freed."""
    monkeypatch.setattr(event_service_mod, "FINALIZE_STEP_WAIT_SECONDS", 0.2)
    state = _state(tmp_path)
    user = _user(state, "do the thing")
    state.consumed_user_message_id = user.id
    service = _service(state)
    service._run_task = asyncio.create_task(asyncio.sleep(10))
    try:
        with _StepHoldingLock(state):
            with pytest.raises(ValueError, match="step_in_progress"):
                await asyncio.wait_for(service.finalize(), timeout=2)
        await asyncio.sleep(0.3)
        assert not service._conversation.pause.called
        assert state.execution_status == ConversationExecutionStatus.RUNNING
    finally:
        service._run_task.cancel()
