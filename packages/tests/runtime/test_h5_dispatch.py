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

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/conversations/search":
            return httpx.Response(200, json={"items": []})
        return handler(request)

    client._http = httpx.Client(transport=httpx.MockTransport(route))
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


def test_dispatch_defaults_iteration_budget_and_preserves_explicit_value(
    monkeypatch, tmp_path
) -> None:
    bodies = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json=_created_body(request))

    client = _mock_client(handle)
    client.dispatch("task", str(tmp_path))
    client.dispatch("task", str(tmp_path), max_iterations=7)

    assert bodies[0]["max_iterations"] == 100_000
    assert bodies[1]["max_iterations"] == 7


def test_dispatch_prepends_context_files_and_sets_commit_hook(tmp_path) -> None:
    workspace = tmp_path / "repo"
    workspace.mkdir()
    import subprocess

    subprocess.run(["git", "init", str(workspace)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(workspace),
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@t",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
        check=True,
        capture_output=True,
    )
    context = tmp_path / "context.md"
    context.write_text("shared preamble", encoding="utf-8")
    seen = {}

    def handle(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(201, json=_created_body(request))

    _mock_client(handle).dispatch(
        "do the work", str(workspace), context_files=[str(context)], require="commit"
    )
    body = seen["body"]
    text = body["initial_message"]["content"][0]["text"]
    assert text == f"### Context file: {context}\nshared preamble\n\ndo the work"
    hook = body["hook_config"]["stop"][0]["hooks"][0]
    assert hook["type"] == "command"
    assert "agentrt.runtime.commit_hook" in hook["command"]


def test_context_file_limits_fail_before_dispatch(tmp_path) -> None:
    context = tmp_path / "large.txt"
    context.write_bytes(b"x" * (200 * 1024 + 1))
    client = _mock_client(
        lambda request: httpx.Response(201, json=_created_body(request))
    )

    with pytest.raises(client_mod.ClientError, match="per-file cap"):
        client.dispatch("task", str(tmp_path), context_files=[str(context)])
    with pytest.raises(client_mod.ClientError, match="does not exist"):
        client.dispatch(
            "task", str(tmp_path), context_files=[str(tmp_path / "missing")]
        )


def test_dispatch_warns_about_running_session_in_same_workspace(tmp_path) -> None:
    workspace = str(tmp_path)
    client = _mock_client(
        lambda request: httpx.Response(201, json=_created_body(request))
    )

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/conversations/search":
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "id": "12345678-1234-1234-1234-123456789012",
                            "execution_status": "running",
                            "workspace": {"working_dir": workspace},
                        }
                    ]
                },
            )
        return httpx.Response(201, json=_created_body(request))

    client._http = httpx.Client(transport=httpx.MockTransport(route))
    response = client.dispatch("task", workspace)

    assert response["shared_workspace_with"] == ["12345678"]
    assert response["warning"]


def test_default_max_iterations_environment_override(monkeypatch) -> None:
    monkeypatch.setenv("AGENTRT_DEFAULT_MAX_ITERATIONS", "1234")
    assert config.default_max_iterations() == 1234
