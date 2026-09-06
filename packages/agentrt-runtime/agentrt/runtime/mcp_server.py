"""MCP server exposing the agentrt daemon to an orchestrator.

A thin front end: state lives in the daemon, which outlives this process. The
client is created lazily so importing this module has no side effects.

The docstrings below are the product's documentation. Nothing else reaches the
orchestrator -- a file under docs/ is never read by a session working in some
other repository, and an MCP resource is not loaded unless something asks for
it. Whatever a tool's description does not say, the caller has to guess.
"""

from threading import Lock

from agentrt.runtime import client as client_mod
from mcp.server.fastmcp import FastMCP

INSTRUCTIONS = """agentrt runs coding agents in background sessions on this
machine. A session survives this conversation ending: dispatch work, close the
orchestrator, come back later and collect the result.

Two things are worth knowing before the first call. A session is not cheaper
than doing the work yourself -- it costs roughly ten seconds of startup and its
own model spend, so it pays off on work that is long, separable, or worth
detaching from. And whatever a session reports about its own work is a claim,
not evidence; check the files it produced."""

mcp = FastMCP("agentrt", instructions=INSTRUCTIONS)

_client: client_mod.Client | None = None
_client_lock = Lock()


def _get_client() -> client_mod.Client:
    """Return the shared daemon client, starting the daemon on first use."""
    global _client
    if _client is None:
        with _client_lock:
            if _client is None:
                _client = client_mod.Client()
    return _client


def _guard(fn, *args, **kwargs) -> dict:
    """Run a client call, returning failures as data rather than raising.

    A raised exception reaches the orchestrator as a protocol error, which it
    cannot reason about. A dict it can read tells it what to do differently.
    """
    try:
        return fn(*args, **kwargs)
    except client_mod.ClientError as exc:
        return {"error": type(exc).__name__, "message": str(exc)}


@mcp.tool()
def dispatch(task: str, workspace: str, title: str | None = None) -> dict:
    """Start a background agent session and return immediately.

    The session keeps running after this conversation ends. Returns a
    short_id -- how you refer to the session in every other tool here.

    WHEN THIS IS WORTH IT. Dispatching costs about ten seconds of startup plus
    the session's own model spend. It pays off when the work is long, when
    several independent pieces can run at once, when it should outlive this
    conversation, or when you want it isolated with its own history. It does
    not pay off as a way to avoid work you could finish in a couple of tool
    calls yourself.

    WRITING THE TASK. The agent receives task and nothing else. It cannot see
    this conversation, your reasoning, or the file you were just reading. Brief
    it the way you would brief someone who has the repository but was not in
    the room:

    - State the finished condition, not the steps. "OUTPUT.txt contains the
      result of fizzbuzz(15), one entry per line" beats "run it and save the
      output".
    - Name the files it should create or change. Otherwise it invents names and
      you will not know what to look for.
    - Tell it how to check itself, and to report what the check printed. Ask
      for evidence and you get work that was tested; ask for nothing and you
      get work that looks finished.
    - Point at files rather than pasting their contents. The agent can read the
      repository; a path costs you a line, a paste costs you the whole file.
      Then say how far the reference goes -- "this file is complete for this
      task, do not verify it against the source". Calling a document
      authoritative says where it ranks, not that reading further is
      unnecessary. An agent that keeps checking can burn an hour confirming
      what you already told it and write nothing.
    - Say what it must not touch -- committing, pushing, files outside its
      remit -- if that matters. It will not infer your conventions.
    - Do not describe its tools. It has a terminal, a file editor and a task
      tracker, and it knows.

    WORKSPACE. An absolute path, created if missing. Give each session its own
    directory unless you specifically want them sharing one: nothing
    coordinates concurrent writes, so two sessions in one directory can
    overwrite each other silently, and sequencing them is your job.

    A session runs with the same authority you do. It can read anything you
    can, including credentials elsewhere on this machine, whatever workspace
    you name. Do not dispatch work you would not run yourself.
    """
    return _guard(_get_client().dispatch, task, workspace, title=title)


# Named `list` for the orchestrator but not defined as `list` here: a
# module-level rebinding of the builtin is a trap for anything added later.
@mcp.tool(name="list")
def list_sessions(limit: int = 20) -> dict:
    """List recent sessions, newest first.

    Returns id, short_id, title, status and timestamps for each. Use it to find
    a session whose short_id you no longer have, or to see what is still
    running before dispatching more work.
    """
    return {"sessions": _guard(_get_client().list_sessions, limit)}


@mcp.tool()
def status(session: str) -> dict:
    """Report one session's lifecycle state.

    session is the short id or the full UUID.

    The states you will see: running means the agent is working; paused means
    it was interrupted or stopped and can be resumed; finished means it stopped
    on its own; error means it failed, and the error key says why.

    Check this before result, which is null both while a session is still
    working and when a finished session had nothing to say.
    """
    return _guard(_get_client().status, session)


@mcp.tool()
def result(session: str) -> dict:
    """Return a session's closing summary.

    session is the short id or the full UUID. result is null while the agent is
    still working, so read status first to tell "still running" from "finished
    with nothing to say".

    THIS IS THE AGENT'S OWN ACCOUNT OF WHAT IT DID, NOT EVIDENCE THAT IT DID
    IT. An agent that says it verified its output has sometimes only said so.
    If the work matters, check it: read the file with artifacts, run the test,
    diff against what you expected. That is one tool call, and it is the
    difference between believing and knowing.
    """
    return _guard(_get_client().result, session)


@mcp.tool()
def transcript(session: str, limit: int = 30, cursor: str | None = None) -> dict:
    """Read what a session actually did, condensed.

    Returns events oldest first: messages, each action as its tool name and
    short intent, and each observation truncated. next_cursor pages backwards
    into older events; pass it back as cursor.

    limit counts raw events, and roughly half of those are internal bookkeeping
    that gets dropped, so you will get noticeably fewer than you asked for --
    limit=30 typically returns 11 to 15. It is capped at 100. When next_cursor
    is not null there are older events regardless of how few came back.

    The agent's private reasoning and its system prompt are excluded. They are
    the bulk of the raw payload and would cost you far more context than they
    are worth.

    Use this to find out why a session failed, or what it is doing now, when
    result is empty or does not explain itself.
    """
    return _guard(_get_client().transcript, session, limit=limit, cursor=cursor)


@mcp.tool()
def control(session: str, action: str, message: str | None = None) -> dict:
    """Change a running session's state.

    session is the short id or the full UUID. action is one of:

    - send -- give the agent a further instruction and let it act on it.
      Requires message. Works whether the session is running, paused or
      finished; a finished session starts working again.
    - interrupt -- cancel what the agent is doing right now. Any command it is
      running is killed. The session becomes paused with its history intact, so
      you can send a correction and then resume.
    - stop -- suspend the session at the next safe boundary. It reaches the
      same resumable paused state as interrupt but does not kill work in
      flight, so a command the agent is running carries on to completion and
      THIS CALL BLOCKS UNTIL THE BOUNDARY IS REACHED -- measured at 28 seconds
      against a long-running command, where interrupt returned in 2. Prefer
      interrupt unless you specifically want the current work finished.
    - resume -- continue a paused session from where it stopped. It does
      nothing to a session that already finished; to give a finished session
      more work, use send.
    - delete -- remove the session and its history permanently. This is the
      only action here that destroys anything and it cannot be undone. Files
      the session wrote in its workspace are left alone.

    Reach for interrupt when a session is going the wrong way: it is faster and
    cheaper than letting it finish, and its history survives, so you can
    redirect it rather than start again.
    """
    client = _get_client()
    if action == "send":
        if not message:
            return {
                "error": "MissingArgument",
                "message": "action 'send' requires `message`",
            }
        return _guard(client.send, session, message)
    if action in ("interrupt", "stop", "resume", "delete"):
        return _guard(getattr(client, action), session)
    return {
        "error": "UnknownAction",
        "message": (
            f"unknown action {action!r}; expected one of: "
            "send, interrupt, stop, resume, delete"
        ),
    }


@mcp.tool()
def artifacts(session: str, path: str | None = None) -> dict:
    """List or read the files a session produced.

    With no path, lists the session's workspace -- relative path, size and
    modification time for each file, skipping .git and __pycache__. With a path
    relative to that workspace, returns the file's content.

    This is how you check a session's work instead of taking its word for it.
    """
    return _guard(_get_client().artifacts, session, path=path)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
