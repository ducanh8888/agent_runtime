"""H2: runtime client projections for result scope, progress and errors.

The client is exercised against a MockTransport, so every request and response
is deterministic and no daemon is started.
"""

from __future__ import annotations

import types
import uuid
from collections.abc import Callable
from pathlib import Path

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


def test_status_surfaces_the_pinned_commit_for_snapshot_mode() -> None:
    """H8 item 8: a caller can check what a session actually ran against
    without a separate call."""
    client = _mock_client(
        lambda request: httpx.Response(
            200,
            json=_conversation(
                workspace_mode="snapshot", workspace_resolved_sha="deadbee"
            ),
        )
    )
    status = client.status(SESSION)
    assert status["workspace_mode"] == "snapshot"
    assert status["workspace_resolved_sha"] == "deadbee"


def test_status_omits_pinned_commit_for_shared_mode() -> None:
    client = _mock_client(lambda request: httpx.Response(200, json=_conversation()))
    status = client.status(SESSION)
    assert "workspace_resolved_sha" not in status
    assert "workspace_mode" not in status


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


def _reports_dir(tmp_path, monkeypatch) -> Path:
    """Point `config.state_dir()` at a tmpdir and return this session's
    reports directory, matching the convention `guarded_tools.py` uses
    server-side: `<state_dir>/conversations/<uuid-hex>/reports`."""
    monkeypatch.setenv("AGENTRT_STATE_DIR", str(tmp_path))
    conversation_dir = tmp_path / "conversations" / uuid.UUID(SESSION).hex
    reports_dir = conversation_dir / "reports"
    reports_dir.mkdir(parents=True)
    return reports_dir


def test_artifacts_lists_reports_alongside_workspace_files(
    tmp_path, monkeypatch
) -> None:
    """H8 item 7: a readonly/inspect session's report channel is listed the
    same way the workspace is, independently of it."""
    reports_dir = _reports_dir(tmp_path, monkeypatch)
    (reports_dir / "findings.md").write_text("# findings\n")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_conversation())

    client = _mock_client(handler)
    listing = client.artifacts(SESSION)
    assert listing["reports"] == [
        {
            "path": "findings.md",
            "size": len("# findings\n"),
            "modified": listing["reports"][0]["modified"],
        }
    ]
    # No workspace: the listing is unavailable, and `reports` is still there.
    assert listing["outcome"] == client_mod.ARTIFACTS_UNAVAILABLE


def test_artifacts_no_reports_directory_is_empty_list(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTRT_STATE_DIR", str(tmp_path))

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_conversation())

    client = _mock_client(handler)
    assert client.artifacts(SESSION)["reports"] == []


def test_artifacts_path_reads_report_before_workspace(tmp_path, monkeypatch) -> None:
    """A report and a same-named workspace file are different files; the
    report is the one this channel exists to read back."""
    reports_dir = _reports_dir(tmp_path, monkeypatch)
    (reports_dir / "answer.txt").write_text("from the report channel")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "answer.txt").write_text("from the workspace")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_conversation(workspace={"working_dir": str(workspace)}),
        )

    client = _mock_client(handler)
    result = client.artifacts(SESSION, path="answer.txt")
    assert result["content"] == "from the report channel"


def test_artifacts_path_falls_back_to_workspace_when_not_a_report(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("AGENTRT_STATE_DIR", str(tmp_path))
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "only_in_workspace.txt").write_text("workspace content")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/only_in_workspace.txt"):
            return httpx.Response(200, text="workspace content")
        return httpx.Response(
            200,
            json=_conversation(workspace={"working_dir": str(workspace)}),
        )

    client = _mock_client(handler)
    result = client.artifacts(SESSION, path="only_in_workspace.txt")
    assert result["content"] == "workspace content"
