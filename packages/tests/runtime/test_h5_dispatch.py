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
