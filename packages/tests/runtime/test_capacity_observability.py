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
