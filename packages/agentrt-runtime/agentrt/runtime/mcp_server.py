"""MCP server exposing the agentrt daemon to an orchestrator.

A thin front end: state lives in the daemon, which outlives this process. The
client is created lazily so importing this module has no side effects.

The docstrings below are the product's documentation. Nothing else reaches the
orchestrator -- a file under docs/ is never read by a session working in some
other repository, and an MCP resource is not loaded unless something asks for
it. Whatever a tool's description does not say, the caller has to guess.
"""

from threading import Lock

from mcp.server.fastmcp import FastMCP

from agentrt.runtime import client as client_mod, config


INSTRUCTIONS = """agentrt runs coding agents in background sessions on this
machine. A session survives this conversation ending: dispatch work, close the
orchestrator, come back later and collect the result.

Two things are worth knowing before the first call. A session is not cheaper
than doing the work yourself -- it costs roughly ten seconds of startup and its
own model spend, so it pays off on work that is long, separable, or worth
detaching from. And whatever a session reports about its own work is a claim,
not evidence; check the files it produced."""

mcp = FastMCP("agentrt", instructions=INSTRUCTIONS)
# FastMCP takes no version argument, so the low-level server falls back to the
# installed `mcp` library's own version. Report this runtime's version instead,
# or every client's initialize response misidentifies the tool surface as the
# MCP SDK rather than AgentRT.
mcp._mcp_server.version = config.runtime_version()

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
def dispatch(
    task: str,
    workspace: str,
    title: str | None = None,
    permission: str | None = None,
    llm_profile: str | None = None,
    max_iterations: int | None = None,
) -> dict:
    """Start a background agent session and return immediately.

    The session keeps running after this conversation ends. Returns a
    short_id -- how you refer to the session in every other tool here.

    PERMISSION. One of `readonly`, `workspace` (the default) or `broad`; call
    `profiles` for what each grants.

    Choose `readonly` when the session only needs to look -- reviewing,
    summarising, answering a question about code. It is the only preset that
    cannot reach the provider credential, because it has no terminal at all.

    A `readonly` session cannot write its answer to a file. Ask it to report in
    its final message and read that with `result`; telling it to produce a
    report file gives it an instruction it cannot carry out. `artifacts` on such
    a session correctly lists nothing.

    `workspace` confines the file editor to the workspace but still grants a
    terminal, and a terminal can open any file you can. Treat it as constraining
    ordinary behaviour, not as containment.

    LLM_PROFILE. Optional name of the allowed LLM profile to run under, from
    `profiles`. It is a reference, not a credential -- the key never leaves the
    daemon. Defaults to the permission preset's own profile. An unknown or
    unbound name is refused rather than silently falling back to a weaker
    policy.

    MAX_ITERATIONS bounds one run of the agent. Left unset, the daemon applies
    its own default of 500 -- there is no such thing as an unlimited session
    here, only one whose ceiling you did not choose. Setting it lowers that.

    Measured, so you know what you are buying:

    - Running out puts the session in `error`, not `finished`, and nothing says
      why. There is no counter and no message: a session that exhausted its
      steps and one that genuinely failed report the same state. `status` shows
      the ceiling, and the transcript ending mid-task after about that many
      steps is what distinguishes them.
    - The agent is not warned. It is cut between steps, mid-task, with no chance
      to summarise -- so a session that stops this way has done part of the work
      and told you nothing about which part. Check `artifacts`.
    - The budget is per run, not per session. `control` with `send` starts a
      fresh allowance of the same size, so a limit of 5 and three follow-ups is
      up to twenty steps, not five.

    It is a circuit breaker, not a cost control: it stops a session that would
    otherwise run unattended, at the price of cutting it off mid-thought. For
    work you are watching, `interrupt` is better -- it keeps the history and
    lets you redirect. Reach for this when nobody will be watching.

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
    - Name the files it should create or change -- unless the preset is
      `readonly`, which cannot create any. Otherwise it invents names and you
      will not know what to look for.
    - Give line ranges, not just function names, when you point at part of a
      large file. An agent that reads a 737-line file and is then asked about a
      function by name has to find it again, and a review session spent two
      five-minute turns viewing wrong ranges before it was redirected. One
      `grep -n` before dispatching is cheaper than that.
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

    Unless the preset is `readonly`, a session runs with the same authority
    you do. Do not dispatch work you would not run yourself.
    """
    return _guard(
        _get_client().dispatch,
        task,
        workspace,
        title=title,
        permission=permission,
        llm_profile=llm_profile,
        max_iterations=max_iterations,
    )


# Named `list` for the orchestrator but not defined as `list` here: a
# module-level rebinding of the builtin is a trap for anything added later.
@mcp.tool(name="list")
def list_sessions(limit: int = 20) -> dict:
    """List recent sessions, newest first.

    Returns id, short_id, title, status, timestamps and tags for each. Use it
    to find a session whose short_id you no longer have, or to see what is
    still running before dispatching more work.

    Titles are auto-generated and repetitive, so after a few dozen sessions
    they stop telling them apart. `tags` is what does -- set them with
    `control` and read them here. Check them before deleting anything in bulk.

    `limit` is how many of the most recent to return, not a search. Sessions
    are kept until deleted and nothing expires, so an old one falls off the end
    of any listing you ask for; addressing it by short id still works, because
    that looks through all of them.
    """
    return {"sessions": _guard(_get_client().list_sessions, limit)}


@mcp.tool()
def status(session: str) -> dict:
    """Report one session's lifecycle state.

    session is the short id or the full UUID.

    The states you will see: running means the agent is working; paused means
    it was interrupted or stopped and can be resumed; finished means it stopped
    on its own; error means it stopped without finishing.

    `error` arrives with no explanation. The daemon carries no message for it,
    so there is usually no error key and nothing here says what went wrong. Two
    things distinguish the cases:

    - `max_iterations` is always here -- 500 unless you chose otherwise at
      dispatch -- so its presence tells you nothing on its own. What it gives
      you is the number to compare against: a session that ran out stops in
      `error` with a transcript that ends mid-task after about that many steps.
      No counter is exposed, so that comparison is the only signal.
    - Read `transcript` either way. A session that failed stops mid-work and its
      last events show where, and one that ran out looks like a task abandoned
      in the middle rather than one that went wrong. Server-side tracebacks go
      to the daemon log (`agentrt daemon logs`), not into this response.

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

    Expect fewer events than you asked for. limit counts raw events and about
    half of those are internal bookkeeping that gets dropped, so a long session
    returns roughly half of limit; a short one returns everything it has, which
    may be two or three. It is capped at 100.

    Judge "is there more" by next_cursor, never by how few events came back. A
    non-null cursor means older events exist no matter how short the page.

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
      the session wrote in its workspace are left alone. Read the session's
      tags first if you are deleting in bulk; nothing here stops you removing
      one you meant to keep.
    - tag -- attach notes to a session. Requires message, as `key=value` pairs
      separated by commas: `keep=evidence for the guide, round=3`. A key with
      an empty value removes it. Tags merge with what is already there, and
      show up in `list` and `status`.

      This is the only durable place to record why a session matters. Sessions
      are kept until deleted and a title is auto-generated, so after fifty of
      them a listing is fifty similar titles; a tag is what tells you which one
      is evidence and which is a probe you can throw away.

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
    if action == "tag":
        if not message:
            return {
                "error": "MissingArgument",
                "message": "action 'tag' requires `message`, as key=value pairs "
                "separated by commas",
            }
        parsed: dict[str, str] = {}
        for pair in message.split(","):
            pair = pair.strip()
            if not pair:
                continue
            key, sep, value = pair.partition("=")
            # A bare word is a key with an empty value, which `tag` reads as a
            # removal -- so it is rejected here rather than silently deleting
            # the tag the caller was trying to set.
            if not sep:
                return {
                    "error": "MalformedTag",
                    "message": f"{pair!r} is not key=value; write "
                    f"'{pair}=yes' to set it, or '{pair}=' to remove it",
                }
            parsed[key.strip()] = value.strip()
        return _guard(client.tag, session, parsed)

    if action in ("interrupt", "stop", "resume", "delete"):
        return _guard(getattr(client, action), session)
    return {
        "error": "UnknownAction",
        "message": (
            f"unknown action {action!r}; expected one of: "
            "send, interrupt, stop, resume, delete, tag"
        ),
    }


@mcp.tool()
def artifacts(session: str, path: str | None = None) -> dict:
    """List or read the files a session produced.

    With no path, lists what changed: every file in the workspace created or
    modified since the session started, newest first, with relative path, size
    and modification time. `since` is the cutoff it used. With a path relative
    to that workspace, returns that file's content.

    This is how you check a session's work instead of taking its word for it.

    Check `filtered`. It is true in normal use. False means the session's start
    time could not be read, so nothing was filtered and `files` is every file in
    the workspace rather than the session's -- which looks like a plausible
    answer and is not one.

    An empty `files` with a non-zero `total_scanned` is a real answer, not a
    failure: the session ran and wrote nothing. That is worth knowing early.
    `total_scanned` counts files outside the pruned directories, so it is not
    the size of the workspace -- a repository with a 400-file virtualenv in it
    reports 4.

    Four things it does not show. Deletions -- a file removed leaves nothing to
    list, so a session asked to remove something must be checked another way.
    Copies that preserve timestamps, which keep the original's time. And
    anything written inside a pruned directory: dependency trees and tool caches
    (`pruned` names them) are skipped, because a session that runs `npm install`
    would otherwise bury its own output under thirty thousand files.

    Fourth, it cannot tell your edits from the session's. It reports what
    changed, not who changed it, so anything you write in that workspace while
    the session runs appears in its list. Either leave the workspace alone until
    it finishes, or read the list knowing your own files are in it.
    """
    return _guard(_get_client().artifacts, session, path=path)


@mcp.tool()
def profiles() -> dict:
    """List the permission presets and the LLM references dispatch accepts.

    Call this before dispatching work whose authority matters, rather than
    guessing a preset name -- an unknown name is refused, not quietly widened.
    `allowed_llm_profiles` is the secret-free set of names `dispatch`'s
    `llm_profile` may select.
    """
    return _guard(_get_client().profiles)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
