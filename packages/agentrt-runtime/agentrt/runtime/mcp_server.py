"""MCP server exposing the agentrt daemon to Claude Code.

This module is a thin front end: state lives in the daemon, which outlives this
process. The daemon client is created lazily so importing this module has no
side effects.
"""

from threading import Lock

from agentrt.runtime import client as client_mod
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("agentrt")

_client: client_mod.Client | None = None
_client_lock = Lock()


def _get_client() -> client_mod.Client:
    """Return the shared daemon client, starting it on first use."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = client_mod.Client()
    return _client


def _client_error(exc: client_mod.ClientError) -> dict:
    """Render a client failure as a tool result the model can read."""
    return {"error": type(exc).__name__, "message": str(exc)}


@mcp.tool()
def dispatch(task: str, workspace: str, title: str | None = None) -> dict:
    """Start a background agent session and return immediately.

    Give the task and workspace path; the agent begins work on the daemon and
    this function returns as soon as the session is created. The session keeps
    running after this conversation ends, so use this for work you do not need
    to block on. The returned `short_id` is how you refer to the session later
    in `status` and `result`. This call does not wait for the work to finish.
    """
    try:
        return _get_client().dispatch(task, workspace, title=title)
    except client_mod.ClientError as exc:
        return _client_error(exc)


@mcp.tool()
def status(session: str) -> dict:
    """Report one session's state.

    `session` accepts either the short id returned by `dispatch` or the full
    UUID. The returned dict tells you whether the agent is still running,
    finished, or failed. Use this before `result` to decide whether a final
    answer is available.
    """
    try:
        return _get_client().status(session)
    except client_mod.ClientError as exc:
        return _client_error(exc)


@mcp.tool()
def result(session: str) -> dict:
    """Return a session's final answer.

    `session` accepts either the short id returned by `dispatch` or the full
    UUID. The returned `result` key is null while the agent is still working,
    so check `status` first to tell "still running" from "finished with
    nothing to say".
    """
    try:
        return _get_client().result(session)
    except client_mod.ClientError as exc:
        return _client_error(exc)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
