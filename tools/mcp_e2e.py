"""Drive the MCP server over stdio, the way an orchestrator does.

The MCP process inside a running Claude Code holds whatever code it was started
with, so the new surface cannot be exercised there without a restart. Spawning a
fresh server and speaking the protocol to it verifies everything except how the
host renders the result -- initialize, the tool list, the server instructions,
and each tool actually returning data.
"""
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO = r"C:\Users\ADMIN\Desktop\ORCHESTRATOR\agent_runtime"
PY = os.path.join(REPO, "packages", ".venv", "Scripts", "python.exe")


def payload(result):
    """Pull the JSON a tool returned out of its content blocks."""
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            try:
                return json.loads(text)
            except ValueError:
                return text
    return None


async def main() -> int:
    params = StdioServerParameters(
        command=PY, args=["-m", "agentrt.runtime.mcp_server"], cwd=REPO
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print("server      :", init.serverInfo.name, init.serverInfo.version)
            instr = init.instructions or ""
            print("instructions: %d chars -> %s"
                  % (len(instr), (instr[:70] + "...") if instr else "NOT SENT"))

            tools = await session.list_tools()
            names = sorted(t.name for t in tools.tools)
            print("tools       :", names)

            print("\ncalling each read-only tool for real:")
            listed = payload(await session.call_tool("list", {"limit": 5}))
            sessions = listed["sessions"]
            print("  list       -> %d sessions" % len(sessions))
            sid = sessions[0]["short_id"]

            st = payload(await session.call_tool("status", {"session": sid}))
            print("  status     -> %s (%s)" % (st["status"], sid))

            tr = payload(await session.call_tool(
                "transcript", {"session": sid, "limit": 20}))
            print("  transcript -> %d events, cursor=%s"
                  % (len(tr["events"]), tr["next_cursor"]))

            rs = payload(await session.call_tool("result", {"session": sid}))
            got = rs.get("result")
            print("  result     -> %s" % (repr(got[:50]) if got else "null"))

            ar = payload(await session.call_tool("artifacts", {"session": sid}))
            print("  artifacts  -> %d files" % len(ar["files"]))

            print("\nerror handling (must be data, not a protocol error):")
            bad = payload(await session.call_tool(
                "control", {"session": sid, "action": "explode"}))
            print("  bad action -> %s" % bad)
            nosend = payload(await session.call_tool(
                "control", {"session": sid, "action": "send"}))
            print("  send w/o msg -> %s" % nosend)
            nosess = payload(await session.call_tool(
                "status", {"session": "zzzzzzzz"}))
            print("  unknown id -> %s" % nosess)
    return 0


sys.exit(asyncio.run(main()))
