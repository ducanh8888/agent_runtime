"""Tests for Client.watch and CLI 'agentrt watch'.

Exercises streaming state changes (status, tool actions, error events, settled)
against httpx.MockTransport.
"""

from __future__ import annotations

import types
from collections.abc import Callable

import httpx
import pytest

from agentrt.runtime import client as client_mod
from agentrt.runtime.cli import main


SESSION_A = "11111111-1111-1111-1111-111111111111"
SESSION_B = "22222222-2222-2222-2222-222222222222"

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


def test_watch_yields_state_changes_in_order() -> None:
    """watch() yields status, tool action, error, and settled state changes."""
    status_samples = [
        {"execution_status": "running", "result_state": "pending"},
        {"execution_status": "running", "result_state": "pending"},
        {"execution_status": "finished", "result_state": "final"},
        {"execution_status": "finished", "result_state": "final"},
    ]
    status_idx = 0

    events_page_1 = {
        "items": [
            {
                "id": "ev-1",
                "kind": "ActionEvent",
                "timestamp": "2026-09-30T10:00:00Z",
                "tool_name": "file_editor",
                "thought": "viewing file",
                "action": {"path": "src/main.py", "view_range": [1, 10]},
            }
        ],
        "next_page_id": None,
    }
    events_page_2 = {
        "items": [
            {
                "id": "ev-2",
                "kind": "ConversationErrorEvent",
                "timestamp": "2026-09-30T10:00:01Z",
                "code": "CheckFailed",
                "detail": "test failed",
            },
            {
                "id": "ev-1",
                "kind": "ActionEvent",
                "timestamp": "2026-09-30T10:00:00Z",
                "tool_name": "file_editor",
                "thought": "viewing file",
                "action": {"path": "src/main.py", "view_range": [1, 10]},
            },
        ],
        "next_page_id": None,
    }

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_idx
        path = request.url.path
        if path.endswith("/agent_final_response"):
            return httpx.Response(200, json={"response": "done", "state": "final"})
        if path.endswith("/events/search"):
            # Return page 2 after status sample 1
            page = events_page_2 if status_idx >= 2 else events_page_1
            return httpx.Response(200, json=page)
        # /api/conversations/{id}
        payload = status_samples[min(status_idx, len(status_samples) - 1)]
        status_idx += 1
        body = {
            "id": SESSION_A,
            "title": "fix bug",
            "workspace": {"working_dir": "/tmp"},
            "tags": {},
            **payload,
        }
        return httpx.Response(200, json=body)

    client = _mock_client(handler)
    changes = list(client.watch([SESSION_A], interval=0.01))

    kinds = [c["kind"] for c in changes]
    assert "status" in kinds
    assert "tool" in kinds
    assert "error" in kinds
    assert "settled" in kinds

    # Verify summaries
    tool_changes = [c for c in changes if c["kind"] == "tool"]
    assert len(tool_changes) == 1
    assert "file_editor" in tool_changes[0]["summary"]
    assert len(tool_changes[0]["summary"]) <= 120

    error_changes = [c for c in changes if c["kind"] == "error"]
    assert len(error_changes) == 1
    assert "CheckFailed" in error_changes[0]["summary"]

    settled_changes = [c for c in changes if c["kind"] == "settled"]
    assert len(settled_changes) == 1
    assert settled_changes[0]["summary"] == "settled: completed"


def test_cli_watch_prints_one_line_per_state_change(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """CLI watch formats each change as '<short_id> <time> <summary>' and exits 0."""
    status_samples = [
        {"execution_status": "finished", "result_state": "final"},
        {"execution_status": "finished", "result_state": "final"},
    ]
    status_idx = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal status_idx
        path = request.url.path
        if path.endswith("/agent_final_response"):
            return httpx.Response(200, json={"response": "done", "state": "final"})
        if path.endswith("/events/search"):
            return httpx.Response(200, json={"items": [], "next_page_id": None})
        payload = status_samples[min(status_idx, len(status_samples) - 1)]
        status_idx += 1
        return httpx.Response(
            200,
            json={
                "id": SESSION_A,
                "title": "sample task",
                "workspace": {"working_dir": "/tmp"},
                "tags": {},
                **payload,
            },
        )

    mock = _mock_client(handler)
    monkeypatch.setattr(client_mod, "Client", lambda: mock)

    code = main(["watch", SESSION_A, "--interval", "0.01"])
    assert code == 0

    out = capsys.readouterr().out
    lines = [line.strip() for line in out.strip().split("\n") if line.strip()]
    assert len(lines) >= 2  # status + settled
    for line in lines:
        parts = line.split(" ", 2)
        assert len(parts) == 3
        short_id, local_time, summary = parts
        assert short_id == client_mod.short_id(SESSION_A)
        assert ":" in local_time  # HH:MM:SS
        assert len(summary) <= 120


def test_cli_watch_until_any(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--until any stops as soon as any session settles."""
    # SESSION_A is finished, SESSION_B is still running
    statuses = {
        SESSION_A: {"execution_status": "finished", "result_state": "final"},
        SESSION_B: {"execution_status": "running", "result_state": "pending"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/agent_final_response"):
            return httpx.Response(200, json={"response": "done", "state": "final"})
        if path.endswith("/events/search"):
            return httpx.Response(200, json={"items": [], "next_page_id": None})
        sid = path.rsplit("/", 1)[-1]
        st = statuses.get(sid, {})
        return httpx.Response(
            200,
            json={
                "id": sid,
                "title": "task",
                "workspace": {"working_dir": "/tmp"},
                "tags": {},
                **st,
            },
        )

    mock = _mock_client(handler)
    monkeypatch.setattr(client_mod, "Client", lambda: mock)

    code = main(["watch", SESSION_A, SESSION_B, "--interval", "0.01", "--until", "any"])
    assert code == 0


def test_watch_missing_session() -> None:
    """A 404 session settles as missing without blocking."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "not found"})

    client = _mock_client(handler)
    missing_id = "00000000-0000-0000-0000-000000000000"
    changes = list(client.watch([missing_id], interval=0.01))

    summaries = [c["summary"] for c in changes]
    assert "status: missing" in summaries
    assert "settled: missing" in summaries
