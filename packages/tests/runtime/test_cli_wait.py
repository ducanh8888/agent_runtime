"""H8 item 2: `agentrt wait` -- a backgroundable CLI substitute for holding
an MCP `wait_any`/`wait_all` call open, with a real exit code a caller can
check without parsing JSON.

`Client.wait` is patched directly rather than mocking the HTTP layer: this
file is about the CLI's argument parsing and exit-code mapping, which
`tests/runtime/test_h3_wait.py` already covers at the client level.
"""

from __future__ import annotations

import pytest

from agentrt.runtime import client as client_mod
from agentrt.runtime.cli import main


def _patch_wait(monkeypatch: pytest.MonkeyPatch, result: dict) -> list[tuple]:
    calls: list[tuple] = []

    def fake_wait(self, session_ids, *, mode="all", timeout=600.0, poll_interval=2.0):
        calls.append((session_ids, mode, timeout, poll_interval))
        return result

    monkeypatch.setattr(client_mod.Client, "wait", fake_wait)
    return calls


def test_wait_exits_zero_when_settled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _patch_wait(
        monkeypatch,
        {"completed": [{"id": "a"}], "still_running": [], "timed_out": False},
    )

    code = main(["wait", "a"])

    assert code == 0
    assert calls == [(["a"], "all", 600.0, 2.0)]


def test_wait_exits_three_on_timeout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """3 is distinct from 0 (settled) and 1/2 (the call itself failed) --
    the point of a real exit code is telling "still running" from either of
    those without parsing stdout."""
    _patch_wait(
        monkeypatch,
        {"completed": [], "still_running": [{"id": "a"}], "timed_out": True},
    )

    code = main(["wait", "a", "--timeout", "5"])

    assert code == 3


def test_wait_accepts_multiple_sessions_and_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    calls = _patch_wait(
        monkeypatch,
        {"completed": [{"id": "a"}], "still_running": [], "timed_out": False},
    )

    code = main(["wait", "a", "b", "c", "--mode", "any", "--poll-interval", "0.5"])

    assert code == 0
    assert calls == [(["a", "b", "c"], "any", 600.0, 0.5)]


def test_wait_prints_the_result_as_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_wait(
        monkeypatch,
        {"completed": [{"id": "a"}], "still_running": [], "timed_out": False},
    )

    main(["wait", "a"])

    out = capsys.readouterr().out
    assert '"timed_out": false' in out
