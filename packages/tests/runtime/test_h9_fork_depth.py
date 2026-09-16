"""H9 item 5: a fork chain is bounded.

The plan recorded why this was not built earlier -- no permission preset grants
a tool that can call `dispatch_from`, so there was no live nesting to cap. What
it is for is the orchestrator calling `dispatch_from` in a loop: each hop looks
cheap and the cost of the whole chain lands at once, later. Depth counts
ancestors, so a directly-dispatched session is 0 and its fork is 1.
"""

from __future__ import annotations

import json
import types

import httpx
import pytest

from agentrt.runtime import client as client_mod, config


ROOT = "11111111-1111-1111-1111-111111111111"
CHILD = "22222222-2222-2222-2222-222222222222"
GRANDCHILD = "33333333-3333-3333-3333-333333333333"
FORKED = "77777777-7777-7777-7777-777777777777"


def _client(parents: dict[str, str | None]) -> tuple[client_mod.Client, dict]:
    """A client whose conversations declare the given parent lineage."""
    seen: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/fork"):
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "id": FORKED,
                    "execution_status": "idle",
                    "forked_from_event_id": seen["body"].get("from_event_id"),
                },
            )
        if path.endswith("/events"):
            return httpx.Response(200, json={"success": True})
        conversation = path.rsplit("/", 1)[-1]
        if conversation == FORKED:
            # The fork this client just asked for; `send` reads its status.
            return httpx.Response(
                200,
                json={
                    "id": FORKED,
                    "execution_status": "idle",
                    "tags": {},
                    "workspace": {"working_dir": "/tmp"},
                    "parent_conversation_id": None,
                },
            )
        if conversation not in parents:
            return httpx.Response(404, json={"detail": "not found"})
        return httpx.Response(
            200,
            json={
                "id": conversation,
                "execution_status": "idle",
                "tags": {},
                "workspace": {"working_dir": "/tmp"},
                "parent_conversation_id": parents[conversation],
            },
        )

    client = client_mod.Client()
    setattr(
        client,
        "_daemon_info",
        types.SimpleNamespace(base_url="http://daemon.test", token="test-token"),
    )
    client._profile_ref = {}
    client._http = httpx.Client(transport=httpx.MockTransport(handle))
    client._profile_id = lambda preset, llm_profile: (
        "00000000-0000-0000-0000-000000000001"
    )
    return client, seen


@pytest.fixture(autouse=True)
def _default_cap(monkeypatch):
    monkeypatch.setattr(config, "max_running_sessions", lambda: 0)
    monkeypatch.delenv("AGENTRT_MAX_FORK_DEPTH", raising=False)


def test_a_root_session_is_depth_zero() -> None:
    client, _ = _client({ROOT: None})

    assert client._fork_depth(ROOT) == 0


def test_depth_counts_the_whole_chain() -> None:
    client, _ = _client({ROOT: None, CHILD: ROOT, GRANDCHILD: CHILD})

    assert client._fork_depth(CHILD) == 1
    assert client._fork_depth(GRANDCHILD) == 2


def test_status_reports_the_parent_so_the_chain_is_visible() -> None:
    """Without this field the cap could not be computed at all."""
    client, _ = _client({ROOT: None, CHILD: ROOT})

    assert client.status(CHILD)["parent_conversation_id"] == ROOT
    assert client.status(ROOT)["parent_conversation_id"] is None


def test_a_gone_ancestor_ends_the_walk_instead_of_failing() -> None:
    """A deleted source must not turn into a dispatch failure."""
    client, _ = _client({CHILD: ROOT})  # ROOT itself is unknown -> 404

    assert client._fork_depth(CHILD) == 1


def test_a_cyclic_chain_terminates() -> None:
    """A malformed chain must not loop: this runs before a refusal decision."""
    client, _ = _client({CHILD: ROOT, ROOT: CHILD})

    assert client._fork_depth(CHILD) == 2


@pytest.mark.parametrize(
    "source,depth,allowed",
    [
        # Forking at 2 deep yields a 3rd generation, which the default of 3
        # permits; forking at 3 deep would yield a 4th and is refused.
        (GRANDCHILD, 2, True),
        ("44444444-4444-4444-4444-444444444444", 3, False),
    ],
    ids=["at-the-limit", "past-the-limit"],
)
def test_the_cap_refuses_the_fork_past_the_limit(source, depth, allowed) -> None:
    parents: dict[str, str | None] = {
        ROOT: None,
        CHILD: ROOT,
        GRANDCHILD: CHILD,
        "44444444-4444-4444-4444-444444444444": GRANDCHILD,
    }
    assert _client(parents)[0]._fork_depth(source) == depth

    client, seen = _client(parents)
    if allowed:
        client.dispatch_from(source, "review")
        assert "body" in seen
    else:
        with pytest.raises(client_mod.ClientError, match="fork depth limit"):
            client.dispatch_from(source, "review")
        # Refused before the fork request, so nothing is left to clean up.
        assert "body" not in seen


def test_zero_disables_the_bound(monkeypatch) -> None:
    monkeypatch.setenv("AGENTRT_MAX_FORK_DEPTH", "0")
    parents = {ROOT: None, CHILD: ROOT, GRANDCHILD: CHILD}
    client, seen = _client(parents)

    client.dispatch_from(GRANDCHILD, "review")

    assert "body" in seen


def test_an_unset_or_unparsable_value_keeps_the_default(monkeypatch) -> None:
    monkeypatch.setenv("AGENTRT_MAX_FORK_DEPTH", "not-a-number")
    assert config.max_fork_depth() == config.DEFAULT_MAX_FORK_DEPTH

    monkeypatch.delenv("AGENTRT_MAX_FORK_DEPTH")
    assert config.max_fork_depth() == 3


def test_a_deeper_chain_is_refused_by_a_lower_configured_limit(monkeypatch) -> None:
    monkeypatch.setenv("AGENTRT_MAX_FORK_DEPTH", "1")
    client, seen = _client({ROOT: None, CHILD: ROOT})

    with pytest.raises(client_mod.ClientError, match="AGENTRT_MAX_FORK_DEPTH is 1"):
        client.dispatch_from(CHILD, "review")

    assert "body" not in seen
