"""H3: blocking waits over session outcomes.

The client is exercised against a MockTransport, so no daemon is started and
every sample is deterministic.
"""

from __future__ import annotations

import json
import types
from collections.abc import Callable

import httpx
import pytest

from agentrt.runtime import client as client_mod


A = "11111111-1111-1111-1111-111111111111"
B = "22222222-2222-2222-2222-222222222222"
C = "33333333-3333-3333-3333-333333333333"
D = "44444444-4444-4444-4444-444444444444"
E = "55555555-5555-5555-5555-555555555555"
F = "66666666-6666-6666-6666-666666666666"
G = "77777777-7777-7777-7777-777777777777"
H = "88888888-8888-8888-8888-888888888888"

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


def _handler(
    statuses: dict[str, dict],
    results: dict[str, dict] | None = None,
    samples: dict[str, int] | None = None,
    changing: dict[str, list[dict]] | None = None,
) -> Handler:
    results = results or {}
    samples = samples if samples is not None else {}
    changing = changing or {}

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/agent_final_response"):
            conversation = path.split("/")[-2]
            return httpx.Response(
                200,
                json=results.get(conversation, {"response": "", "state": "final"}),
            )
        conversation = path.rsplit("/", 1)[-1]
        if conversation in changing:
            series = changing[conversation]
            index = min(samples.get(conversation, 0), len(series) - 1)
            samples[conversation] = samples.get(conversation, 0) + 1
            payload = series[index]
        elif conversation in statuses:
            payload = statuses[conversation]
        else:
            return httpx.Response(404, json={"detail": "not found"})
        body = {"id": conversation, "tags": {}, "workspace": {"working_dir": "/tmp"}}
        body.update(payload)
        return httpx.Response(200, json=body)

    return handle


def test_wait_all_groups_outcomes() -> None:
    statuses = {
        A: {"execution_status": "finished", "result_state": "final"},
        B: {"execution_status": "paused", "result_state": "partial"},
        C: {"execution_status": "error", "result_state": "partial"},
        D: {"execution_status": "stuck", "result_state": "unavailable"},
    }
    results = {
        A: {"response": "done", "state": "final"},
        B: {"response": "half", "state": "partial"},
        C: {"response": "partial text", "state": "partial"},
        D: {"response": None, "state": "unavailable"},
    }
    client = _mock_client(_handler(statuses, results))

    out = client.wait([A, B, C, D], mode="all", timeout=5.0, poll_interval=0.5)

    assert [item["id"] for item in out["completed"]] == [A]
    assert [item["id"] for item in out["stopped"]] == [B]
    assert [item["id"] for item in out["partial"]] == [C]
    assert [item["id"] for item in out["failed"]] == [D]
    assert out["still_running"] == []
    assert out["timed_out"] is False
    assert out["completed"][0]["result"] == "done"


def test_wait_buckets_an_error_with_no_usable_output_as_failed() -> None:
    """H8 item 5: an errored run with an empty result is `failed`, not
    `partial` -- the shape a consumer report said was misbucketed.

    `derive_result_state` (run_scope.py) names every non-`finished`
    terminal `PARTIAL` by design (a request-scope "was this answered"
    state, not a verdict on the content) -- so `state == "partial"` alone
    does not mean usable output exists. `_wait_bucket` already narrows it:
    empty/blank `result` text demotes to `failed` regardless of `state`.
    This closes item 5 with a regression rather than a code change --
    read directly, `_wait_bucket` was already correct; nothing exercised
    this exact combination (error execution_status, `state: "partial"`,
    blank text) before. docs/plans/deepseek-hardening.md H8 item 5,
    2026-09-17.
    """
    statuses = {E: {"execution_status": "error", "result_state": "partial"}}
    results = {E: {"response": "", "state": "partial"}}
    client = _mock_client(_handler(statuses, results))

    out = client.wait([E], mode="all", timeout=5.0, poll_interval=0.5)

    assert [item["id"] for item in out["failed"]] == [E]
    assert out["partial"] == []


def test_wait_timeout_returns_still_running() -> None:
    statuses = {
        A: {"execution_status": "finished", "result_state": "final"},
        E: {"execution_status": "running", "result_state": "pending"},
    }
    client = _mock_client(_handler(statuses))

    out = client.wait([A, E], mode="all", timeout=1.0, poll_interval=0.5)

    assert out["timed_out"] is True
    assert [item["id"] for item in out["still_running"]] == [E]
    assert "result" not in out["still_running"][0]


def test_wait_caps_a_large_timeout_to_the_safe_ceiling(monkeypatch) -> None:
    """A requested timeout above the safe ceiling is truncated, not honored.

    Regression for the transport-idle-ceiling gap: a large ``timeout`` used
    to hold the call open for its full requested span with zero interim
    signal, which some transports between an orchestrator and this server
    kill first with a generic error. docs/plans/deepseek-hardening.md H8.
    """
    monkeypatch.setenv("AGENTRT_WAIT_SAFE_CEILING_SECONDS", "1")
    statuses = {E: {"execution_status": "running", "result_state": "pending"}}
    client = _mock_client(_handler(statuses))

    out = client.wait([E], mode="all", timeout=1000.0, poll_interval=0.3)

    assert out["timed_out"] is True
    assert [item["id"] for item in out["still_running"]] == [E]


def test_wait_safe_ceiling_disabled_by_zero(monkeypatch) -> None:
    """The safe ceiling itself is opt-out, matching AGENTRT_MAX_SESSIONS."""
    monkeypatch.setenv("AGENTRT_WAIT_SAFE_CEILING_SECONDS", "0")
    statuses = {A: {"execution_status": "finished", "result_state": "final"}}
    client = _mock_client(_handler(statuses))

    out = client.wait([A], mode="all", timeout=1.0, poll_interval=0.5)

    assert out["timed_out"] is False
    assert [item["id"] for item in out["completed"]] == [A]


def test_wait_missing_id_does_not_wait_forever() -> None:
    statuses = {A: {"execution_status": "finished", "result_state": "final"}}
    client = _mock_client(_handler(statuses))

    out = client.wait([A, F], mode="all", timeout=1.0, poll_interval=0.5)

    assert [item["id"] for item in out["missing"]] == [F]
    assert [item["id"] for item in out["completed"]] == [A]
    assert out["timed_out"] is False


def test_wait_any_returns_on_the_first_outcome() -> None:
    statuses = {
        A: {"execution_status": "finished", "result_state": "final"},
        E: {"execution_status": "running", "result_state": "pending"},
    }
    client = _mock_client(_handler(statuses))

    out = client.wait([A, E], mode="any", timeout=5.0, poll_interval=0.5)

    assert [item["id"] for item in out["completed"]] == [A]
    assert [item["id"] for item in out["still_running"]] == [E]
    # An early return is not a timeout: the deadline did not end the wait.
    assert out["timed_out"] is False


def test_wait_does_not_settle_a_terminal_session_with_unconsumed_input() -> None:
    """`finished` with a newer unconsumed input still has no answer."""
    statuses = {
        A: {
            "execution_status": "finished",
            "result_state": "pending",
            "admission_status": "admitted",
        }
    }
    client = _mock_client(_handler(statuses))

    out = client.wait([A], mode="all", timeout=1.0, poll_interval=0.5)

    assert out["completed"] == []
    assert [item["id"] for item in out["still_running"]] == [A]
    assert out["timed_out"] is True


def test_wait_rejects_a_provisional_finished() -> None:
    """A run that reports finished once then continues is not settled."""
    changing = {
        G: [
            {"execution_status": "finished", "result_state": "final"},
            {"execution_status": "running", "result_state": "pending"},
            {"execution_status": "running", "result_state": "pending"},
        ]
    }
    client = _mock_client(_handler({}, changing=changing))

    out = client.wait([G], mode="all", timeout=1.2, poll_interval=0.5)

    assert out["completed"] == []
    assert [item["id"] for item in out["still_running"]] == [G]
    assert out["timed_out"] is True


def test_finalize_passes_summary_and_returns_the_outcome() -> None:
    seen: dict = {}

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/finalize"):
            seen["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "response": "half",
                    "state": "partial",
                    "summary": "did X; Y remains",
                },
            )
        return httpx.Response(
            200, json={"id": A, "execution_status": "paused", "tags": {}}
        )

    client = _mock_client(handle)
    out = client.finalize(A, summary=True)

    assert seen["body"] == {"summary": True}
    assert out["state"] == "partial"
    assert out["result"] == "half"
    assert out["summary"] == "did X; Y remains"
    assert out["status"] == "paused"


def test_finalize_omits_an_absent_summary() -> None:
    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/finalize"):
            return httpx.Response(200, json={"response": None, "state": "pending"})
        return httpx.Response(
            200, json={"id": A, "execution_status": "paused", "tags": {}}
        )

    client = _mock_client(handle)
    out = client.finalize(A)

    assert out["result"] is None
    assert "summary" not in out


def test_wait_does_not_settle_unadmitted_work() -> None:
    """Accepted-but-unstarted input is not an outcome, whatever the status."""
    statuses = {
        H: {
            "execution_status": "idle",
            "result_state": "pending",
            "admission_status": "queued",
        }
    }
    client = _mock_client(_handler(statuses))

    out = client.wait([H], mode="all", timeout=1.0, poll_interval=0.5)

    assert out["completed"] == []
    assert [item["id"] for item in out["still_running"]] == [H]


def test_still_running_items_carry_progress() -> None:
    """A poller sees movement without a second call."""
    statuses = {
        E: {
            "execution_status": "running",
            "result_state": "pending",
            "admission_status": "admitted",
            "iterations_used": 3,
            "iterations_remaining": 47,
        }
    }
    client = _mock_client(_handler(statuses))

    out = client.wait([E], mode="all", timeout=1.0, poll_interval=0.5)

    item = out["still_running"][0]
    assert item["iterations_used"] == 3
    assert item["iterations_remaining"] == 47
    assert item["admission_status"] == "admitted"
    assert "result" not in item  # no partial output as an answer


# ── H9 item 2: what a settled item carries ────────────────────────────
#
# A wait already returned `result`, so the "second round-trip" item 2 was
# written against did not exist for the answer. The real delta was the title
# (free: the settle sample already fetched the row holding it) and usage (a
# separate endpoint, so opt-in and counted below).


def _tracking_handler(
    statuses: dict[str, dict],
    results: dict[str, dict] | None = None,
    usage_status: int = 200,
) -> tuple[Handler, list[str]]:
    """The standard handler, recording every path it is asked for."""
    base = _handler(statuses, results)
    paths: list[str] = []

    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        paths.append(path)
        if path.endswith("/usage"):
            if usage_status != 200:
                return httpx.Response(usage_status, json={"detail": "nope"})
            conversation = path.split("/")[-2]
            return httpx.Response(
                200,
                json={
                    "session": conversation,
                    "totals": {"prompt": 10, "completion": 2},
                },
            )
        return base(request)

    return handle, paths


def test_settled_item_carries_its_title_at_no_extra_cost() -> None:
    statuses = {
        A: {
            "execution_status": "finished",
            "result_state": "final",
            "title": "nightly audit",
        }
    }
    # `execution_status` is included so `result()` does not re-read status to
    # learn it: the count below is then the settle rule's own samples plus one
    # result read, and does not move with the mock's payload shape.
    results = {
        A: {"response": "done", "state": "final", "execution_status": "finished"}
    }
    handle, paths = _tracking_handler(statuses, results)
    client = _mock_client(handle)

    out = client.wait([A], mode="all", timeout=5.0, poll_interval=0.5)

    assert out["completed"][0]["title"] == "nightly audit"
    # Teeth beyond "no /usage": two samples (the second-sample rule needs a
    # previous terminal reading) plus the result read. A title fetched by its
    # own call would make this 4. A differential form -- same scenario with and
    # without a title, asserting equal counts -- was tried and rejected: an
    # implementation that fetched the title in a separate call would do so in
    # both runs, so the counts would agree and the assertion would pass.
    assert len(paths) == 3, paths
    assert not any(p.endswith("/usage") for p in paths), paths


def test_usage_is_absent_unless_asked_for() -> None:
    """The default must stay one request per session, not two."""
    statuses = {A: {"execution_status": "finished", "result_state": "final"}}
    results = {A: {"response": "done", "state": "final"}}
    handle, paths = _tracking_handler(statuses, results)
    client = _mock_client(handle)

    out = client.wait([A], mode="all", timeout=5.0, poll_interval=0.5)

    assert "usage" not in out["completed"][0]
    assert not any(p.endswith("/usage") for p in paths), paths


def test_include_usage_adds_usage_to_each_settled_item() -> None:
    """One usage request per settled session -- the N a fan-out pays."""
    statuses = {
        A: {"execution_status": "finished", "result_state": "final"},
        B: {"execution_status": "error", "result_state": "partial"},
    }
    results = {
        A: {"response": "done", "state": "final"},
        B: {"response": "half", "state": "partial"},
    }
    handle, paths = _tracking_handler(statuses, results)
    client = _mock_client(handle)

    out = client.wait(
        [A, B], mode="all", timeout=5.0, poll_interval=0.5, include_usage=True
    )

    usage_paths = [p for p in paths if p.endswith("/usage")]
    assert len(usage_paths) == 2, paths
    assert out["completed"][0]["usage"]["totals"]["prompt"] == 10
    assert out["partial"][0]["usage"]["totals"]["prompt"] == 10


def test_a_failing_usage_read_does_not_cost_the_result() -> None:
    """The result is what the caller waited for; usage is an addition."""
    statuses = {A: {"execution_status": "finished", "result_state": "final"}}
    results = {A: {"response": "done", "state": "final"}}
    handle, _ = _tracking_handler(statuses, results, usage_status=500)
    client = _mock_client(handle)

    out = client.wait(
        [A], mode="all", timeout=5.0, poll_interval=0.5, include_usage=True
    )

    item = out["completed"][0]
    assert item["result"] == "done"
    assert "usage" not in item


@pytest.mark.parametrize(
    "body",
    [
        # A 200 whose body is not JSON, and a 204 with no body at all: both
        # make `.json()` raise past `except ClientError`, so before that catch
        # was widened this lost every bucket rather than one key. A status-code
        # test alone (the 500 above) never reaches that path.
        httpx.Response(200, text="<html>not json</html>"),
        httpx.Response(204),
    ],
    ids=["non-json-200", "empty-204"],
)
def test_an_undecodable_usage_response_does_not_cost_the_result(body) -> None:
    statuses = {A: {"execution_status": "finished", "result_state": "final"}}
    results = {A: {"response": "done", "state": "final"}}
    base = _handler(statuses, results)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/usage"):
            return body
        return base(request)

    client = _mock_client(handle)

    out = client.wait(
        [A], mode="all", timeout=5.0, poll_interval=0.5, include_usage=True
    )

    item = out["completed"][0]
    assert item["result"] == "done"
    assert "usage" not in item


def test_an_undecodable_transcript_does_not_cost_an_errored_result() -> None:
    """The same escape as the usage read, on a path nothing opts into.

    `result()` adds `progress_summary` for an errored session by reading the
    transcript, which ends in `.json()` -- so an undecodable body used to take
    the whole result with it, for every failed session rather than only when a
    caller asked for more.
    """
    statuses = {A: {"execution_status": "error", "result_state": "partial"}}
    results = {
        A: {
            "response": "partial text",
            "state": "partial",
            "execution_status": "error",
        }
    }
    base = _handler(statuses, results)

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/events/search"):
            return httpx.Response(200, text="<html>not json</html>")
        return base(request)

    payload = _mock_client(handle).result(A)

    assert payload["result"] == "partial text"
    assert "progress_summary" not in payload


def test_still_running_items_carry_a_title() -> None:
    statuses = {
        E: {
            "execution_status": "running",
            "result_state": "pending",
            "title": "long index build",
        }
    }
    client = _mock_client(_handler(statuses))

    out = client.wait([E], mode="all", timeout=1.0, poll_interval=0.5)

    assert out["still_running"][0]["title"] == "long index build"


def test_mcp_wait_tools_forward_include_usage(monkeypatch) -> None:
    """The tool wrappers are the surface an orchestrator actually calls."""
    from agentrt.runtime import mcp_server

    captured: list[dict] = []

    class FakeClient:
        def wait(self, session_ids, **kwargs):
            captured.append(kwargs)
            return {"completed": []}

    monkeypatch.setattr(mcp_server, "_get_client", lambda: FakeClient())

    mcp_server.wait_any([A], include_usage=True)
    mcp_server.wait_all([A], include_usage=True)
    mcp_server.wait_all([A])

    assert captured[0]["mode"] == "any"
    assert captured[0]["include_usage"] is True
    assert captured[1]["mode"] == "all"
    assert captured[1]["include_usage"] is True
    # Omitted by default, so an older daemon path is unchanged.
    assert captured[2]["include_usage"] is False
