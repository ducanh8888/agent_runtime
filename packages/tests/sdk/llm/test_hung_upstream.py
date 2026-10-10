"""AgentRT #15: a call to an upstream that stops answering fails after `timeout`,
not after the OpenAI client's hidden retries have each waited it out."""

from __future__ import annotations

import socket
import threading
import time

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
