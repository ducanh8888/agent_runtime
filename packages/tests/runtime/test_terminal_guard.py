from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from agentrt.runtime import config, guarded_terminal
from agentrt.tools.terminal.definition import TerminalAction, TerminalObservation


class _RecordingInner:
    def __init__(self) -> None:
        self.action: TerminalAction | None = None
        self.reaped = 0
        self.interrupted = 0

    def reap(self) -> None:
        self.reaped += 1

    def interrupt(self) -> None:
        self.interrupted += 1

    def __call__(
        self, action: TerminalAction, conversation: Any = None
    ) -> TerminalObservation:
        self.action = action
        return TerminalObservation.from_text(
            text=("output state-key workspace-key backup-passphrase example-value"),
            command=action.command,
            timeout=True,
        )


def test_terminal_masks_config_and_workspace_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    state_env = tmp_path / "state.env"
    state_env.write_text("STATE_KEY=state-key\n", encoding="utf-8")
    monkeypatch.setattr(config, "config_file", lambda: state_env)

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".env.local").write_text(
        "WORKSPACE_KEY=workspace-key\n", encoding="utf-8"
    )
    (workspace / ".env.example").write_text("EXAMPLE=example-value\n", encoding="utf-8")
    (workspace / "backup-passphrase.txt").write_text(
        "BACKUP=backup-passphrase\n", encoding="utf-8"
    )
    inner = _RecordingInner()
    executor = guarded_terminal.GuardedTerminalExecutor(inner, str(workspace))

    result = executor(TerminalAction(command="printenv"))

    assert "state-key" not in result.text
    assert "workspace-key" not in result.text
    assert "backup-passphrase" not in result.text
    assert "<redacted>" in result.text
    assert "example-value" in result.text


def test_terminal_injects_default_hard_timeout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("AGENTRT_TERMINAL_MAX_SECONDS", raising=False)
    inner = _RecordingInner()
    executor = guarded_terminal.GuardedTerminalExecutor(inner, str(tmp_path))

    result = executor(TerminalAction(command="sleep 3600"))

    assert inner.action is not None
    assert inner.action.timeout == 1800
    assert "Stopped by AgentRT's 1800-second terminal limit" in result.text


def test_terminal_preserves_explicit_timeout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("AGENTRT_TERMINAL_MAX_SECONDS", "12")
    inner = _RecordingInner()
    executor = guarded_terminal.GuardedTerminalExecutor(inner, str(tmp_path))

    executor(TerminalAction(command="sleep 30", timeout=5))

    assert inner.action is not None
    assert inner.action.timeout == 5


def test_terminal_caps_a_longer_model_timeout(tmp_path: Path, monkeypatch) -> None:
    """#15: the limit is a maximum. A model asking for more than it got no limit,
    and an unclosed quote then held the session for as long as it asked."""
    monkeypatch.setenv("AGENTRT_TERMINAL_MAX_SECONDS", "12")
    inner = _RecordingInner()
    executor = guarded_terminal.GuardedTerminalExecutor(inner, str(tmp_path))

    result = executor(TerminalAction(command="make test", timeout=7200))

    assert inner.action is not None
    assert inner.action.timeout == 12
    assert "Stopped by AgentRT's 12-second terminal limit" in result.text


def test_guard_delegates_reap_to_the_wrapped_executor(tmp_path: Path) -> None:
    """#17: the daemon wraps the terminal, so `reap` must reach the real one.

    A run's end reaps the terminal; on the daemon path the tool's executor is
    this guard, and a guard that swallowed `reap` would leave every session's
    PowerShell alive exactly as before the fix.
    """
    inner = _RecordingInner()
    executor = guarded_terminal.GuardedTerminalExecutor(inner, str(tmp_path))

    executor.reap()

    assert inner.reaped == 1


def test_guard_reap_is_a_noop_for_an_inner_without_it(tmp_path: Path) -> None:
    class _PlainInner:
        def __call__(self, action: Any, conversation: Any = None):
            return TerminalObservation.from_text("ok", command="x")

    executor = guarded_terminal.GuardedTerminalExecutor(_PlainInner(), str(tmp_path))

    executor.reap()  # must not raise


def test_guard_stops_waiting_when_the_backend_never_returns(
    tmp_path: Path, monkeypatch
) -> None:
    """A stuck backend must not hold the run past its deadline (#15).

    The client's sessions sat `running` with no events for hours because the
    wall-clock limit lives inside the session loop, which is what is stuck when
    a shell exits without a prompt marker. The guard now waits out here.
    """
    import threading

    monkeypatch.setenv("AGENTRT_TERMINAL_MAX_SECONDS", "1")
    monkeypatch.setattr(guarded_terminal, "_DEADLINE_GRACE_SECONDS", 0.2)
    monkeypatch.setattr(guarded_terminal, "_INTERRUPT_GRACE_SECONDS", 0.2)

    released = threading.Event()

    class _StuckInner:
        interrupted = 0
        reaped = 0

        def __call__(self, action: Any, conversation: Any = None):
            released.wait(30)
            return TerminalObservation.from_text("late", command="late")

        def interrupt(self) -> None:
            type(self).interrupted += 1

        def reap(self) -> None:
            type(self).reaped += 1

    executor = guarded_terminal.GuardedTerminalExecutor(_StuckInner(), str(tmp_path))
    start = time.monotonic()

    result = executor(TerminalAction(command="sleep forever"))

    elapsed = time.monotonic() - start
    assert elapsed < 10, f"the call waited {elapsed:.1f}s past its deadline"
    assert result.is_error is True
    assert "did not return within" in result.text
    assert _StuckInner.interrupted == 1, "the stuck command was not interrupted"
    assert _StuckInner.reaped == 1, "the shell was not reset for the next command"
    released.set()


def test_guard_delegates_interrupt_to_the_wrapped_executor(tmp_path: Path) -> None:
    """ParallelToolExecutor interrupts the tool's executor -- which is the guard.

    Without the delegation the terminal command kept running when a session was
    interrupted, so `interrupt`/`delete` could not take the conversation back
    (#15).
    """
    inner = _RecordingInner()
    executor = guarded_terminal.GuardedTerminalExecutor(inner, str(tmp_path))

    executor.interrupt()

    assert inner.interrupted == 1
