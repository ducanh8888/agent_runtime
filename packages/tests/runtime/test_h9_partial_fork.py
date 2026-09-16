"""H9 item 4: bounding how much history a fork inherits.

The server has supported ``from_event_id`` on ``POST /{id}/fork`` all along
(``conversation_router.fork_conversation`` -> ``fork_conversation`` ->
``BaseConversation.fork``), with tests of its own in
``tests/agent_server/test_conversation_service.py``. What never existed was a
way to *reach* it: ``Client.dispatch_from`` sent only ``title``/``tags``, so
every fork from an orchestrator copied the whole conversation. These cover the
wrapper, not the fork semantics -- which the server tests already own.
"""

from __future__ import annotations

import json
import types
from collections.abc import Callable

import httpx
import pytest

from agentrt.runtime import bootstrap, client as client_mod, config, mcp_server


SOURCE = "11111111-1111-1111-1111-111111111111"
CREATED = "77777777-7777-7777-7777-777777777777"
BRANCH_POINT = "evt-branch-point"
Handler = Callable[[httpx.Request], httpx.Response]


@pytest.fixture(autouse=True)
def _no_client_side_cap(monkeypatch):
    monkeypatch.setattr(config, "max_running_sessions", lambda: 0)
    monkeypatch.setattr(
        bootstrap, "agent_profile_llm_ref", lambda preset: "deepseek-high"
    )


def _mock_client(handler: Handler) -> client_mod.Client:
    client = client_mod.Client()
    setattr(
        client,
        "_daemon_info",
        types.SimpleNamespace(base_url="http://daemon.test", token="test-token"),
    )
    client._profile_ref = {}
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    client._profile_id = lambda preset, llm_profile: (
        "00000000-0000-0000-0000-000000000001"
    )
    return client


def _fork_client(seen: dict) -> client_mod.Client:
    """A client whose fork endpoint records the body it was sent."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/fork"):
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "id": CREATED,
                    "execution_status": "idle",
                    "title": "forked",
                    "forked_from_event_id": seen["body"].get("from_event_id"),
                },
            )
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"success": True})
        return httpx.Response(
            200, json={"id": CREATED, "execution_status": "idle", "tags": {}}
        )

    return _mock_client(handle)


def test_from_event_id_reaches_the_fork_body_and_back() -> None:
    seen: dict = {}
    client = _fork_client(seen)

    out = client.dispatch_from(SOURCE, "review the change", from_event_id=BRANCH_POINT)

    assert seen["body"]["from_event_id"] == BRANCH_POINT
    assert out["forked_from_event_id"] == BRANCH_POINT
    assert out["forked_from"] == SOURCE
    assert out["id"] == CREATED


def test_omitting_from_event_id_sends_no_key_and_still_forks() -> None:
    """A plain fork must stay a full copy.

    Guards against the parameter leaking into the default path, e.g. an
    unconditional ``body["from_event_id"] = from_event_id``. Note the reason is
    *not* that an explicit null differs from an absent key: it does not.
    ``ForkConversationRequest.from_event_id`` defaults to ``None``, so
    ``model_validate({})`` and ``model_validate({"from_event_id": None})`` are
    the same request. The key is omitted because that is the honest description
    of what was asked for, not because the server distinguishes the two.
    """
    seen: dict = {}
    client = _fork_client(seen)

    out = client.dispatch_from(SOURCE, "review the change")

    assert "from_event_id" not in seen["body"]
    assert out["forked_from_event_id"] is None
    assert out["id"] == CREATED


def test_unknown_event_id_is_refused_rather_than_forking_everything() -> None:
    """A stale id must fail loudly, not fall back to a full-history fork.

    That fallback is the failure mode worth pinning: it would look like it
    worked, and would cost the whole source history the caller was trying to
    avoid.
    """

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/fork"):
            return httpx.Response(
                404, json={"detail": f"from_event_id {BRANCH_POINT} not found"}
            )
        if path.endswith("/events"):
            raise AssertionError(f"the task must not be sent, saw {path}")
        # The fork-chain read `dispatch_from` does before forking.
        return httpx.Response(
            200, json={"id": SOURCE, "execution_status": "idle", "tags": {}}
        )

    client = _mock_client(handle)

    with pytest.raises(client_mod.ClientError, match="from_event_id"):
        client.dispatch_from(SOURCE, "review", from_event_id=BRANCH_POINT)


def test_a_daemon_that_ignores_the_bound_is_refused() -> None:
    """An older daemon would answer exactly like a deliberate full fork.

    The response alone cannot distinguish "bounded as asked" from "key ignored,
    whole history copied", and the fork this exists to avoid is the unbounded
    one. The guard therefore compares the reported lineage against the request,
    and must fire *before* the task is sent, so an unbounded fork is never run.
    """
    sent: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/fork"):
            # What a daemon predating the parameter returns: no lineage field.
            return httpx.Response(201, json={"id": CREATED, "execution_status": "idle"})
        if path.endswith("/events"):
            sent.append(path)
            return httpx.Response(200, json={"success": True})
        # dispatch_from reads the fork chain before forking, so a status read
        # is expected here and must not be counted as the task being sent.
        return httpx.Response(
            200, json={"id": SOURCE, "execution_status": "idle", "tags": {}}
        )

    client = _mock_client(handle)

    with pytest.raises(client_mod.ClientError, match="did not honour"):
        client.dispatch_from(SOURCE, "review", from_event_id=BRANCH_POINT)

    assert sent == [], f"the task must not be sent, saw {sent}"


def test_a_daemon_reporting_a_different_bound_is_refused() -> None:
    """Lineage disagreeing with the request is refused, not reconciled."""

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/fork"):
            return httpx.Response(
                201,
                json={
                    "id": CREATED,
                    "execution_status": "idle",
                    "forked_from_event_id": "some-other-event",
                },
            )
        if path.endswith("/events"):
            raise AssertionError("the task must not be sent")
        return httpx.Response(
            200, json={"id": SOURCE, "execution_status": "idle", "tags": {}}
        )

    client = _mock_client(handle)

    with pytest.raises(client_mod.ClientError, match="did not honour"):
        client.dispatch_from(SOURCE, "review", from_event_id=BRANCH_POINT)


def test_mcp_tool_passes_from_event_id_through(monkeypatch) -> None:
    """The tool whose docstring is the orchestrator's documentation.

    Also checks the failure shape the orchestrator sees: ``_guard`` turns a
    ClientError into data, because a raised exception reaches it as a protocol
    error it cannot reason about.
    """
    captured: dict = {}

    class FakeClient:
        def dispatch_from(self, session, task, **kwargs):
            captured.update({"session": session, "task": task, **kwargs})
            return {"id": CREATED}

    monkeypatch.setattr(mcp_server, "_get_client", lambda: FakeClient())

    out = mcp_server.dispatch_from(
        SOURCE, "review", title="t", from_event_id=BRANCH_POINT
    )
    assert captured["from_event_id"] == BRANCH_POINT
    assert captured["title"] == "t"
    assert out["id"] == CREATED

    class FailingClient:
        def dispatch_from(self, session, task, **kwargs):
            raise client_mod.ClientError("from_event_id evt-x not found")

    monkeypatch.setattr(mcp_server, "_get_client", lambda: FailingClient())

    failure = mcp_server.dispatch_from(SOURCE, "review", from_event_id="evt-x")
    assert failure["error"] == "ClientError"
    assert "from_event_id" in failure["message"]


# ── Naming a branch point ─────────────────────────────────────────────
#
# from_event_id is only usable if a caller can learn an event id. transcript
# is the only tool that hands them out, and it used to drop the id for
# message entries -- so the natural branch point, a conversation boundary,
# could not be named at all. Found by running the smoke test against a real
# daemon rather than by reading the code.


def _search_client(items: list[dict]) -> client_mod.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events/search"):
            return httpx.Response(200, json={"items": items, "next_page_id": None})
        return httpx.Response(
            200, json={"id": SOURCE, "execution_status": "idle", "tags": {}}
        )

    return _mock_client(handle)


def test_transcript_message_entries_carry_an_event_id() -> None:
    """Without this, no tool can name a message as a fork point."""
    client = _search_client(
        [
            {
                "kind": "MessageEvent",
                "id": BRANCH_POINT,
                "llm_message": {
                    "role": "user",
                    "content": [{"type": "text", "text": "do the thing"}],
                },
            }
        ]
    )

    events = client.transcript(SOURCE)["events"]

    assert len(events) == 1
    assert events[0]["type"] == "message"
    assert events[0]["id"] == BRANCH_POINT
    assert events[0]["text"] == "do the thing"


def test_every_transcript_entry_type_carries_an_id() -> None:
    """The docstring says every entry has an id; this is what holds it true."""
    client = _search_client(
        [
            {
                "kind": "MessageEvent",
                "id": "msg-1",
                "llm_message": {"role": "assistant", "content": []},
            },
            {
                "kind": "ActionEvent",
                "id": "act-1",
                "tool_name": "file_editor",
                "thought": [],
                "action": {},
            },
            {
                "kind": "ObservationEvent",
                "id": "obs-1",
                "tool_name": "file_editor",
                "observation": {"content": []},
            },
            {
                "kind": "ConversationErrorEvent",
                "id": "err-1",
                "code": "MaxIterationsReached",
                "detail": "d",
            },
        ]
    )

    events = client.transcript(SOURCE)["events"]

    assert {e["type"] for e in events} == {
        "message",
        "action",
        "observation",
        "error",
    }
    assert all(e.get("id") for e in events), events


def test_an_id_from_transcript_is_accepted_as_a_branch_point() -> None:
    """The round trip an orchestrator actually performs.

    transcript gives the id; dispatch_from takes it, on one client, as a
    caller would. Testing each half alone would not catch the halves
    disagreeing about what an event id is.
    """
    seen: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events/search"):
            return httpx.Response(
                200,
                json={
                    "items": [
                        {
                            "kind": "MessageEvent",
                            "id": BRANCH_POINT,
                            "llm_message": {
                                "role": "user",
                                "content": [{"type": "text", "text": "do it"}],
                            },
                        }
                    ],
                    "next_page_id": None,
                },
            )
        if request.url.path.endswith("/fork"):
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                201,
                json={
                    "id": CREATED,
                    "execution_status": "idle",
                    "forked_from_event_id": seen["body"].get("from_event_id"),
                },
            )
        if request.url.path.endswith("/events"):
            return httpx.Response(200, json={"success": True})
        return httpx.Response(
            200, json={"id": CREATED, "execution_status": "idle", "tags": {}}
        )

    client = _mock_client(handle)

    branch = client.transcript(SOURCE)["events"][0]["id"]
    out = client.dispatch_from(SOURCE, "review", from_event_id=branch)

    assert branch == BRANCH_POINT
    assert seen["body"]["from_event_id"] == BRANCH_POINT
    assert out["forked_from_event_id"] == BRANCH_POINT
