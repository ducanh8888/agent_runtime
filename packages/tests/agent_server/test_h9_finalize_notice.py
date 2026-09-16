"""H9 item 6's second half: `finalize` is visible to the agent it stops.

`pause` is observable to a UI and to nobody else -- `PauseEvent` is not an
`LLMConvertibleEvent` -- so a session stopped by `finalize` and resumed later
had no record of why its run ended. Same blind spot the interrupt notice closes,
except here the caller knows the cause, so the notice can name it.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from pydantic import SecretStr

from agentrt.agent_server.event_service import EventService
from agentrt.sdk import Agent
from agentrt.sdk.conversation.state import (
    ConversationExecutionStatus,
    ConversationState,
)
from agentrt.sdk.event import MessageEvent
from agentrt.sdk.llm import LLM, Message, TextContent
from agentrt.sdk.workspace import LocalWorkspace


def _state(tmp_path, status=ConversationExecutionStatus.RUNNING) -> ConversationState:
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
    event = MessageEvent(
        source="user",
        llm_message=Message(role="user", content=[TextContent(text="do it")]),
    )
    state.append_event(event)
    state.last_user_message_id = event.id
    state.consumed_user_message_id = event.id
    return state


class _RunningTask:
    """Stands in for an in-flight run task."""

    def done(self) -> bool:
        return False


def _service(state, *, status_after_pause, run_in_flight: bool = True):
    notes: list[str] = []

    def _pause() -> None:
        state.execution_status = status_after_pause

    conversation = SimpleNamespace(
        _state=state,
        pause=MagicMock(side_effect=_pause),
        note_external_stop=notes.append,
        agent=None,
    )
    service = EventService(stored=MagicMock(), conversations_dir=Path("test_dir"))
    service._conversation = conversation
    if run_in_flight:
        service._run_task = _RunningTask()
    return service, notes


@pytest.mark.asyncio
async def test_finalize_on_a_running_session_records_the_stop(tmp_path) -> None:
    state = _state(tmp_path)
    service, notes = _service(
        state, status_after_pause=ConversationExecutionStatus.PAUSED
    )

    await service.finalize()

    assert len(notes) == 1, notes
    assert "finalized" in notes[0].lower()


@pytest.mark.asyncio
async def test_an_idle_session_gets_no_stop_notice(tmp_path) -> None:
    """Nothing was stopped, so there is nothing to explain."""
    state = _state(tmp_path, status=ConversationExecutionStatus.IDLE)
    service, notes = _service(
        state,
        status_after_pause=ConversationExecutionStatus.IDLE,
        run_in_flight=False,
    )

    await service.finalize()

    assert notes == []


@pytest.mark.asyncio
async def test_a_run_that_finished_during_the_pause_gets_no_stop_notice(
    tmp_path,
) -> None:
    """The false-claim guard.

    A run can complete on its own inside the pause window; it ends FINISHED,
    not PAUSED. Saying finalize stopped it would be a lie written into the
    agent's own history, which is the failure mode worth pinning.
    """
    state = _state(tmp_path)
    service, notes = _service(
        state, status_after_pause=ConversationExecutionStatus.FINISHED
    )

    await service.finalize()

    assert notes == []
