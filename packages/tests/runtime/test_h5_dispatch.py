"""H5: the runtime side of dispatch-many, capacity and submission keys."""

from __future__ import annotations

import json
import types
from collections.abc import Callable

import httpx
import pytest

from agentrt.runtime import bootstrap, client as client_mod, config


@pytest.fixture(autouse=True)
def _no_client_side_cap(monkeypatch):
    monkeypatch.setattr(config, "max_running_sessions", lambda: 0)
    monkeypatch.setattr(
        bootstrap, "agent_profile_llm_ref", lambda preset: "deepseek-high"
    )


Handler = Callable[[httpx.Request], httpx.Response]
CREATED = "99999999-9999-9999-9999-999999999999"


def _mock_client(handler: Handler) -> client_mod.Client:
    client = client_mod.Client()
    setattr(
        client,
        "_daemon_info",
        types.SimpleNamespace(base_url="http://daemon.test", token="test-token"),
    )
    client._profile_ref = {}
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    # Dispatch resolves a permission to a configured profile and consults a
    # client-side cap; neither belongs to the wire shape under test.
    client._profile_id = lambda preset, llm_profile: (
        "00000000-0000-0000-0000-000000000001"
    )
    return client


def _created_body(request: httpx.Request) -> dict:
    body = json.loads(request.content)
    return {
        "id": CREATED,
        "execution_status": "idle",
        "tags": body.get("tags", {}),
        "admission_state": "queued",
    }


def test_capacity_reports_the_surface() -> None:
    client = _mock_client(
        lambda request: httpx.Response(
            200,
            json={
                "limiting_dimension": "runs",
                "limit": 3,
                "running": 3,
                "queued": 2,
                "available": 0,
            },
        )
    )

    surface = client.capacity()

    assert surface["available"] == 0
    assert surface["queued"] == 2
    assert surface["limiting_dimension"] == "runs"


def test_dispatch_sends_the_idempotency_key() -> None:
    seen: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=_created_body(request))

    client = _mock_client(handle)
    client.dispatch("task", "/tmp/ws", idempotency_key="batch-1:0")

    assert seen["body"]["idempotency_key"] == "batch-1:0"


def test_dispatch_omits_workspace_mode_by_default() -> None:
    """H8 item 8: the default (shared) mode is not sent as an explicit
    value -- absence, not the string "shared", is what "default" means on
    the wire, matching every other optional dispatch field."""
    seen: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=_created_body(request))

    client = _mock_client(handle)
    client.dispatch("task", "/tmp/ws")

    assert "workspace_mode" not in seen["body"]


def test_dispatch_sends_snapshot_workspace_mode() -> None:
    seen: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=_created_body(request))

    client = _mock_client(handle)
    client.dispatch("task", "/tmp/ws", workspace_mode="snapshot")

    assert seen["body"]["workspace_mode"] == "snapshot"


def test_dispatch_surfaces_the_pinned_commit() -> None:
    """H8 item 8: a snapshot session's pinned SHA comes back on dispatch's
    own response, not only from a separate status call."""

    def handle(request: httpx.Request) -> httpx.Response:
        body = _created_body(request)
        body["workspace_mode"] = "snapshot"
        body["workspace_resolved_sha"] = "abc1234"
        return httpx.Response(201, json=body)

    client = _mock_client(handle)
    created = client.dispatch("task", "/tmp/ws", workspace_mode="snapshot")

    assert created["workspace_mode"] == "snapshot"
    assert created["workspace_resolved_sha"] == "abc1234"


def test_dispatch_response_omits_pinned_commit_for_shared_mode() -> None:
    client = _mock_client(
        lambda request: httpx.Response(201, json=_created_body(request))
    )
    created = client.dispatch("task", "/tmp/ws")

    assert "workspace_resolved_sha" not in created
    assert "workspace_mode" not in created


def test_dispatch_many_validates_before_any_side_effect() -> None:
    calls = {"n": 0}

    def handle(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(201, json=_created_body(request))

    client = _mock_client(handle)

    with pytest.raises(ValueError, match="at least one task"):
        client.dispatch_many([])
    with pytest.raises(ValueError, match="no workspace"):
        client.dispatch_many([{"task": "one"}])
    with pytest.raises(ValueError, match="exceeds the batch size"):
        client.dispatch_many([{"task": "t", "workspace": "/tmp/w"}] * 3, max_batch=2)

    # Nothing was created by a rejected batch.
    assert calls["n"] == 0


def test_dispatch_many_reports_per_item_outcomes() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        text = body["initial_message"]["content"][0]["text"]
        if text == "bad":
            return httpx.Response(400, json={"detail": "refused"})
        return httpx.Response(201, json=_created_body(request))

    client = _mock_client(handle)

    result = client.dispatch_many(
        [
            {"task": "good one", "workspace": "/tmp/w"},
            {"task": "bad", "workspace": "/tmp/w"},
            {"task": "good two", "workspace": "/tmp/w"},
        ]
    )

    assert result["requested"] == 3
    assert [item["index"] for item in result["accepted"]] == [0, 2]
    assert [item["index"] for item in result["failed"]] == [1]
    assert result["failed"][0]["error"]


def test_dispatch_generates_a_key_and_retries_timeout_with_same_key() -> None:
    seen: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        key = json.loads(request.content)["idempotency_key"]
        seen.append(key)
        if len(seen) == 1:
            raise httpx.ReadTimeout("timed out", request=request)
        return httpx.Response(201, json=_created_body(request))

    client = _mock_client(handle)
    created = client.dispatch("task", "/tmp/ws")

    assert created["id"] == CREATED
    assert len(seen) == 2
    assert seen[0] == seen[1]
    assert seen[0]


def test_dispatch_timeout_error_names_method_path_and_duration() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = _mock_client(handle)
    client._timeout = 7.5

    with pytest.raises(
        client_mod.ClientError,
        match=r"daemon busy.*7.5 s.*POST /api/conversations",
    ):
        client.dispatch("task", "/tmp/ws")


def test_prefix_resolution_indexes_incrementally_and_detects_ambiguity() -> None:
    from uuid import UUID

    ids = [f"{i:08d}-0000-0000-0000-000000000000" for i in range(250)]
    requests: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        requests.append(params)
        page = params.get("page_id")
        offset = (
            0
            if page is None
            else next(
                index for index, value in enumerate(ids) if UUID(value).hex == page
            )
        )
        items = ids[offset : offset + 100]
        next_page = UUID(ids[offset + 100]).hex if offset + 100 < len(ids) else None
        return httpx.Response(
            200,
            json={
                "items": [{"id": value} for value in items],
                "next_page_id": next_page,
            },
        )

    client = _mock_client(handle)
    client._resolve_session("00000000")
    first_count = len(requests)
    assert first_count == 3

    ids[:0] = ["00000000-aaaa-aaaa-aaaa-aaaaaaaaaaaa"]
    with pytest.raises(client_mod.AmbiguousSession):
        client._resolve_session("00000000")
    assert len(requests) == first_count + 1
    assert all(params.get("sort_order") == "CREATED_AT_DESC" for params in requests)


def test_full_uuid_resolution_does_not_search() -> None:
    client = _mock_client(lambda request: pytest.fail("unexpected search"))
    assert client._resolve_session(CREATED) == CREATED
