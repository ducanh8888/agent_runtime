"""H2: runtime client projections for result scope, progress and errors.

The client is exercised against a MockTransport, so every request and response
is deterministic and no daemon is started.
"""

from __future__ import annotations

import types
from collections.abc import Callable

import httpx

from agentrt.runtime import client as client_mod


SESSION = "22222222-2222-2222-2222-222222222222"
Handler = Callable[[httpx.Request], httpx.Response]


def _mock_client(handler: Handler) -> client_mod.Client:
    client = client_mod.Client()
    setattr(
        client,
        "_daemon_info",
        types.SimpleNamespace(base_url="http://daemon.test", token="test-token"),
    )
    client._profile_ref = {}
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def _conversation(**overrides) -> dict:
    payload = {
        "id": SESSION,
        "execution_status": "running",
        "tags": {},
        "result_state": "pending",
        "admission_status": "queued",
        "iterations_used": 0,
        "iterations_remaining": 500,
    }
    payload.update(overrides)
    return payload


def test_status_surfaces_request_scope() -> None:
    client = _mock_client(
        lambda request: httpx.Response(
            200,
            json=_conversation(
                result_state="partial",
                admission_status="admitted",
                iterations_used=7,
                iterations_remaining=493,
            ),
        )
    )
    status = client.status(SESSION)
    assert status["result_state"] == "partial"
    assert status["admission_status"] == "admitted"
    assert status["iterations_used"] == 7
    assert status["iterations_remaining"] == 493


def test_result_pending_is_null_not_the_previous_answer() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agent_final_response"):
            return httpx.Response(
                200,
                json={
                    "response": None,
                    "state": "pending",
                    "request_message_id": None,
                    "iterations_used": 0,
                },
            )
        return httpx.Response(200, json=_conversation())

    client = _mock_client(handler)
    payload = client.result(SESSION)
    assert payload["result"] is None
    assert payload["state"] == "pending"
    # A pending request has no boundary; the client must not invent one.
    assert "request_message_id" not in payload


def test_result_error_adds_a_deterministic_progress_summary() -> None:
    """H8 item 10: an errored result carries a tool-call tally, not an LLM
    summary -- available for free, even after the run already stopped."""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/agent_final_response"):
            return httpx.Response(
                200,
                json={
                    "response": "",
                    "state": "partial",
                    "request_message_id": "u1",
                    "iterations_used": 3,
                    "error": {"code": "MaxIterationsReached", "detail": "limit"},
                },
            )
        if path.endswith("/events/search"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {"kind": "ActionEvent", "tool_name": "file_editor"},
                        {"kind": "ActionEvent", "tool_name": "file_editor"},
                        {"kind": "ActionEvent", "tool_name": "terminal"},
                    ],
                    "next_page_id": None,
                },
            )
        return httpx.Response(200, json=_conversation(execution_status="error"))

    client = _mock_client(handler)
    payload = client.result(SESSION)
    assert payload["status"] == "error"
    assert payload["progress_summary"] == "file_editor x2, terminal x1"


def test_result_non_error_has_no_progress_summary() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agent_final_response"):
            return httpx.Response(
                200, json={"response": "done", "state": "final"}
            )
        return httpx.Response(200, json=_conversation(execution_status="finished"))

    client = _mock_client(handler)
    payload = client.result(SESSION)
    assert "progress_summary" not in payload


def test_result_final_empty_is_kept() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agent_final_response"):
            return httpx.Response(
                200,
                json={
                    "response": "",
                    "state": "final",
                    "request_message_id": "u1",
                    "iterations_used": 2,
                    "iterations_remaining": 498,
                    "last_completed_tool": "terminal",
                    "last_progress_at": "2026-01-01T00:00:05Z",
                    "error": {"code": "MaxIterationsReached", "detail": "limit"},
                },
            )
        return httpx.Response(200, json=_conversation(execution_status="finished"))

    client = _mock_client(handler)
    payload = client.result(SESSION)
    assert payload["result"] == ""
    assert payload["state"] == "final"
    assert payload["iterations_used"] == 2
    assert payload["last_completed_tool"] == "terminal"
    assert payload["last_progress_at"] == "2026-01-01T00:00:05Z"
    assert payload["error"]["code"] == "MaxIterationsReached"


def test_transcript_includes_errors_and_action_locations() -> None:
    items = [
        {
            "kind": "ActionEvent",
            "id": "act-1",
            "tool_name": "file_editor",
            "thought": [{"type": "text", "text": "reading"}],
            "action": {"path": "/repo/src/app.py", "view_range": [10, 20]},
        },
        {
            "kind": "ObservationEvent",
            "id": "obs-1",
            "tool_name": "file_editor",
            "observation": {"content": [{"type": "text", "text": "lines"}]},
        },
        {
            "kind": "ConversationErrorEvent",
            "id": "err-1",
            "code": "MaxIterationsReached",
            "detail": "Agent reached maximum iterations limit (5).",
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events/search"):
            return httpx.Response(200, json={"items": items, "next_page_id": None})
        return httpx.Response(200, json=_conversation())

    client = _mock_client(handler)
    events = client.transcript(SESSION)["events"]
    by_type = {event["type"]: event for event in events}
    assert set(by_type) == {"action", "observation", "error"}

    action = by_type["action"]
    assert action["id"] == "act-1"
    assert action["path"] == "/repo/src/app.py"
    assert action["range"] == [10, 20]

    assert by_type["observation"]["id"] == "obs-1"
    error = by_type["error"]
    assert error["code"] == "MaxIterationsReached"
    assert "maximum iterations" in error["detail"]


def test_transcript_strips_ansi_from_terminal_output() -> None:
    """H8 item 10: raw escape sequences from a terminal-tool observation do
    not reach a transcript reader as literal bytes."""
    raw = "[?2004l[32mok[0m\r\n"
    items = [
        {
            "kind": "ObservationEvent",
            "id": "obs-ansi",
            "tool_name": "terminal",
            "observation": {"content": [{"type": "text", "text": raw}]},
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events/search"):
            return httpx.Response(200, json={"items": items, "next_page_id": None})
        return httpx.Response(200, json=_conversation())

    client = _mock_client(handler)
    events = client.transcript(SESSION)["events"]
    output = events[0]["output"]
    assert output == "ok\r\n"
    assert "\x1b" not in output
