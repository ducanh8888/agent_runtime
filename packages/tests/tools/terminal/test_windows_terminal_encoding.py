"""AgentRT fork: the PowerShell session is switched to UTF-8 before use.

Runs on any OS: the PowerShell process is faked.
"""

from __future__ import annotations

import io

from agentrt.tools.terminal.terminal import windows_terminal


class _FakeProcess:
    pid = 4242

    def __init__(self, *args, **kwargs) -> None:
        self.stdin = io.BytesIO()
        self.stdout = io.BytesIO()

    def poll(self) -> None:
        return None


def test_first_input_switches_powershell_to_utf8(tmp_path, monkeypatch) -> None:
    started: list[_FakeProcess] = []

    def popen(*args, **kwargs) -> _FakeProcess:
        started.append(_FakeProcess())
        return started[-1]

    monkeypatch.setattr(windows_terminal.subprocess, "Popen", popen)
    terminal = windows_terminal.WindowsTerminal(str(tmp_path))
    monkeypatch.setattr(terminal, "_wait_for_startup_output", lambda: None)
    monkeypatch.setattr(terminal, "clear_screen", lambda: None)

    terminal.initialize()

    written = started[0].stdin.getvalue().decode("utf-8")
    assert written.startswith("$__utf8 = New-Object System.Text.UTF8Encoding $false;")
    assert "[Console]::OutputEncoding = $__utf8" in written
    assert "[Console]::InputEncoding = $__utf8" in written
