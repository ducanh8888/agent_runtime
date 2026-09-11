"""H3: blocking waits over session outcomes.

The client is exercised against a MockTransport, so no daemon is started and
every sample is deterministic.
"""

from __future__ import annotations

import json
import types
from collections.abc import Callable

import httpx

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
