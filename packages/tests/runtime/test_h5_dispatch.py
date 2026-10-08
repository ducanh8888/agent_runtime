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


def _mock_client(handler: Handler, *, stub_search: bool = True) -> client_mod.Client:
    client = client_mod.Client()
    setattr(
        client,
        "_daemon_info",
        types.SimpleNamespace(base_url="http://daemon.test", token="test-token"),
    )
    client._profile_ref = {}

    def route(request: httpx.Request) -> httpx.Response:
        if stub_search and request.url.path == "/api/conversations/search":
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


def test_dispatch_many_applies_defaults_and_rejects_unknown_arguments() -> None:
    bodies: list[dict] = []

    def handle(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json=_created_body(request))

    client = _mock_client(handle)

    with pytest.raises(ValueError, match="unknown argument"):
        client.dispatch_many([{"task": "t", "workspace": "/tmp/w", "wrokspace": 1}])
    assert bodies == []

    result = client.dispatch_many(
        [
            {"task": "one", "tags": {"lane": "a"}},
            {"task": "two", "workspace": "/tmp/other", "tags": {"lane": "b"}},
        ],
        defaults={"workspace": "/tmp/w", "tags": {"batch": "x"}},
    )

    assert result["count"] == 2
    tags = sorted((body["tags"]["batch"], body["tags"]["lane"]) for body in bodies)
    assert tags == [("x", "a"), ("x", "b")]
    workspaces = sorted(body["workspace"]["working_dir"] for body in bodies)
    assert workspaces == ["/tmp/other", "/tmp/w"]


def test_dispatch_many_submits_items_concurrently() -> None:
    import threading

    active = {"now": 0, "peak": 0}
    lock = threading.Lock()
    barrier = threading.Barrier(3, timeout=5)

    def handle(request: httpx.Request) -> httpx.Response:
        with lock:
            active["now"] += 1
            active["peak"] = max(active["peak"], active["now"])
        barrier.wait()
        with lock:
            active["now"] -= 1
        return httpx.Response(201, json=_created_body(request))

    client = _mock_client(handle)
    result = client.dispatch_many(
        [{"task": f"t{i}", "workspace": f"/tmp/w{i}"} for i in range(3)]
    )

    assert result["count"] == 3
    assert [item["index"] for item in result["accepted"]] == [0, 1, 2]
    assert active["peak"] == 3


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

    client = _mock_client(handle, stub_search=False)
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


def test_dispatch_refuses_a_workspace_that_does_not_exist(tmp_path) -> None:
    """#3: a missing path used to be created and then git-initialised, so a
    typo became a session reviewing an empty repository."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201, json=_created_body(request))

    missing = tmp_path / "not-yet-created"
    with pytest.raises(client_mod.ClientError, match="does not exist"):
        _mock_client(handler).dispatch("task", str(missing))
    assert not missing.exists()
    assert not [c for c in calls if c.method == "POST"]


def test_dispatch_creates_the_workspace_only_when_asked(tmp_path) -> None:
    client = _mock_client(
        lambda request: httpx.Response(201, json=_created_body(request))
    )
    target = tmp_path / "fresh"

    client.dispatch("task", str(target), create_workspace=True)

    assert target.is_dir()
    with pytest.raises(client_mod.ClientError, match="does not exist"):
        client.dispatch(
            "task",
            str(tmp_path / "pinned"),
            workspace_mode="snapshot",
            create_workspace=True,
        )


def test_dispatch_many_refuses_a_missing_workspace_before_any_item(tmp_path) -> None:
    """#3: the check runs in the up-front pass, so a typo in one item does
    not leave the earlier items created."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(201, json=_created_body(request))

    tasks = [
        {"task": "a", "workspace": str(tmp_path)},
        {"task": "b", "workspace": str(tmp_path / "typo")},
    ]
    with pytest.raises(ValueError, match="item 1 workspace .* does not exist"):
        _mock_client(handler).dispatch_many(tasks)
    assert not [c for c in calls if c.method == "POST"]
