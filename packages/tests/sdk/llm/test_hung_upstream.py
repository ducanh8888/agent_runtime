"""AgentRT #15: a call to an upstream that stops answering fails after `timeout`,
not after the OpenAI client's hidden retries have each waited it out."""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from pydantic import SecretStr

from agentrt.sdk.llm import LLM, Message, TextContent
from agentrt.sdk.llm.exceptions import LLMTimeoutError


@pytest.fixture
def silent_server():
    """Accepts connections, reads nothing, answers nothing."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(16)
    held: list[socket.socket] = []

    def accept() -> None:
        while True:
            try:
                held.append(server.accept()[0])
            except OSError:
                return

    threading.Thread(target=accept, daemon=True).start()
    yield server.getsockname()[1]
    server.close()
    for conn in held:
        conn.close()


@pytest.fixture
def keepalive_server():
    """Streams SSE keepalives indefinitely, like an idle upstream response."""

    class KeepaliveHandler(BaseHTTPRequestHandler):
        requests = 0

        def do_POST(self) -> None:
            type(self).requests += 1
            length = int(self.headers.get("Content-Length", "0"))
            json.loads(self.rfile.read(length))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.end_headers()
            try:
                while True:
                    self.wfile.write(
                        b'data: {"id":"chatcmpl-keepalive",'
                        b'"object":"chat.completion.chunk","choices":[]}\n\n'
                    )
                    self.wfile.flush()
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def log_message(self, format: str, *_args: object) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), KeepaliveHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port, KeepaliveHandler
    server.shutdown()
    server.server_close()
    thread.join(timeout=1)


@pytest.mark.parametrize("stream", [False, True])
def test_a_silent_upstream_fails_after_one_timeout(silent_server: int, stream: bool):
    llm = LLM(
        model="openai/agentrt-worker",
        base_url=f"http://127.0.0.1:{silent_server}/v1",
        api_key=SecretStr("k"),
        timeout=1,
        num_retries=0,
        stream=stream,
    )
    started = time.monotonic()
    with pytest.raises(LLMTimeoutError):
        llm.completion(
            messages=[Message(role="user", content=[TextContent(text="hi")])],
            on_token=(lambda chunk: None) if stream else None,
        )
    # Three attempts at timeout=1 would take ~3.3 s.
    assert time.monotonic() - started < 2.2


@pytest.mark.asyncio
@pytest.mark.timeout(10)
async def test_streaming_keepalives_respect_total_timeout_and_retry(keepalive_server):
    port, handler = keepalive_server
    llm = LLM(
        model="openai/agentrt-worker",
        base_url=f"http://127.0.0.1:{port}/v1",
        api_key=SecretStr("k"),
        timeout=1,
        num_retries=2,
        retry_min_wait=0,
        retry_max_wait=0,
        stream=True,
    )

    started = time.monotonic()
    with pytest.raises(LLMTimeoutError):
        await asyncio.wait_for(
            llm.acompletion(
                messages=[Message(role="user", content=[TextContent(text="hi")])],
                on_token=lambda _chunk: None,
            ),
            timeout=7,
        )

    assert time.monotonic() - started < 4
    assert handler.requests == 2
