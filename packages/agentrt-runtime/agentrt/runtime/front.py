"""Front process of a multi-worker daemon (AgentRT #24).

One server process is one event loop and, in practice, one core: every step
re-sends and re-serializes a session's whole history, so about 50 long sessions
saturate it however many cores the host has. With ``AGENTRT_WORKERS`` above 1
the daemon is this front plus that many copies of the ordinary server
(`server_launch`), each owning its own conversations directory.

The front holds no conversation state. A conversation's owner is the worker
whose directory contains it on disk, so ownership survives restarts without a
table. New conversations go round-robin. Listing and capacity fan out and are
merged; settings writes are sent to every worker. A worker that exits is
started again, and its runs come back as after a daemon restart.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import itertools
import json
import os
import signal
import socket
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from agentrt.runtime import config, server_launch


API_KEY_HEADER = "X-Session-API-Key"
_CURSOR_PREFIX = "fc."
_HOP_HEADERS = {
    "host",
    "content-length",
    "transfer-encoding",
    "connection",
    "content-encoding",
    "keep-alive",
}
_START_TIMEOUT = 240.0
_SORT_FIELDS = {
    "CREATED_AT": ("created_at", False),
    "CREATED_AT_DESC": ("created_at", True),
    "UPDATED_AT": ("updated_at", False),
    "UPDATED_AT_DESC": ("updated_at", True),
}


def conversations_dir(state: Path, index: int) -> Path:
    """Worker 0 keeps the single-process directory, so existing sessions stay."""
    return state / ("conversations" if index == 0 else f"conversations-w{index}")


def _die_with_parent() -> None:
    """Linux: a worker gets SIGTERM when the front dies, even by SIGKILL.

    An orphaned worker would keep serving its conversations directory, and the
    next front would start a second owner for the same sessions.
    """
    import ctypes

    pr_set_pdeathsig = 1
    ctypes.CDLL(None).prctl(pr_set_pdeathsig, signal.SIGTERM)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _conversation_hex(segment: str) -> str | None:
    try:
        return uuid.UUID(segment).hex
    except ValueError:
        return None


@dataclass
class Worker:
    index: int
    port: int = 0
    proc: subprocess.Popen | None = None
    restarts: int = 0
    ready: bool = False

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"


@dataclass
class Front:
    state: Path
    token: str
    count: int
    workers: list[Worker] = field(default_factory=list)
    stopping: bool = False

    def __post_init__(self) -> None:
        self.workers = [Worker(index) for index in range(self.count)]
        self._next = itertools.cycle(range(self.count))
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=10.0))

    # ----- worker processes -------------------------------------------------

    def _spawn(self, worker: Worker) -> None:
        worker.port = _free_port()
        worker.ready = False
        env = os.environ.copy()
        env["AGENTRT_WORKER_INDEX"] = str(worker.index)
        env["AGENTRT_CONVERSATIONS_PATH"] = str(
            conversations_dir(self.state, worker.index)
        )
        popen_kwargs: dict = {}
        if os.name == "nt":
            # The front itself has no console; without this each worker would
            # open a console window.
            popen_kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        else:
            popen_kwargs["start_new_session"] = True
            if sys.platform.startswith("linux"):
                popen_kwargs["preexec_fn"] = _die_with_parent
        worker.proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "agentrt.runtime.server_launch",
                "--host",
                "127.0.0.1",
                "--port",
                str(worker.port),
            ],
            stdin=subprocess.DEVNULL,
            env=env,
            **popen_kwargs,
        )
        print(
            f"front: worker {worker.index} pid={worker.proc.pid} port={worker.port}",
            flush=True,
        )

    async def _wait_ready(self, worker: Worker) -> None:
        deadline = time.monotonic() + _START_TIMEOUT
        while time.monotonic() < deadline:
            if worker.proc is None or worker.proc.poll() is not None:
                return
            try:
                response = await self._client.get(
                    f"{worker.base_url}/health",
                    headers={API_KEY_HEADER: self.token},
                    timeout=2.0,
                )
                if response.status_code == 200:
                    worker.ready = True
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.3)

    async def start(self) -> None:
        for worker in self.workers:
            self._spawn(worker)
        await asyncio.gather(*(self._wait_ready(w) for w in self.workers))

    async def supervise(self) -> None:
        while not self.stopping:
            await asyncio.sleep(1.0)
            for worker in self.workers:
                if self.stopping or worker.proc is None:
                    continue
                if worker.proc.poll() is None:
                    continue
                print(
                    f"front: worker {worker.index} exited "
                    f"rc={worker.proc.returncode}; restarting",
                    flush=True,
                )
                worker.restarts += 1
                self._spawn(worker)
                await self._wait_ready(worker)

    def stop(self) -> None:
        self.stopping = True
        for worker in self.workers:
            if worker.proc is not None and worker.proc.poll() is None:
                worker.proc.terminate()
        deadline = time.monotonic() + 60.0
        for worker in self.workers:
            if worker.proc is None:
                continue
            try:
                worker.proc.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                worker.proc.kill()

    # ----- routing -----------------------------------------------------------

    def owner(self, conversation_id: str) -> Worker:
        """The worker whose directory holds the conversation; worker 0 if none."""
        hex_id = _conversation_hex(conversation_id)
        if hex_id is not None:
            for worker in self.workers:
                if (conversations_dir(self.state, worker.index) / hex_id).exists():
                    return worker
        return self.workers[0]

    def pick_for_new(self) -> Worker:
        return self.workers[next(self._next)]

    async def forward(
        self, worker: Worker, request: Request, body: bytes | None = None
    ) -> Response:
        if body is None:
            body = await request.body()
        headers = {
            key: value
            for key, value in request.headers.items()
            if key.lower() not in _HOP_HEADERS
        }
        try:
            upstream = await self._client.request(
                request.method,
                f"{worker.base_url}{request.url.path}",
                params=list(request.query_params.multi_items()),
                headers=headers,
                content=body,
            )
        except httpx.HTTPError as exc:
            return JSONResponse(
                {"detail": f"worker {worker.index} unavailable: {exc}"},
                status_code=503,
            )
        return Response(
            upstream.content,
            status_code=upstream.status_code,
            headers={
                key: value
                for key, value in upstream.headers.items()
                if key.lower() not in _HOP_HEADERS
            },
        )

    async def _get_all(self, request: Request, params: list) -> list[dict | None]:
        headers = {API_KEY_HEADER: request.headers.get(API_KEY_HEADER, "")}

        async def one(worker: Worker):
            try:
                response = await self._client.get(
                    f"{worker.base_url}{request.url.path}",
                    params=params,
                    headers=headers,
                )
            except httpx.HTTPError:
                return None
            if response.status_code != 200:
                return None
            return response.json()

        return await asyncio.gather(*(one(w) for w in self.workers))

    # ----- merged endpoints --------------------------------------------------

    async def search(self, request: Request) -> Response:
        """Merge every worker's page in sort order.

        The cursor handed back records, per worker, the id its next page
        starts at (or that the worker is exhausted), so pages stay consistent
        as callers walk them with ``page_id``.
        """
        query = [
            (k, v) for k, v in request.query_params.multi_items() if k != "page_id"
        ]
        limit = int(request.query_params.get("limit") or 100)
        order = request.query_params.get("sort_order") or "CREATED_AT_DESC"
        sort_field, reverse = _SORT_FIELDS.get(order, _SORT_FIELDS["CREATED_AT_DESC"])
        cursors = _decode_cursor(request.query_params.get("page_id"), self.count)

        headers = {API_KEY_HEADER: request.headers.get(API_KEY_HEADER, "")}

        async def page(worker: Worker):
            cursor = cursors.get(worker.index, "")
            if cursor is None:
                return []
            params = list(query)
            if cursor:
                params.append(("page_id", cursor))
            response = await self._client.get(
                f"{worker.base_url}{request.url.path}",
                params=params,
                headers=headers,
            )
            response.raise_for_status()
            data = response.json()
            return data

        try:
            pages = await asyncio.gather(*(page(w) for w in self.workers))
        except httpx.HTTPError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503)

        rows: list[tuple[int, dict]] = []
        worker_next: dict[int, str | None] = {}
        for worker, data in zip(self.workers, pages, strict=True):
            if not data:
                worker_next[worker.index] = None
                continue
            rows.extend((worker.index, item) for item in data.get("items") or [])
            worker_next[worker.index] = data.get("next_page_id")
        rows.sort(key=lambda row: _sort_key(row[1], sort_field), reverse=reverse)
        taken, rest = rows[:limit], rows[limit:]

        next_cursors: dict[int, str | None] = dict(worker_next)
        for index, item in reversed(rest):
            hex_id = _conversation_hex(str(item.get("id", "")))
            next_cursors[index] = hex_id
        more = any(value is not None for value in next_cursors.values())
        return JSONResponse(
            {
                "items": [item for _, item in taken],
                "next_page_id": _encode_cursor(next_cursors) if more else None,
            }
        )

    async def count(self, request: Request) -> Response:
        results = await self._get_all(request, list(request.query_params.multi_items()))
        return JSONResponse(sum(r for r in results if isinstance(r, int)))

    async def capacity(self, request: Request) -> Response:
        results = await self._get_all(request, list(request.query_params.multi_items()))
        merged: dict = {"workers": []}
        for worker, data in zip(self.workers, results, strict=True):
            entry = {
                "index": worker.index,
                "pid": worker.proc.pid if worker.proc else None,
                "restarts": worker.restarts,
                "responding": data is not None,
            }
            if isinstance(data, dict):
                entry.update(
                    {k: data.get(k) for k in ("running", "queued", "available")}
                )
                for key, value in data.items():
                    if key == "running_sessions":
                        merged.setdefault(key, []).extend(value or [])
                    elif key == "limit":
                        if value is None or merged.get(key, 0) is None:
                            merged[key] = None
                        else:
                            merged[key] = merged.get(key, 0) + value
                    elif isinstance(value, int | float) and not isinstance(value, bool):
                        merged[key] = merged.get(key, 0) + value
                    elif key not in merged:
                        merged[key] = value
            merged["workers"].append(entry)
        if merged.get("limit") is None:
            merged["available"] = None
        return JSONResponse(merged)

    async def batch_get(self, request: Request) -> Response:
        ids = request.query_params.getlist("ids")
        extra = [(k, v) for k, v in request.query_params.multi_items() if k != "ids"]
        headers = {API_KEY_HEADER: request.headers.get(API_KEY_HEADER, "")}
        groups: dict[int, list[str]] = {}
        for conversation_id in ids:
            groups.setdefault(self.owner(conversation_id).index, []).append(
                conversation_id
            )
        found: dict[str, dict | None] = {}

        async def fetch(index: int, group: list[str]) -> None:
            worker = self.workers[index]
            response = await self._client.get(
                f"{worker.base_url}{request.url.path}",
                params=[*extra, *(("ids", i) for i in group)],
                headers=headers,
            )
            response.raise_for_status()
            for conversation_id, item in zip(group, response.json(), strict=True):
                found[conversation_id] = item

        try:
            await asyncio.gather(*(fetch(i, g) for i, g in groups.items()))
        except httpx.HTTPError as exc:
            return JSONResponse({"detail": str(exc)}, status_code=503)
        return JSONResponse([found.get(i) for i in ids])

    async def broadcast(self, request: Request) -> Response:
        """Writes to settings go to every worker; the first answer is returned."""
        body = await request.body()
        responses = [await self.forward(w, request, body) for w in self.workers]
        failed = [r for r in responses if r.status_code >= 400]
        return failed[0] if failed else responses[0]

    async def health(self, _request: Request) -> Response:
        ready = all(
            w.ready and (w.proc is None or w.proc.poll() is None) for w in self.workers
        )
        return JSONResponse(
            {
                "status": "ok" if ready else "starting",
                "workers": [
                    {"index": w.index, "ready": w.ready, "restarts": w.restarts}
                    for w in self.workers
                ],
            },
            status_code=200 if ready else 503,
        )

    async def dispatch(self, request: Request) -> Response:
        if request.headers.get(API_KEY_HEADER) != self.token:
            return JSONResponse({"detail": "Unauthorized"}, status_code=401)
        path = request.url.path
        method = request.method
        parts = [p for p in path.split("/") if p]
        if parts[:2] == ["api", "conversations"]:
            rest = parts[2:]
            if not rest:
                if method == "POST":
                    return await self.forward(self.pick_for_new(), request)
                if method == "GET":
                    return await self.batch_get(request)
            elif rest == ["search"] and method == "GET":
                return await self.search(request)
            elif rest == ["count"] and method == "GET":
                return await self.count(request)
            elif rest == ["capacity"] and method == "GET":
                return await self.capacity(request)
            elif _conversation_hex(rest[0]) is not None:
                return await self.forward(self.owner(rest[0]), request)
        if parts[:2] == ["api", "settings"] and method not in ("GET", "HEAD"):
            return await self.broadcast(request)
        return await self.forward(self.workers[0], request)


def _sort_key(item: dict, sort_field: str) -> tuple:
    raw = item.get(sort_field)
    try:
        stamp = datetime.fromisoformat(str(raw)).timestamp()
    except ValueError:
        stamp = 0.0
    return (stamp, str(item.get("id", "")))


def _encode_cursor(cursors: dict[int, str | None]) -> str:
    raw = json.dumps({str(k): v for k, v in cursors.items()}).encode()
    return _CURSOR_PREFIX + base64.urlsafe_b64encode(raw).decode()


def _decode_cursor(page_id: str | None, count: int) -> dict[int, str | None]:
    """Per-worker cursors: "" starts a worker's listing, None means exhausted."""
    if not page_id or not page_id.startswith(_CURSOR_PREFIX):
        return dict.fromkeys(range(count), "")
    try:
        raw = json.loads(base64.urlsafe_b64decode(page_id[len(_CURSOR_PREFIX) :]))
    except ValueError:
        return dict.fromkeys(range(count), "")
    return {index: raw.get(str(index)) for index in range(count)}


def build_app(front: Front) -> Starlette:
    methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
    app = Starlette(
        routes=[
            Route("/health", front.health, methods=["GET"]),
            Route("/{path:path}", front.dispatch, methods=methods),
        ]
    )
    return app


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    # The front is what clients connect to: on Windows one aborted connection
    # would otherwise close its listener for good (PR #26).
    server_launch._install_windows_accept_loop_resilience()

    front = Front(
        state=Path(os.environ.get("AGENTRT_PERSISTENCE_DIR") or config.state_dir()),
        token=os.environ["AGENTRT_SESSION_API_KEYS_0"],
        count=config.workers(),
    )
    server = uvicorn.Server(
        uvicorn.Config(
            build_app(front), host=args.host, port=args.port, log_level="warning"
        )
    )

    async def run() -> None:
        await front.start()
        supervisor = asyncio.create_task(front.supervise())
        try:
            await server.serve()
        finally:
            supervisor.cancel()
            await asyncio.to_thread(front.stop)

    if os.name != "nt":
        signal.signal(signal.SIGTERM, lambda *_: setattr(server, "should_exit", True))
    asyncio.run(run())


if __name__ == "__main__":
    main()
