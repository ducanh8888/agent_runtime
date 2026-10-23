"""The multi-worker front merges what clients read from several workers (#24)."""

from __future__ import annotations

import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from agentrt.runtime.front import Front, Worker, build_app, conversations_dir


TOKEN = "front-token"


def _row(minute: int) -> dict:
    return {
        "id": str(uuid.uuid4()),
        "created_at": f"2026-10-15T10:{minute:02d}:00+00:00",
        "updated_at": f"2026-10-15T10:{minute:02d}:00+00:00",
    }


def _paged(rows: list[dict], request: httpx.Request) -> dict:
    """What one worker's search answers: newest first, page_id = next row's hex."""
    rows = sorted(rows, key=lambda r: r["created_at"], reverse=True)
    limit = int(request.url.params.get("limit", 100))
    start = 0
    if page_id := request.url.params.get("page_id"):
        start = next(i for i, r in enumerate(rows) if uuid.UUID(r["id"]).hex == page_id)
    page = rows[start : start + limit]
    rest = rows[start + limit :]
    return {
        "items": page,
        "next_page_id": uuid.UUID(rest[0]["id"]).hex if rest else None,
    }


@pytest.fixture
def front(tmp_path: Path):
    def make(handler) -> TestClient:
        front = Front(state=tmp_path, token=TOKEN, count=2)
        for worker, port in zip(front.workers, (1001, 1002), strict=True):
            worker.port = port
            worker.ready = True
        front._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        client = TestClient(build_app(front))
        client.headers["X-Session-API-Key"] = TOKEN
        client.front = front  # type: ignore[attr-defined]
        return client

    return make


def test_search_pages_through_every_worker_in_one_order(front) -> None:
    shards = {1001: [_row(m) for m in (1, 4, 5, 9)], 1002: [_row(m) for m in (2, 3, 7)]}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_paged(shards[request.url.port], request))

    client = front(handler)
    seen: list[str] = []
    page_id = None
    for _ in range(10):
        params = {"limit": 3, **({"page_id": page_id} if page_id else {})}
        data = client.get("/api/conversations/search", params=params).json()
        seen.extend(item["created_at"] for item in data["items"])
        page_id = data["next_page_id"]
        if page_id is None:
            break

    every = sorted((r["created_at"] for s in shards.values() for r in s), reverse=True)
    assert seen == every


def test_conversation_routes_go_to_the_worker_that_holds_it(front, tmp_path) -> None:
    conversation_id = uuid.uuid4()
    (conversations_dir(tmp_path, 1) / conversation_id.hex).mkdir(parents=True)
    hits: list[int | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.port)
        return httpx.Response(200, json={"id": str(conversation_id)})

    client = front(handler)
    assert client.get(f"/api/conversations/{conversation_id}").status_code == 200
    assert client.post(f"/api/conversations/{conversation_id}/run").status_code == 200
    assert hits == [1002, 1002]


def test_new_conversations_alternate_between_workers(front) -> None:
    hits: list[int | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        hits.append(request.url.port)
        return httpx.Response(201, json={})

    client = front(handler)
    for _ in range(4):
        client.post("/api/conversations", json={})
    assert hits == [1001, 1002, 1001, 1002]


def test_capacity_adds_up_workers_and_reports_each(front) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        running = 3 if request.url.port == 1001 else 4
        return httpx.Response(
            200,
            json={
                "running": running,
                "queued": 0,
                "limit": 10,
                "available": 10 - running,
                "running_sessions": [{"id": str(i)} for i in range(running)],
            },
        )

    data = front(handler).get("/api/conversations/capacity").json()
    assert data["running"] == 7
    assert data["limit"] == 20
    assert data["available"] == 13
    assert len(data["running_sessions"]) == 7
    assert [w["running"] for w in data["workers"]] == [3, 4]


def test_requests_without_the_token_are_refused(front) -> None:
    client = front(lambda request: httpx.Response(200, json={}))
    client.headers.pop("X-Session-API-Key")
    assert client.get("/api/conversations/search").status_code == 401


def test_health_waits_for_every_worker(front) -> None:
    client = front(lambda request: httpx.Response(200, json={}))
    assert client.get("/health").status_code == 200
    workers: list[Worker] = client.front.workers  # type: ignore[attr-defined]
    workers[1].ready = False
    assert client.get("/health").status_code == 503


_JOB_PARENT = """
import subprocess, sys, time
from agentrt.runtime import front
job = front._kill_on_close_job()
child = subprocess.Popen(
    [sys.executable, "-c", "import time; time.sleep(120)"],
    stdin=subprocess.DEVNULL,
)
assert job is not None and front._assign_to_job(job, child)
print(child.pid, flush=True)
time.sleep(120)
"""


@pytest.mark.skipif(sys.platform != "win32", reason="Windows job objects")
def test_workers_die_when_the_front_is_hard_killed() -> None:
    """Windows does not end children with their parent: without the job, a
    `taskkill /F` of the front left every worker running (#24)."""
    parent = subprocess.Popen(
        [sys.executable, "-c", _JOB_PARENT],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        text=True,
    )
    assert parent.stdout is not None
    child_pid = int(parent.stdout.readline())
    subprocess.run(["taskkill", "/PID", str(parent.pid), "/F"], capture_output=True)
    parent.wait(timeout=30)

    from agentrt.runtime import daemon

    deadline = time.monotonic() + 15
    while daemon._process_alive(child_pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    alive = daemon._process_alive(child_pid)
    if alive:
        subprocess.run(["taskkill", "/PID", str(child_pid), "/F"], capture_output=True)
    assert not alive
