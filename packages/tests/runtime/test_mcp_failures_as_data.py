"""MCP tools return every failure as data (AGENTS.md invariant), in one shape."""

from __future__ import annotations

import pytest

from agentrt.runtime import client as client_mod, mcp_server


class _BrokenClient:
    def list_sessions(self, limit, offset, *, status=None, tag=None):
        raise client_mod.ClientError("daemon busy or not responding within 120 s")

    def status(self, session):
        raise ModuleNotFoundError("No module named 'agentrt.agent_server'")

    def tag(self, session, tags):
        raise AssertionError("an empty tag map must not reach the client")


@pytest.fixture
def broken(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(mcp_server, "_get_client", lambda: _BrokenClient())


def test_an_unexpected_exception_is_data_not_a_protocol_error(broken) -> None:
    result = mcp_server.status("abc")

    assert result["error"] == "ModuleNotFoundError"
    assert "agentrt.agent_server" in result["message"]


def test_list_failure_is_the_top_level_error_like_every_other_tool(broken) -> None:
    result = mcp_server.list_sessions()

    assert result == {
        "error": "ClientError",
        "message": "daemon busy or not responding within 120 s",
    }


def test_tag_with_no_key_value_pair_is_refused(broken) -> None:
    result = mcp_server.control("abc", "tag", message=",, ,")

    assert result["error"] == "MalformedTag"
