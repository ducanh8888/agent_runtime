"""AgentRT #13/#11: one unreachable MCP server must not fail the whole session."""

from __future__ import annotations

import pytest

from agentrt.sdk.mcp import config as mcp_config, utils


class _Client:
    def __init__(self, names: list[str]) -> None:
        self.names = names

    def sync_close(self) -> None:
        pass


def _servers(*names: str) -> dict[str, mcp_config.MCPServer]:
    return {n: mcp_config.MCPServer(url=f"http://{n}.invalid/mcp") for n in names}


@pytest.fixture
def attempts(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    """Fake connector: any attempt that includes the server "bad" times out."""
    seen: list[list[str]] = []

    def once(servers, timeout=30.0, **kwargs):
        seen.append(sorted(servers))
        if "bad" in servers:
            raise TimeoutError("bad did not answer")
        return _Client(sorted(servers))

    monkeypatch.setattr(utils, "_create_mcp_tools_once", once)
    monkeypatch.setattr(utils, "_dead_until", {})
    monkeypatch.setattr(utils, "_RETRY_PAUSE_SECONDS", 0)
    return seen


def test_unreachable_server_is_left_out_not_fatal(attempts) -> None:
    client = utils.create_mcp_tools(_servers("good", "bad"))

    assert client.names == ["good"]  # type: ignore[attr-defined]
    assert ["good"] in attempts and ["bad"] in attempts  # probed one by one


def test_a_dead_server_costs_one_wait_not_one_per_start(attempts) -> None:
    utils.create_mcp_tools(_servers("good", "bad"))
    attempts.clear()

    client = utils.create_mcp_tools(_servers("good", "bad"))

    assert client.names == ["good"]  # type: ignore[attr-defined]
    assert attempts == [["good"]]  # no repeat of the failing attempt


def test_a_server_moved_to_another_url_is_tried_again(attempts) -> None:
    utils.create_mcp_tools(_servers("good", "bad"))
    attempts.clear()
    moved = _servers("good", "bad")
    moved["bad"] = mcp_config.MCPServer(url="http://elsewhere.invalid/mcp")

    utils.create_mcp_tools(moved)

    assert ["bad", "good"] in attempts  # not skipped: the endpoint changed


def test_a_single_unreachable_server_still_raises(attempts) -> None:
    with pytest.raises(TimeoutError):
        utils.create_mcp_tools(_servers("bad"))


def test_all_unreachable_raises_the_original_error(attempts, monkeypatch) -> None:
    def always_fail(servers, timeout=30.0, **kwargs):
        raise TimeoutError("down")

    monkeypatch.setattr(utils, "_create_mcp_tools_once", always_fail)
    with pytest.raises(TimeoutError, match="down"):
        utils.create_mcp_tools(_servers("a", "b"))


def test_a_listing_that_fails_once_is_retried(attempts, monkeypatch) -> None:
    """#28: a dispatch failed on a listing that answered seconds later."""
    calls = 0

    def flaky(servers, timeout=30.0, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise TimeoutError("slow once")
        return _Client(sorted(servers))

    monkeypatch.setattr(utils, "_create_mcp_tools_once", flaky)
    client = utils.create_mcp_tools(_servers("research"))

    assert client.names == ["research"]  # type: ignore[attr-defined]
    assert calls == 2
