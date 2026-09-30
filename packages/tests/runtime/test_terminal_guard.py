from __future__ import annotations

from pathlib import Path
from typing import Any

from agentrt.runtime import config, guarded_terminal
from agentrt.tools.terminal.definition import TerminalAction, TerminalObservation


class _RecordingInner:
    def __init__(self) -> None:
        self.action: TerminalAction | None = None

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
