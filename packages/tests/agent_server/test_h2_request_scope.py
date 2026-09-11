"""H2: request-scoped result state, admission status and progress derivation.

Pure functions over the persisted conversation state, so no daemon is started
and no model is called.
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from agentrt.agent_server.models import AgentResponseResult
from agentrt.agent_server.run_scope import (
    AdmissionStatus,
    AgentResponseState,
    derive_admission_status,
    derive_result_state,
    iterations_remaining,
)
from agentrt.sdk.conversation.request import StartConversationRequest
from agentrt.sdk.conversation.state import ConversationExecutionStatus


def _state(**overrides):
    base = {
        "last_user_message_id": None,
        "consumed_user_message_id": None,
        "execution_status": ConversationExecutionStatus.IDLE,
        "iterations_used": 0,
        "max_iterations": 500,
        "events": [],
        # Added by H3; the projection reads them, and a fake missing one fails
        # rather than silently diverging from the real state.
        "final_summary": None,
        "finalized_request_id": None,
        "finalized_at": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _service(events, **overrides):
    """An EventService whose conversation state is a plain namespace.

    The projection reads only a handful of state fields, so this exercises the
    real route-side code without building a full LocalConversation.
    """
    from pathlib import Path
    from unittest.mock import MagicMock

    from agentrt.agent_server.event_service import EventService

    conversation = SimpleNamespace(_state=_state(events=events, **overrides))
    service = EventService(stored=MagicMock(), conversations_dir=Path("test_dir"))
    service._conversation = conversation
    return service


def _user(event_id: str, text: str):
    from agentrt.sdk.event import MessageEvent
    from agentrt.sdk.llm import Message, TextContent

    return MessageEvent(
        id=event_id,
        source="user",
        llm_message=Message(role="user", content=[TextContent(text=text)]),
    )


def _agent(event_id: str, text: str):
    from agentrt.sdk.event import MessageEvent
    from agentrt.sdk.llm import Message, TextContent

    return MessageEvent(
        id=event_id,
        source="agent",
        llm_message=Message(role="assistant", content=[TextContent(text=text)]),
    )


# --- result state ---------------------------------------------------------


def test_new_input_not_yet_consumed_is_pending() -> None:
    state = _state(last_user_message_id="u2", consumed_user_message_id="u1")
    assert derive_result_state(state) is AgentResponseState.PENDING


def test_consumed_boundary_still_running_is_pending() -> None:
    state = _state(
        last_user_message_id="u1",
        consumed_user_message_id="u1",
        execution_status=ConversationExecutionStatus.RUNNING,
    )
    assert derive_result_state(state) is AgentResponseState.PENDING


def test_finished_run_is_final() -> None:
    state = _state(
        last_user_message_id="u1",
        consumed_user_message_id="u1",
        execution_status=ConversationExecutionStatus.FINISHED,
    )
    assert derive_result_state(state) is AgentResponseState.FINAL


def test_run_stopped_early_is_partial() -> None:
    state = _state(
        last_user_message_id="u1",
        consumed_user_message_id="u1",
        execution_status=ConversationExecutionStatus.ERROR,
    )
    assert derive_result_state(state) is AgentResponseState.PARTIAL


def test_legacy_session_without_boundary_is_unavailable() -> None:
    state = _state(last_user_message_id="u1")
    assert derive_result_state(state) is AgentResponseState.UNAVAILABLE


def test_session_with_no_input_is_unavailable() -> None:
    assert derive_result_state(_state()) is AgentResponseState.UNAVAILABLE


# --- admission ------------------------------------------------------------


def test_accepted_input_without_a_run_is_queued() -> None:
    state = _state(last_user_message_id="u1")
    assert derive_admission_status(state) is AdmissionStatus.QUEUED


def test_paused_with_unconsumed_input_is_queued() -> None:
    state = _state(
        last_user_message_id="u1",
        execution_status=ConversationExecutionStatus.PAUSED,
    )
    assert derive_admission_status(state) is AdmissionStatus.QUEUED


def test_run_started_without_a_step_is_preparing() -> None:
    state = _state(
        last_user_message_id="u1",
        consumed_user_message_id="u1",
        execution_status=ConversationExecutionStatus.RUNNING,
    )
    assert derive_admission_status(state) is AdmissionStatus.PREPARING


def test_run_with_a_completed_step_is_admitted() -> None:
    state = _state(
        last_user_message_id="u1",
        consumed_user_message_id="u1",
        execution_status=ConversationExecutionStatus.RUNNING,
        iterations_used=3,
    )
    assert derive_admission_status(state) is AdmissionStatus.ADMITTED


def test_active_run_with_a_newer_input_is_admitted() -> None:
    """The run is admitted; it simply has not reached the newer input yet."""
    state = _state(
        last_user_message_id="u2",
        consumed_user_message_id="u1",
        execution_status=ConversationExecutionStatus.RUNNING,
    )
    assert derive_admission_status(state) is AdmissionStatus.ADMITTED


def test_finished_legacy_session_is_not_reported_queued() -> None:
    state = _state(
        last_user_message_id="u1",
        execution_status=ConversationExecutionStatus.FINISHED,
    )
    assert derive_admission_status(state) is AdmissionStatus.ADMITTED


# --- progress -------------------------------------------------------------


def test_iterations_remaining_counts_down() -> None:
    assert iterations_remaining(_state(iterations_used=5)) == 495


def test_iterations_remaining_never_negative() -> None:
    assert iterations_remaining(_state(iterations_used=600)) == 0


# --- request-scoped projection -------------------------------------------


def test_projection_returns_the_answer_for_the_consumed_input() -> None:
    service = _service(
        [_user("u1", "q"), _agent("a1", "answer one")],
        last_user_message_id="u1",
        consumed_user_message_id="u1",
        execution_status=ConversationExecutionStatus.FINISHED,
        iterations_used=2,
    )
    result = service._get_agent_response_result_sync()
    assert result.state is AgentResponseState.FINAL
    assert result.response == "answer one"
    assert result.request_message_id == "u1"
    assert result.iterations_used == 2
    assert result.iterations_remaining == 498


def test_projection_pending_never_returns_the_previous_answer() -> None:
    service = _service(
        [_user("u1", "q1"), _agent("a1", "answer one"), _user("u2", "q2")],
        last_user_message_id="u2",
        consumed_user_message_id="u1",
        execution_status=ConversationExecutionStatus.RUNNING,
    )
    result = service._get_agent_response_result_sync()
    assert result.state is AgentResponseState.PENDING
    assert result.response is None
    assert result.request_message_id is None


def test_projection_pending_carries_no_previous_error_or_tool() -> None:
    """A pending request must not be shown the previous request's failure."""
    from agentrt.sdk.event.conversation_error import ConversationErrorEvent

    service = _service(
        [
            _user("u1", "q1"),
            _agent("a1", "answer one"),
            ConversationErrorEvent(
                source="environment",
                code="MaxIterationsReached",
                detail="previous request ran out",
            ),
            _user("u2", "q2"),
        ],
        last_user_message_id="u2",
        consumed_user_message_id="u1",
        execution_status=ConversationExecutionStatus.IDLE,
    )
    result = service._get_agent_response_result_sync()
    assert result.state is AgentResponseState.PENDING
    assert result.response is None
    assert result.error is None
    assert result.last_completed_tool is None
    assert result.last_progress_at is None


def test_projection_unknown_boundary_is_unavailable() -> None:
    """A boundary missing from the event log cannot scope a final answer."""
    service = _service(
        [_user("u1", "q"), _agent("a1", "answer one")],
        last_user_message_id="u1",
        consumed_user_message_id="not-in-log",
        execution_status=ConversationExecutionStatus.FINISHED,
    )
    result = service._get_agent_response_result_sync()
    assert result.state is AgentResponseState.UNAVAILABLE
    assert result.response is None
    assert result.request_message_id is None


def test_projection_last_tool_includes_an_errored_observation() -> None:
    """An errored tool attempt is still the last completed tool."""
    from agentrt.sdk.event import AgentErrorEvent

    service = _service(
        [
            _user("u1", "q"),
            AgentErrorEvent(
                source="environment",
                tool_name="terminal",
                tool_call_id="tc1",
                error="command failed",
            ),
        ],
        last_user_message_id="u1",
        consumed_user_message_id="u1",
        execution_status=ConversationExecutionStatus.ERROR,
    )
    result = service._get_agent_response_result_sync()
    assert result.state is AgentResponseState.PARTIAL
    assert result.last_completed_tool == "terminal"


def test_projection_reports_a_terminal_error_as_partial() -> None:
    from agentrt.sdk.event.conversation_error import ConversationErrorEvent

    service = _service(
        [
            _user("u1", "q"),
            _agent("a1", "partial answer"),
            ConversationErrorEvent(
                source="environment",
                code="MaxIterationsReached",
                detail="Agent reached maximum iterations limit (5).",
            ),
        ],
        last_user_message_id="u1",
        consumed_user_message_id="u1",
        execution_status=ConversationExecutionStatus.ERROR,
    )
    result = service._get_agent_response_result_sync()
    assert result.state is AgentResponseState.PARTIAL
    assert result.response == "partial answer"
    assert result.error is not None
    assert result.error.code == "MaxIterationsReached"


# --- legacy fixtures ------------------------------------------------------


def test_legacy_state_fixture_loads_and_reports_unavailable() -> None:
    """A persisted session written before H2 has no boundary.

    It must still load, and must report unavailable rather than inventing a
    request scope from whatever answer it happens to carry.
    """
    import json
    from pathlib import Path

    from agentrt.sdk.conversation.state import ConversationState

    path = (
        Path(__file__).parents[1]
        / "fixtures"
        / "conversations"
        / "v1_11_5_cli_default"
        / "base_state.json"
    )
    state = ConversationState.model_validate(json.loads(path.read_text()))

    assert state.consumed_user_message_id is None
    assert state.iterations_used == 0
    assert derive_result_state(state) is AgentResponseState.UNAVAILABLE
    assert derive_admission_status(state) is AdmissionStatus.ADMITTED


# --- wire contracts -------------------------------------------------------


def test_pending_result_serializes_null_response() -> None:
    payload = AgentResponseResult(
        response=None, state=AgentResponseState.PENDING
    ).model_dump(mode="json")
    assert payload["response"] is None
    assert payload["state"] == "pending"


def test_final_empty_answer_is_distinguishable() -> None:
    payload = AgentResponseResult(
        response="", state=AgentResponseState.FINAL
    ).model_dump(mode="json")
    assert payload["response"] == ""
    assert payload["state"] == "final"


def test_legacy_response_payload_still_parses() -> None:
    """A pre-H2 body carries only `response`; it must still validate."""
    result = AgentResponseResult.model_validate({"response": "old answer"})
    assert result.response == "old answer"
    assert result.state is AgentResponseState.UNAVAILABLE


# --- explicit title at create --------------------------------------------


def _create_request(tmp_path, **overrides) -> StartConversationRequest:
    payload = {
        "workspace": {"working_dir": str(tmp_path)},
        "agent_profile_id": str(uuid4()),
        "title": "Named task",
    }
    payload.update(overrides)
    return StartConversationRequest.model_validate(payload)


def test_create_request_accepts_an_explicit_title(tmp_path) -> None:
    request = _create_request(tmp_path)
    assert request.title == "Named task"
    # The title must survive the dump the create path persists from, otherwise
    # it is dropped exactly as it was before H2.
    assert request.model_dump()["title"] == "Named task"


def test_create_request_title_is_bounded(tmp_path) -> None:
    with pytest.raises(ValueError):
        _create_request(tmp_path, title="")
    with pytest.raises(ValueError):
        _create_request(tmp_path, title="x" * 201)
