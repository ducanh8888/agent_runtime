"""Focused tests for terminal pool initialization and recovery."""

import tempfile

from libtmux.exc import LibTmuxException

from agentrt.tools.terminal.definition import TerminalAction
from agentrt.tools.terminal.impl import TerminalExecutor
from agentrt.tools.terminal.terminal.tmux_pane_pool import TmuxPanePool


def test_failed_pool_initialize_does_not_leave_pool_and_next_call_recovers(monkeypatch):
    with tempfile.TemporaryDirectory() as work_dir:
        executor = TerminalExecutor(working_dir=work_dir, terminal_type="tmux")
        executor.close()
        executor._pool = None
        executor._session = None

        original_initialize = TmuxPanePool.initialize
        attempts = 0

        def initialize_once_then_succeed(pool):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("transient initialization failure")
            return original_initialize(pool)

        monkeypatch.setattr(TmuxPanePool, "initialize", initialize_once_then_succeed)

        try:
            executor._initialize_pool()
        except RuntimeError:
            pass
        else:
            raise AssertionError("expected the first pool initialization to fail")

        assert executor._pool is None

        observation = executor(TerminalAction(command="echo recovered", timeout=5))
        assert not observation.is_error
        assert "recovered" in observation.text
        executor.close()


def test_runtime_error_for_unusable_pool_is_recoverable():
    error = RuntimeError("TmuxPanePool is not initialized or already closed")
    assert TerminalExecutor._is_recoverable_tmux_pool_error(error)


def test_no_such_session_is_recoverable():
    error = LibTmuxException("no such session: $0")
    assert TerminalExecutor._is_recoverable_tmux_pool_error(error)
