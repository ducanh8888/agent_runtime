from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, cast

import pytest


config = cast(Any, pytest.importorskip("agentrt.runtime.config"))
daemon = cast(Any, pytest.importorskip("agentrt.runtime.daemon"))


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTRT_STATE_DIR", str(tmp_path))


def test_stop_terminates_child_and_removes_daemon_file():
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    daemon._write_daemon_file(daemon.DaemonInfo(port=1234, token="token", pid=proc.pid))

    assert daemon.stop(timeout=0.1) is True
    proc.wait(timeout=2)
    assert not config.daemon_file().exists()


def test_daemon_env_sets_noninteractive_defaults(monkeypatch):
    names = {
        "GIT_EDITOR": "true",
        "EDITOR": "true",
        "VISUAL": "true",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
    }
    for name in names:
        monkeypatch.delenv(name, raising=False)
    env = daemon._daemon_env("token")
    assert {name: env[name] for name in names} == names


def test_daemon_env_preserves_operator_override(monkeypatch):
    monkeypatch.setenv("GIT_EDITOR", "vim")
    assert daemon._daemon_env("token")["GIT_EDITOR"] == "vim"


def test_history_records_bounded_starts_and_unexpected_exit():
    for _ in range(52):
        daemon._record_start()
    daemon._record_unexpected_exit()
    data = json.loads((config.state_dir() / "daemon_history.json").read_text())
    assert len(data["starts"]) == 50
    assert data["unexpected_exits"] == 1
    assert data["last_unexpected_exit_at"]
    assert daemon.history() == data


def test_ensure_running_counts_dead_daemon_metadata(monkeypatch):
    daemon._write_daemon_file(daemon.DaemonInfo(port=1, token="old", pid=99999999))
    monkeypatch.setattr(daemon, "_proc_daemons", lambda: [])
    monkeypatch.setattr(daemon, "_daemon_env", lambda token: {})

    class ExitedProcess:
        pid = 99999999

        @staticmethod
        def poll():
            return 1

    monkeypatch.setattr(
        daemon.subprocess, "Popen", lambda *args, **kwargs: ExitedProcess()
    )
    with pytest.raises(RuntimeError, match="exited during startup"):
        daemon.ensure_running(startup_timeout=0.1)
    assert daemon.history()["unexpected_exits"] == 1


def test_stop_keeps_metadata_if_process_survives_signals(monkeypatch):
    daemon._write_daemon_file(
        daemon.DaemonInfo(port=1234, token="token", pid=os.getpid())
    )
    monkeypatch.setattr(daemon, "_process_alive", lambda pid: True)
    monkeypatch.setattr(daemon.time, "monotonic", iter([0, 100, 101, 107]).__next__)
    monkeypatch.setattr(daemon.time, "sleep", lambda _: None)
    monkeypatch.setattr(daemon, "_terminate", lambda pid: None)
    monkeypatch.setattr(daemon.os, "kill", lambda pid, sig: None)
    with pytest.raises(RuntimeError, match="daemon.json was retained"):
        daemon.stop(timeout=0)
    assert config.daemon_file().exists()


def test_ensure_running_adopts_discovered_daemon(monkeypatch):
    adopted = daemon.DaemonInfo(port=4321, token="adopted-token", pid=os.getpid())
    monkeypatch.setattr(daemon, "_proc_daemons", lambda: [adopted])
    monkeypatch.setattr(daemon, "is_alive", lambda info: False)
    result = daemon.ensure_running()
    assert result == adopted
    assert daemon.read_info() == adopted


def test_ensure_running_refuses_multiple_discovered_daemons(monkeypatch):
    candidates = [
        daemon.DaemonInfo(port=4321, token="one", pid=111),
        daemon.DaemonInfo(port=4322, token="two", pid=222),
    ]
    monkeypatch.setattr(daemon, "_proc_daemons", lambda: candidates)
    with pytest.raises(RuntimeError, match=r"pid=111 port=4321.*pid=222 port=4322"):
        daemon.ensure_running()


def test_daemon_env_keeps_worktrees_in_the_state_dir():
    env = daemon._daemon_env("token")
    assert env["AGENTRT_CONVERSATION_WORKTREE_ROOT"] == str(
        config.state_dir() / "worktrees"
    )
