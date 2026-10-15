from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Any, cast

import pytest


config = cast(Any, pytest.importorskip("agentrt.runtime.config"))
daemon = cast(Any, pytest.importorskip("agentrt.runtime.daemon"))


@pytest.fixture(autouse=True)
def isolated_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTRT_STATE_DIR", str(tmp_path))


def test_stop_terminates_child_and_removes_daemon_file():
    # The marker argument makes it look like the server to stop().
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import time; time.sleep(120)",
            "agentrt.runtime.server_launch",
        ]
    )
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
    monkeypatch.setattr(daemon, "_is_agentrt_daemon", lambda pid: True)
    monkeypatch.setattr(daemon.time, "monotonic", iter([0, 100, 101, 107]).__next__)
    monkeypatch.setattr(daemon.time, "sleep", lambda _: None)
    monkeypatch.setattr(daemon, "_terminate", lambda pid: None)
    monkeypatch.setattr(daemon.os, "kill", lambda pid, sig: None)
    monkeypatch.setattr(daemon, "_kill", lambda pid: None)
    with pytest.raises(RuntimeError, match="daemon.json was retained"):
        daemon.stop(timeout=0)
    assert config.daemon_file().exists()


@pytest.mark.skipif(os.name != "nt", reason="Windows command-line inspection")
def test_is_agentrt_daemon_rejects_pytest_process_on_windows():
    assert daemon._is_agentrt_daemon(os.getpid()) is False


@pytest.mark.skipif(os.name != "nt", reason="Windows command-line inspection")
def test_stop_removes_metadata_for_live_non_daemon_process(monkeypatch):
    daemon._write_daemon_file(
        daemon.DaemonInfo(port=1234, token="token", pid=os.getpid())
    )
    calls = []
    monkeypatch.setattr(daemon, "_terminate", calls.append)
    monkeypatch.setattr(daemon, "_kill", calls.append)

    assert daemon.stop() is False
    assert not config.daemon_file().exists()
    assert calls == []


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


def test_daemon_env_keeps_worktrees_in_the_state_dir(monkeypatch):
    monkeypatch.delenv("AGENTRT_CONVERSATION_WORKTREE_ROOT", raising=False)
    monkeypatch.delenv("AGENTRT_WORKTREE_ROOT", raising=False)
    env = daemon._daemon_env("token")
    assert env["AGENTRT_CONVERSATION_WORKTREE_ROOT"] == str(
        config.state_dir() / "worktrees"
    )


def test_process_alive_never_uses_os_kill_on_windows(monkeypatch):
    """On Windows os.kill(pid, 0) is TerminateProcess, not a probe."""

    def kill(*args):
        raise AssertionError("os.kill would terminate the process on Windows")

    monkeypatch.setattr(daemon.os, "name", "nt")
    monkeypatch.setattr(daemon.os, "kill", kill)
    monkeypatch.setattr(daemon, "_windows_process_alive", lambda pid: True)
    assert daemon._process_alive(1234) is True


def test_ensure_running_refuses_a_second_daemon_without_discovery(monkeypatch):
    info = daemon.DaemonInfo(port=4321, token="token", pid=os.getpid())
    daemon._write_daemon_file(info)
    monkeypatch.setattr(daemon.sys, "platform", "win32")
    # Wedged: does not answer even given the busy allowance.
    monkeypatch.setattr(daemon, "is_alive", lambda info, timeout=2.0: False)
    monkeypatch.setattr(daemon, "_process_alive", lambda pid: True)
    with pytest.raises(RuntimeError, match="running but not answering"):
        daemon.ensure_running(startup_timeout=0.1)


def test_worktree_root_can_be_moved_to_another_drive(monkeypatch, tmp_path):
    """#12: the default lives in the state dir, but an operator can place
    worktrees elsewhere, by either name."""
    monkeypatch.delenv("AGENTRT_CONVERSATION_WORKTREE_ROOT", raising=False)
    monkeypatch.setenv("AGENTRT_WORKTREE_ROOT", str(tmp_path / "e-drive"))
    assert daemon._daemon_env("t")["AGENTRT_CONVERSATION_WORKTREE_ROOT"] == str(
        tmp_path / "e-drive"
    )
    monkeypatch.setenv("AGENTRT_CONVERSATION_WORKTREE_ROOT", str(tmp_path / "srv"))
    assert daemon._daemon_env("t")["AGENTRT_CONVERSATION_WORKTREE_ROOT"] == str(
        tmp_path / "srv"
    )


def test_status_tells_a_slow_daemon_from_a_dead_one(monkeypatch):
    """#13: a live process that fails its health check is `running` but not
    `responding`; only a missing process is `running: false`."""
    live = daemon.DaemonInfo(port=4321, token="token", pid=os.getpid())
    daemon._write_daemon_file(live)
    monkeypatch.setattr(daemon, "is_alive", lambda info: False)

    slow = daemon.status()
    assert (slow["running"], slow["responding"], slow["pid"]) == (
        True,
        False,
        os.getpid(),
    )

    monkeypatch.setattr(daemon, "_process_alive", lambda pid: False)
    gone = daemon.status()
    assert (gone["running"], gone["responding"], gone["pid"]) == (False, False, None)


@pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="identity check is /proc based"
)
def test_stop_does_not_signal_a_process_that_is_not_the_daemon():
    """daemon.json outlives a daemon that died uncleanly; its pid may be reused."""
    stranger = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        daemon._write_daemon_file(
            daemon.DaemonInfo(port=1234, token="token", pid=stranger.pid)
        )

        assert daemon.stop(timeout=0.1) is False

        assert stranger.poll() is None  # still running
        assert not config.daemon_file().exists()
    finally:
        stranger.kill()


def test_start_lock_is_not_taken_from_a_start_still_within_its_allowance(tmp_path):
    lock = tmp_path / "daemon.lock"
    lock.write_text(str(os.getpid()))  # a live owner
    aged = time.time() - 200  # past the old 120 s rule, inside the 240 s allowance
    os.utime(lock, (aged, aged))
    assert daemon._lock_is_stale(lock) is False

    long_ago = time.time() - 400
    os.utime(lock, (long_ago, long_ago))
    assert daemon._lock_is_stale(lock) is True


def test_start_lock_of_a_dead_owner_is_reclaimed_at_once(tmp_path):
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    lock = tmp_path / "daemon.lock"
    lock.write_text(str(gone.pid))

    assert daemon._lock_is_stale(lock) is True


def test_a_running_daemon_is_returned_without_the_start_lock(monkeypatch, tmp_path):
    """Concurrent CLI calls must not queue behind the daemon lock (#19).

    The lock exists so two processes cannot start two daemons; it was taken on
    every call, so an 8-worker dispatch pool got "Another agentrt CLI/MCP
    process is starting or holding the daemon lock" for most of its calls.
    """
    monkeypatch.setenv("AGENTRT_STATE_DIR", str(tmp_path))
    live = daemon.DaemonInfo(port=4321, token="live-token", pid=os.getpid())
    monkeypatch.setattr(daemon, "read_info", lambda: live)
    monkeypatch.setattr(daemon, "is_alive", lambda info: True)

    # Someone else holds the start lock; a call must not care.
    lock_path = tmp_path / "daemon.lock"
    lock_path.write_text(str(os.getpid()))

    assert daemon.ensure_running() == live
    assert lock_path.exists(), "the other process's lock was disturbed"


def test_a_start_in_progress_is_adopted_as_soon_as_it_answers(monkeypatch, tmp_path):
    """The callers that lost the start-lock race adopt the winner's daemon."""
    monkeypatch.setenv("AGENTRT_STATE_DIR", str(tmp_path))
    live = daemon.DaemonInfo(port=4322, token="started-token", pid=os.getpid())
    answers = iter([None, live, live, live])

    monkeypatch.setattr(daemon, "read_info", lambda: next(answers, live))
    monkeypatch.setattr(daemon, "is_alive", lambda info: True)
    # The lock is held by a live process for the whole call, as during a real
    # start -- a dead owner would look stale and take a different path.
    (tmp_path / "daemon.lock").write_text(str(os.getpid()))

    assert daemon.ensure_running() == live


def test_a_busy_daemon_is_not_declared_dead(monkeypatch, tmp_path):
    """A loaded daemon answers /health in seconds; that is busy, not dead (#22).

    The 2 s probe failed callers with "running but not answering its health
    check" while the daemon was merely slow, which is how the Windows client's
    dispatches started failing during the freeze.
    """
    monkeypatch.setenv("AGENTRT_STATE_DIR", str(tmp_path))
    busy = daemon.DaemonInfo(port=4323, token="busy", pid=os.getpid())
    probes: list[float] = []

    def slow_health(info, timeout=2.0):
        probes.append(timeout)
        return timeout >= daemon.BUSY_HEALTH_TIMEOUT

    monkeypatch.setattr(daemon, "read_info", lambda: busy)
    monkeypatch.setattr(daemon, "is_alive", slow_health)
    monkeypatch.setattr(daemon.sys, "platform", "win32")

    assert daemon.ensure_running() == busy
    assert probes[-1] == daemon.BUSY_HEALTH_TIMEOUT
