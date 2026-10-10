"""wait(): what counts as an outcome, and what counts as missing."""

from __future__ import annotations

import time

from agentrt.runtime import client as client_mod


def _client(monkeypatch, statuses, result) -> client_mod.Client:
    client = client_mod.Client()
    monkeypatch.setattr(client, "_wait_status", statuses)
    monkeypatch.setattr(client, "result", result)
    monkeypatch.setattr(client_mod, "_is_settled", lambda status: status["settled"])
    return client


def test_wait_any_returns_when_an_id_is_missing(monkeypatch) -> None:
    """`any` waited out the running sessions although a missing id is already an
    outcome (it is reported in the `missing` bucket)."""

    def statuses(session: str):
        return None if session == "gone" else {"id": session, "settled": False}

    client = _client(monkeypatch, statuses, lambda s: {})

    started = time.monotonic()
    out = client.wait(["gone", "busy"], mode="any", timeout=4.0, poll_interval=0.5)

    assert time.monotonic() - started < 2.0
    assert [item["id"] for item in out["missing"]] == ["gone"]
    assert [item["id"] for item in out["still_running"]] == ["busy"]


def test_unreadable_result_of_a_live_session_is_not_missing(monkeypatch) -> None:
    """A daemon timeout while reading a settled session's result is a
    ClientError too; it was reported as the session being deleted."""

    def statuses(session: str):
        return {"id": session, "settled": True}

    def result(session: str):
        raise client_mod.ClientError("daemon busy or not responding within 120 s")

    out = _client(monkeypatch, statuses, result).wait(
        ["done"], mode="all", timeout=5.0, poll_interval=0.5
    )

    assert out["missing"] == []
    (item,) = out["still_running"]
    assert item["id"] == "done"
    assert "result read failed" in item["error"]


def test_result_of_a_session_deleted_meanwhile_is_missing(monkeypatch) -> None:
    calls = {"n": 0}

    def statuses(session: str):
        calls["n"] += 1
        return {"id": session, "settled": True} if calls["n"] <= 2 else None

    def result(session: str):
        raise client_mod.ClientError("not found")

    out = _client(monkeypatch, statuses, result).wait(
        ["done"], mode="all", timeout=5.0, poll_interval=0.5
    )

    assert [item["id"] for item in out["missing"]] == ["done"]
    assert out["still_running"] == []
