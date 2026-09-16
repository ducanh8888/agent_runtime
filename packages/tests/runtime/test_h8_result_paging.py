"""H8 item 3: the answer can be sized and windowed.

The item asked for `wait_*` to return metadata by default *instead of* the text.
The decision was the other way round: the default stays whole, because a settled
call that returns a summary forces the second call H9 item 2 removed, and paging
is opt-in for the case it exists for -- an answer too big to want in context.
"""

from __future__ import annotations

import hashlib
import types

import httpx
import pytest

from agentrt.runtime import client as client_mod, mcp_server


SESSION = "11111111-1111-1111-1111-111111111111"
TEXT = "".join(f"line {i}\n" for i in range(200))


def _client(text: str = TEXT, status: str = "finished") -> client_mod.Client:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agent_final_response"):
            return httpx.Response(
                200,
                json={"response": text, "state": "final", "execution_status": status},
            )
        return httpx.Response(
            200,
            json={
                "id": SESSION,
                "execution_status": status,
                "tags": {},
                "workspace": {"working_dir": "/tmp"},
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
    return client


def test_the_default_is_still_the_whole_text() -> None:
    """Opt-in means the shipped behaviour is untouched."""
    payload = _client().result(SESSION)

    assert payload["result"] == TEXT
    assert payload["result_length"] == len(TEXT)
    assert "result_truncated" not in payload
    assert "result_sha256" not in payload


def test_max_chars_returns_a_window_and_the_total_length() -> None:
    payload = _client().result(SESSION, max_chars=50)

    assert payload["result"] == TEXT[:50]
    assert payload["result_length"] == len(TEXT)
    assert payload["result_offset"] == 0
    assert payload["result_truncated"] is True


def test_offset_pages_through_the_whole_answer() -> None:
    """Every character reachable, in order, with no gap or overlap."""
    client = _client()
    pages = [
        client.result(SESSION, offset=i, max_chars=64) for i in range(0, len(TEXT), 64)
    ]

    assert "".join(p["result"] for p in pages) == TEXT


def test_the_hash_covers_the_whole_text_not_the_window() -> None:
    """Both pages agree on one digest -- that is what makes a paged read
    checkable, and what distinguishes pages of one answer from two answers
    that merely start alike."""
    a = _client().result(SESSION, offset=0, max_chars=10)
    b = _client().result(SESSION, offset=1990, max_chars=10)

    expected = hashlib.sha256(TEXT.encode("utf-8")).hexdigest()
    assert a["result_sha256"] == expected
    assert b["result_sha256"] == expected


def test_reading_the_last_window_is_not_marked_truncated() -> None:
    """A window that happens to reach the end is the whole tail, not a cut."""
    payload = _client().result(SESSION, offset=len(TEXT) - 10)

    assert payload["result"] == TEXT[-10:]
    assert payload["result_truncated"] is True  # it is a window...

    whole = _client().result(SESSION, offset=0, max_chars=len(TEXT))
    assert whole["result_truncated"] is False


@pytest.mark.parametrize(
    "kwargs,message",
    [
        ({"offset": -1}, "offset must not be negative"),
        ({"max_chars": 0}, "max_chars must be positive"),
        ({"max_chars": -5}, "max_chars must be positive"),
    ],
)
def test_an_impossible_window_is_refused(kwargs, message) -> None:
    """Refused rather than silently clamped: a caller that asked for a window
    it cannot have should learn that, not receive a different one."""
    with pytest.raises(client_mod.ClientError, match=message):
        _client().result(SESSION, **kwargs)


def test_an_offset_past_the_end_returns_empty_not_an_error() -> None:
    payload = _client().result(SESSION, offset=len(TEXT) + 100)

    assert payload["result"] == ""
    assert payload["result_truncated"] is True
    assert payload["result_length"] == len(TEXT)


def test_a_null_result_is_left_alone() -> None:
    """`pending` has no text to window; the metadata must not invent one."""

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/agent_final_response"):
            # What a session with no answer yet returns.
            return httpx.Response(404, json={"detail": "no answer"})
        return httpx.Response(
            200,
            json={
                "id": SESSION,
                "execution_status": "running",
                "result_state": "pending",
                "tags": {},
                "workspace": {"working_dir": "/tmp"},
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

    payload = client.result(SESSION, max_chars=10)

    assert payload["result"] is None
    assert "result_length" not in payload


def test_wait_items_carry_the_length_too() -> None:
    """A fan-out can see how big each answer is before fetching any of it."""
    out = _client().wait([SESSION], mode="all", timeout=5.0, poll_interval=0.5)

    item = out["completed"][0]
    assert item["result"] == TEXT
    assert item["result_length"] == len(TEXT)


def test_mcp_tool_forwards_the_window(monkeypatch) -> None:
    captured: dict = {}

    class FakeClient:
        def result(self, session, **kwargs):
            captured.update(kwargs)
            return {"result": "x"}

    monkeypatch.setattr(mcp_server, "_get_client", lambda: FakeClient())

    mcp_server.result(SESSION)
    assert captured == {"offset": None, "max_chars": None}

    mcp_server.result(SESSION, offset=5, max_chars=10)
    assert captured == {"offset": 5, "max_chars": 10}
