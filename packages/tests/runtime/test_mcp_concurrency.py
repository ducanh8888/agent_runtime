"""#35/#36: one slow MCP tool call must not hold up the others."""

from __future__ import annotations

import threading
import time

import anyio

from agentrt.runtime import mcp_server


def test_a_slow_tool_call_does_not_block_another(monkeypatch) -> None:
    release = threading.Event()

    class _Client:
        def stop(self, session):
            release.wait(10)
            return {"id": session, "status": "paused"}

        def profiles(self):
            return {"presets": [], "allowed_llm_profiles": ["default"]}

    monkeypatch.setattr(mcp_server, "_get_client", lambda: _Client())

    async def scenario() -> float:
        started = time.monotonic()
        async with anyio.create_task_group() as group:
            group.start_soon(
                mcp_server.mcp.call_tool,
                "control",
                {"session": "abc", "action": "stop"},
            )
            await anyio.sleep(0.2)
            await mcp_server.mcp.call_tool("profiles", {})
            elapsed = time.monotonic() - started
            release.set()
        return elapsed

    assert anyio.run(scenario) < 2
