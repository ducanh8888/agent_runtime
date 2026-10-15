from __future__ import annotations

import asyncio
import importlib
import sys

import pytest


server_launch = importlib.import_module("agentrt.runtime.server_launch")


@pytest.mark.skipif(
    sys.platform != "win32", reason="requires the Windows proactor loop"
)
def test_transient_accept_error_keeps_server_listening(monkeypatch):
    original_accept = asyncio.windows_events.IocpProactor.accept
    injected = False

    def fail_first_accept(proactor, listener):
        nonlocal injected
        if not injected:
            injected = True
            error = OSError(64, "The specified network name is no longer available")
            error.winerror = 64
            future = proactor._loop.create_future()
            proactor._loop.call_soon(future.set_exception, error)
            return future
        return original_accept(proactor, listener)

    monkeypatch.setattr(
        asyncio.windows_events.IocpProactor, "accept", fail_first_accept
    )
    if hasattr(server_launch, "_install_windows_accept_loop_resilience"):
        server_launch._install_windows_accept_loop_resilience()
    loop = asyncio.ProactorEventLoop()
    server = None
    try:
        server = loop.run_until_complete(
            loop.create_server(asyncio.Protocol, "127.0.0.1", 0)
        )
        loop.run_until_complete(asyncio.sleep(0.05))
        port = server.sockets[0].getsockname()[1]
        _reader, writer = loop.run_until_complete(
            asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), timeout=1)
        )
        writer.close()
        loop.run_until_complete(writer.wait_closed())
    finally:
        if server is not None:
            server.close()
            loop.run_until_complete(server.wait_closed())
        loop.close()
