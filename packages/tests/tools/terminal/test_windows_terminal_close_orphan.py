"""WindowsTerminal.close() must return when an orphaned process holds the pipe.

agent_runtime#22: a command that leaves a background process behind whose
parent already exited (``start /b``, a detached job) keeps PowerShell's
stdout pipe open. ``_terminate_child_processes`` cannot see it (its parent is
gone), so the reader thread stays blocked in ``stdout.read()``. The old
``close()`` then called ``stdout.close()`` before stopping PowerShell and
blocked forever on the reader's buffer lock -- on the daemon's event loop,
freezing the whole daemon.
"""

from __future__ import annotations

import platform
import subprocess
import threading
import time

import pytest

from agentrt.tools.terminal.terminal import windows_terminal

pytestmark = pytest.mark.skipif(platform.system() != "Windows",
                                reason="WindowsTerminal is Windows-only")


def _kill_orphans(marker: str) -> None:
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -match "
         f"'{marker}' }} | ForEach-Object {{ Stop-Process -Id $_.ProcessId -Force }}"],
        capture_output=True, timeout=60)


def test_close_returns_while_an_orphan_holds_the_pipe(tmp_path) -> None:
    marker = "agentrt-close-orphan-probe"
    terminal = windows_terminal.WindowsTerminal(str(tmp_path))
    terminal.initialize()
    try:
        # cmd starts ping in the background and exits at once: ping is
        # orphaned (its parent is gone) and inherits PowerShell's stdout.
        terminal.send_keys(f'cmd /c "start /b ping -n 60 127.0.0.1 > nul & '
                           f'rem {marker}"')
        time.sleep(3)
        done = threading.Event()
        worker = threading.Thread(target=lambda: (terminal.close(), done.set()),
                                  daemon=True)
        started = time.monotonic()
        worker.start()
        finished = done.wait(timeout=30)
        assert finished, "close() blocked while an orphan held the pipe"
        assert time.monotonic() - started < 30
        assert terminal.process is None
    finally:
        _kill_orphans("ping -n 60 127.0.0.1")
