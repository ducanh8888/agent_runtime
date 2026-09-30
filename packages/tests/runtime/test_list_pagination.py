"""Tests for list pagination in Client, CLI, and MCP server."""

from __future__ import annotations

import types
from collections.abc import Callable

import httpx
import pytest

from agentrt.runtime import client as client_mod, mcp_server
from agentrt.runtime.cli import main


Handler = Callable[[httpx.Request], httpx.Response]


def _mock_client(handler: Handler) -> client_mod.Client:
    client = client_mod.Client()
    setattr(
        client,
        "_daemon_info",
        types.SimpleNamespace(base_url="http://daemon.test", token="test-token"),
    )
    client._profile_ref = {}
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_list_sessions_default_fetches_single_page_without_walking_all() -> None:
    """Default limit=50, offset=0 makes only one request to the search endpoint."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        # Even if next_page_id exists, caller requested 50 and received 50
        items = [
            {"id": f"00000000-0000-0000-0000-{i:012d}", "title": f"session {i}"}
            for i in range(50)
        ]
        return httpx.Response(
            200, json={"items": items, "next_page_id": "next-page-token"}
        )

    client = _mock_client(handler)
    res = client.list_sessions(limit=50, offset=0)

    assert len(res) == 50
    assert len(requests) == 1
    assert requests[0].url.params["limit"] == "50"
    assert "page_id" not in requests[0].url.params


def test_list_sessions_with_offset_pages_forward() -> None:
    """list_sessions with offset pages forward and slices properly."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        page_id = request.url.params.get("page_id")
        if not page_id:
            items = [
                {"id": f"00000000-0000-0000-0000-{i:012d}", "title": f"session {i}"}
                for i in range(100)
            ]
            return httpx.Response(200, json={"items": items, "next_page_id": "p2"})
        else:
            items = [
                {"id": f"00000000-0000-0000-0000-{i:012d}", "title": f"session {i}"}
                for i in range(100, 200)
            ]
            return httpx.Response(200, json={"items": items, "next_page_id": None})

    client = _mock_client(handler)
    # offset 80, limit 30 -> needs items 80..110 (spans p1 and p2)
    res = client.list_sessions(limit=30, offset=80)

    assert len(res) == 30
    assert res[0]["title"] == "session 80"
    assert res[-1]["title"] == "session 109"
    assert len(requests) == 2


def test_list_sessions_zero_limit_makes_no_requests() -> None:
    """limit=0 returns empty list without calling the daemon."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"items": []})

    client = _mock_client(handler)
    res = client.list_sessions(limit=0)
    assert res == []
    assert len(requests) == 0


def test_cli_list_forwards_limit_and_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CLI list defaults to limit 50, offset 0 and forwards custom flags."""
    calls: list[tuple[int, int]] = []

    class FakeClient:
        def list_sessions(self, limit: int = 50, offset: int = 0) -> list:
            calls.append((limit, offset))
            return []

    monkeypatch.setattr(client_mod, "Client", lambda: FakeClient())

    code = main(["list"])
    assert code == 0
    assert calls[-1] == (50, 0)

    code = main(["list", "--limit", "10", "--offset", "20"])
    assert code == 0
    assert calls[-1] == (10, 20)


def test_mcp_list_sessions_forwards_limit_and_offset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MCP list_sessions tool forwards limit and offset."""
    calls: list[tuple[int, int]] = []

    class FakeClient:
        def list_sessions(self, limit: int = 50, offset: int = 0) -> list:
            calls.append((limit, offset))
            return [{"id": "s1"}]

    monkeypatch.setattr(mcp_server, "_get_client", lambda: FakeClient())

    res = mcp_server.list_sessions()
    assert calls[-1] == (50, 0)
    assert res == {"sessions": [{"id": "s1"}]}

    mcp_server.list_sessions(limit=15, offset=30)
    assert calls[-1] == (15, 30)
