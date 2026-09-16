"""H8 item 10's remaining half: the deliberation is reachable on request.

`thought` is frequently empty on this deployment's ActionEvents while
`reasoning_content` on the same event is not, so a transcript read could show a
tool call with no visible intent. `reasoning_content` is excluded from
transcripts by design (`daemon-behavior.md`), so this adds it as an opt-in
field rather than flipping the default and silently reopening that exclusion.
"""

from __future__ import annotations

import types

import httpx

from agentrt.runtime import client as client_mod, mcp_server


SESSION = "11111111-1111-1111-1111-111111111111"
REASONING = "First I check the listing, then I decide which file to open. " * 8


def _client(items: list[dict]) -> client_mod.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events/search"):
            return httpx.Response(200, json={"items": items, "next_page_id": None})
        return httpx.Response(
            200, json={"id": SESSION, "execution_status": "idle", "tags": {}}
        )

    client = client_mod.Client()
    setattr(
        client,
        "_daemon_info",
        types.SimpleNamespace(base_url="http://daemon.test", token="test-token"),
    )
    client._profile_ref = {}
    client._http = httpx.Client(transport=httpx.MockTransport(handle))
    return client


def _action_item() -> dict:
    return {
        "kind": "ActionEvent",
        "id": "act-1",
        "tool_name": "file_editor",
        "thought": [],
        "reasoning_content": REASONING,
        "action": {"path": "/repo/src/app.py"},
    }


def test_reasoning_is_absent_by_default() -> None:
    """The documented exclusion stays the default."""
    events = _client([_action_item()]).transcript(SESSION)["events"]

    assert len(events) == 1
    assert events[0]["thought"] == ""
    assert "reasoning" not in events[0]


def test_include_reasoning_surfaces_the_deliberation() -> None:
    """The case item 10 hit: an action with empty `thought` but real intent."""
    events = _client([_action_item()]).transcript(SESSION, include_reasoning=True)[
        "events"
    ]

    assert events[0]["thought"] == ""
    assert REASONING.startswith(events[0]["reasoning"])
    assert "First I check the listing" in events[0]["reasoning"]


def test_reasoning_is_capped_and_marked() -> None:
    long = "x" * 5000
    item = _action_item()
    item["reasoning_content"] = long

    events = _client([item]).transcript(SESSION, include_reasoning=True)["events"]

    assert len(events[0]["reasoning"]) < len(long)
    assert events[0]["reasoning"].endswith("[truncated]")


def test_an_entry_without_reasoning_gains_no_field() -> None:
    """No empty string, no null: the key is simply absent."""
    item = _action_item()
    item["reasoning_content"] = None
    events = _client([item]).transcript(SESSION, include_reasoning=True)["events"]

    assert "reasoning" not in events[0]


def test_message_entries_can_carry_reasoning_too() -> None:
    item = {
        "kind": "MessageEvent",
        "id": "msg-1",
        "llm_message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "done"}],
            "reasoning_content": "weighing the options",
        },
    }

    events = _client([item]).transcript(SESSION, include_reasoning=True)["events"]

    assert events[0]["reasoning"] == "weighing the options"


def test_mcp_tool_forwards_include_reasoning(monkeypatch) -> None:
    captured: dict = {}

    class FakeClient:
        def transcript(self, session, **kwargs):
            captured.update(kwargs)
            return {"events": []}

    monkeypatch.setattr(mcp_server, "_get_client", lambda: FakeClient())

    mcp_server.transcript(SESSION)
    assert captured["include_reasoning"] is False

    mcp_server.transcript(SESSION, include_reasoning=True)
    assert captured["include_reasoning"] is True
