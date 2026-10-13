from __future__ import annotations

import json
import types
from datetime import datetime, timedelta

import httpx

from agentrt.runtime import client as client_mod, hostinfo


SESSION = "11111111-1111-1111-1111-111111111111"


def _proc_entry(root, pid: int, ppid: int, start_ticks: int, rss_pages: int, cmd: str):
    directory = root / str(pid)
    directory.mkdir()
    fields = ["S", str(ppid)] + ["0"] * 17 + [str(start_ticks)]
    (directory / "stat").write_text(f"{pid} ({cmd}) " + " ".join(fields))
    (directory / "statm").write_text(f"100 {rss_pages} 0")
    (directory / "cmdline").write_bytes(cmd.encode() + b"\0")


def test_capacity_adds_fake_proc_daemon_and_stall_data(monkeypatch, tmp_path):
    proc = tmp_path / "proc"
    proc.mkdir()
    (proc / "meminfo").write_text("MemTotal: 1000 kB\nMemAvailable: 400 kB\n")
    (proc / "stat").write_text("btime 1000000000\n")
    _proc_entry(proc, 50, 1, 100, 2, "daemon")
    _proc_entry(proc, 51, 50, 200, 3, "worker")
    _proc_entry(proc, 52, 51, 300, 4, "grandchild")
    monkeypatch.setattr(hostinfo, "PROC_ROOT", proc)
    monkeypatch.setattr(
        client_mod.daemon, "read_info", lambda: types.SimpleNamespace(pid=50)
    )
    monkeypatch.setattr(client_mod.config, "state_dir", lambda: tmp_path)
    (tmp_path / "daemon_history.json").write_text(
        json.dumps(
            {
                "starts": ["history-start"],
                "unexpected_exits": 2,
                "last_unexpected_exit_at": "exit-time",
            }
        )
    )
    old = (
        (datetime.now().astimezone() - timedelta(seconds=1200))
        .replace(tzinfo=None)
        .isoformat()
    )

    def handler(request):
        path = request.url.path
        if path.endswith("/capacity"):
            return httpx.Response(
                200,
                json={
                    "in_flight_llm": 0,
                    "llm_limit": None,
                    "recovered_after_restart": True,
                    "server_started_at": "server-start",
                },
            )
        if path == "/api/conversations/search":
            return httpx.Response(
                200,
                json={
                    "sessions": [
                        {
                            "id": SESSION,
                            "title": "running",
                            "execution_status": "running",
                        }
                    ]
                },
            )
        if path.endswith("/events/search"):
            return httpx.Response(
                200, json={"items": [{"kind": "ActionEvent", "timestamp": old}]}
            )
        if path.endswith("/" + SESSION):
            return httpx.Response(
                200,
                json={
                    "id": SESSION,
                    "execution_status": "running",
                    "iterations_used": 8,
                },
            )
        return httpx.Response(404)

    client = client_mod.Client()
    client._daemon_info = types.SimpleNamespace(
        base_url="http://daemon.test", token="token"
    )
    client._profile_ref = {}
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    monkeypatch.setenv("AGENTRT_STALL_SECONDS", "900")
    result = client.capacity()

    assert result["recovered_after_restart"] is True
    assert result["server_started_at"] == "server-start"
    assert result["host"]["total_memory_bytes"] == 1_024_000
    assert result["host"]["daemon_process_tree_rss_bytes"] == 9 * hostinfo._PAGE_SIZE
    assert result["host"]["biggest_children"][0]["rss_bytes"] == 7 * hostinfo._PAGE_SIZE
    assert result["daemon"]["unexpected_exits"] == 2
    assert result["daemon"]["started_at"] != "history-start"
    session = result["running_sessions"][0]
    assert session["id"] == SESSION[:8]
    assert session["iterations_used"] == 8
    assert session["last_event_at"] == old
    assert session["stalled"] is True
    # The newest event is a tool call with no result after it (#15).
    assert session["waiting_on"].startswith("tool:")


def test_waiting_on_tells_a_pending_tool_from_a_pending_model_reply() -> None:
    waiting_on = client_mod._waiting_on

    assert waiting_on({"type": "action", "tool": "terminal"}) == "tool:terminal"
    assert waiting_on({"type": "observation", "tool": "terminal"}) == "model"
    assert waiting_on({"type": "message", "role": "user"}) == "model"
    assert waiting_on({"type": "error"}) is None
    assert waiting_on(None) is None


def test_capacity_uses_the_daemons_running_sessions_in_one_request(
    monkeypatch, tmp_path
) -> None:
    """`capacity` must not page the store and read each running session (#16).

    The sweep it used to do -- list a thousand rows, then `status` and
    `transcript` per running session -- was about 28 requests per poll and
    most of the 7.5 s an orchestrator measured on a 3,000-session store.
    """
    monkeypatch.setattr(client_mod.config, "state_dir", lambda: tmp_path)
    monkeypatch.setattr(
        client_mod.daemon, "read_info", lambda: types.SimpleNamespace(pid=50)
    )
    monkeypatch.setenv("AGENTRT_STALL_SECONDS", "900")

    progress_at = (
        (datetime.now().astimezone() - timedelta(seconds=1200))
        .replace(tzinfo=None)
        .isoformat()
    )
    live_at = datetime.now().astimezone().replace(tzinfo=None).isoformat()
    requests: list[str] = []

    def handler(request):
        requests.append(request.url.path)
        assert request.url.path.endswith("/capacity"), request.url.path
        return httpx.Response(
            200,
            json={
                "running_sessions": [
                    {
                        "id": SESSION,
                        "short_id": SESSION[:8],
                        "title": "wedged",
                        "iterations_used": 12,
                        "last_event_at": live_at,
                        "last_progress": {
                            "timestamp": progress_at,
                            "type": "action",
                            "tool": "terminal",
                        },
                    }
                ],
                "loaded_conversations": 41,
            },
        )

    client = client_mod.Client()
    client._daemon_info = types.SimpleNamespace(
        base_url="http://daemon.test", token="token"
    )
    client._profile_ref = {}
    client._http = httpx.Client(transport=httpx.MockTransport(handler))

    result = client.capacity()

    assert requests == ["/api/conversations/capacity"]
    assert result["loaded_conversations"] == 41
    [session] = result["running_sessions"]
    assert session["iterations_used"] == 12
    # Alive (a state update just arrived) but not progressing for 20 minutes.
    assert session["last_event_at"] == live_at
    assert session["last_progress_at"] == progress_at
    assert session["stalled"] is True
    assert session["waiting_on"] == "tool:terminal"
