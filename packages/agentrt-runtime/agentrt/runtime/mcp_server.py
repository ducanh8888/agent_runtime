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
    tags: dict[str, str] | None = None,
    idempotency_key: str | None = None,
    attachments: list[str] | None = None,
    workspace_mode: str | None = None,
) -> dict:
    """Start a background agent session and return immediately.

    The session keeps running after this conversation ends. Returns a
    short_id -- how you refer to the session in every other tool here.

    FIRST CALL ON A FRESH INSTALL. The daemon starts lazily, on the first
    dispatch, and a cold start imports the whole model stack -- this can take
    up to a couple of minutes before this call returns anything at all. That
    is expected, not a hang; it only happens once per daemon process.

    PERMISSION. One of `readonly`, `inspect`, `workspace` (the default) or
    `broad`; call `profiles` for what each grants.

    Choose `readonly` when the session only needs file views -- reviewing,
    summarising, or answering a question about code. Both read-only presets
    omit the terminal and guard aliases to the runtime's credential files.

    A `readonly` or `inspect` session cannot write its answer to a file. Ask it
    to report in its final message and read that with `result`; telling it to
    produce a report file gives it an instruction it cannot carry out.
    `artifacts` on such a session correctly lists nothing.

    Choose `inspect` for read-only repository audits that need structured
    search, narrow Git status/diff/log/show, version checks or sanitized
    environment metadata. It has no shell and cannot mutate files.

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
      `readonly` or `inspect`, neither of which can create any. Otherwise it
      invents names and you will not know what to look for.
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
    - Do not describe its tools. The selected permission profile already tells
      it which tools are available.

    WORKSPACE. An absolute path, created if missing. Give each session its own
    directory unless you specifically want them sharing one: nothing
    coordinates concurrent writes, so two sessions in one directory can
    overwrite each other silently, and sequencing them is your job.

    WORKSPACE_MODE. `"shared"` (default) is the above. `"snapshot"` needs
    `workspace` to be a git repository: the daemon creates a detached
    worktree pinned to its current HEAD and the session works there instead,
    isolated from anything else touching the real directory and reproducible
    against the exact commit it saw -- the pinned SHA comes back as
    `workspace_resolved_sha` on this response and on `status`. Use it to
    fan out several read-only reviewers into one repository without hand-
    rolling worktree setup and cleanup yourself; not needed for a single
    session or one you already gave its own directory.

    `workspace` and `broad` include a terminal and therefore run with the same
    user authority you do. Do not dispatch work you would not run yourself.

    TITLE AND TAGS are persisted at creation, before the first run. An explicit
    `title` is terminal: auto-titling is not scheduled for that session, so a
    later generated title cannot replace it. `tags` is a string-to-string map
    (keys lowercase: letters, digits, `_`, `-`). Use both on a fan-out -- the
    auto-title of fifty similar tasks is fifty similar titles, while tags are
    what tell them apart in `list`.

    `result_state` on the returned session is `pending` until a run consumes
    the input; `admission_status` is `queued` until then, so a freshly
    dispatched session is never reported as merely `idle`.

    ATTACHMENTS. `attachments` is a list of image paths to put on the first
    message. Each must be readable from the workspace, must sniff as PNG, JPEG,
    GIF or WebP from its own bytes (the extension is not consulted), and must be
    under a 5 MB cap. The session's model must have vision: otherwise the
    dispatch is refused, because sending it anyway would have the provider
    drop the image and the session answer confidently about something it never
    saw.

    CAPACITY. A full run pool does not refuse a dispatch: the input is
    persisted and the conversation is queued, then admitted when a slot frees.
    Read `capacity` for the backlog. `idempotency_key` makes a repeated
    submission return the conversation the first one created; the same key with
    a different submission is refused rather than silently replayed.
    """
    return _guard(
        _get_client().dispatch,
        task,
        workspace,
        title=title,
        permission=permission,
        llm_profile=llm_profile,
        max_iterations=max_iterations,
        tags=tags,
        idempotency_key=idempotency_key,
        attachments=attachments,
        workspace_mode=workspace_mode,
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

    The execution states you will see: running means the agent is working;
    paused means it was interrupted or stopped and can be resumed; finished
    means it stopped on its own; error means it stopped without finishing.

    REQUEST SCOPE, separate from whether the session is running:

    - `admission_status`: `queued` (input accepted, no run has stepped on it),
      `preparing` (a run started but has not completed a step), `admitted`.
      A freshly dispatched session reports `queued`, not ambiguous `idle`.
    - `result_state`: `pending` (the newest input has no answer yet), `final`,
      `partial` (a run stopped early), `unavailable` (no request boundary --
      the session never ran under this contract).
    - `iterations_used` / `iterations_remaining` count steps against
      `max_iterations` for the current run, so an `error` session that ran out
      of steps is distinguishable here without reading the transcript.

    `execution_status` still carries no message for `error`; read `result` for
    the sanitized code/detail and `transcript` for where it stopped.
    Server-side tracebacks go to the daemon log (`agentrt daemon logs`), not
    into this response.

    `workspace_mode`/`workspace_resolved_sha` appear when the session was
    dispatched with `workspace_mode="snapshot"`: the commit its detached
    worktree is pinned to, so you can check what a review actually ran
    against without a separate call. Absent for the default `"shared"` mode.
    """
    return _guard(_get_client().status, session)


@mcp.tool()
def result(session: str) -> dict:
    """Return the answer for a session's current request.

    session is the short id or the full UUID.

    `state` scopes the answer. `final` means the run answering the newest
    consumed input finished; `result` is its text, and an empty string is a
    valid final answer. `pending` means the newest input has no answer yet --
    `result` is null, and the previous request's answer is deliberately not
    returned in its place. `partial` means the run stopped early (error,
    iteration limit, stuck or pause); any text is partial. `unavailable` means
    the session never ran under request-scope tracking, so provenance is not
    claimed for it.

    Reported with it: `error` (sanitized code/detail for this request),
    `iterations_used` / `iterations_remaining`, `last_completed_tool`, and
    `last_progress_at` -- the timestamp of the last persisted event, which is
    durable progress and does not move during a long model turn. When the
    session's status is `error`, `progress_summary` also appears: a
    deterministic tool-call tally from the transcript ("file_editor x4,
    terminal x2"), not an LLM summary -- `finalize(summary=True)` is that,
    and spends a model call; this is free and available even after the run
    has already stopped.

    THIS IS THE AGENT'S OWN ACCOUNT OF WHAT IT DID, NOT EVIDENCE THAT IT DID
    IT. An agent that says it verified its output has sometimes only said so.
    If the work matters, check it: read the file with artifacts, run the test,
    diff against what you expected. That is one tool call, and it is the
    difference between believing and knowing.
    """
    return _guard(_get_client().result, session)


@mcp.tool()
def dispatch_from(
    session: str,
    task: str,
    title: str | None = None,
    tags: dict[str, str] | None = None,
) -> dict:
    """Fork a session and give the fork the task.

    `session` is the session to inherit from -- the fork gets its history, agent,
    workspace and permission, so a reviewer can start from the writer's
    conversation instead of from a summary of it. Only the task and its metadata
    are yours to choose.

    This is context inheritance as far as it goes here: the fork inherits
    *another AgentRT session*, not this conversation, which the daemon cannot
    read. The fork runs in the source's workspace, so two writers there are
    subject to the shared-writer cap; and a fork of a session that is still
    running copies the history as it stands, not the history it will have.
    """
    return _guard(_get_client().dispatch_from, session, task, title=title, tags=tags)


@mcp.tool()
def dispatch_many(tasks: list[dict], max_batch: int = 25) -> dict:
    """Submit several tasks once and get a per-item outcome.

    Each item is a dict of the arguments `dispatch` takes: `task` and
    `workspace` are required; `title`, `permission`, `llm_profile`,
    `max_iterations`, `tags` and `idempotency_key` are optional.

    Every item is validated before the first is created, so a malformed item
    cannot leave half a batch behind; the result separates `accepted` from
    `failed` with the reason for each failure. A full run pool is not a
    failure: accepted work is persisted and queued, and `capacity` reports the
    backlog. Submit once, then collect with `wait_all` -- do not re-dispatch
    what the daemon has already accepted.
    """
    return _guard(_get_client().dispatch_many, tasks, max_batch=max_batch)


@mcp.tool()
def usage(session: str) -> dict:
    """Report what a session's model calls consumed.

    Per service and per call: the raw provider token counts (prompt, completion,
    cache read, cache write, reasoning) and a normalized view derived from them.
    Read both. A provider that nests cache reads inside its prompt count is
    adjusted in the normalized view, and a field the stats owner did not record
    is reported as unavailable rather than as zero -- so "unknown" and "free"
    stay distinguishable.

    This is spend telemetry; it never changes the model, the effort or the run.
    No prompt, completion, reasoning content or credential is included.

    The projection is present on the conversation record for every session, so
    it works for sessions that ran before this tool existed.
    """
    return _guard(_get_client().usage, session)


@mcp.tool()
def capacity() -> dict:
    """Report how much work the daemon will admit right now.

    `running`, `limit` and `available` describe run slots; `queued` is accepted
    work waiting for one, in submission order. `in_flight_llm` and `llm_limit`
    describe concurrent provider requests when the deployment caps them
    (`AGENTRT_MAX_INFLIGHT_LLM`; null when it does not).
    `shared_writer_limit` / `busiest_workspace_writers` describe the cap on
    sessions writing in one shared directory (`AGENTRT_MAX_SHARED_WRITERS`; null
    when unbounded). Only AgentRT-managed writers are counted: editors and
    unrelated processes in the same directory never were. `limiting_dimension`
    names whichever cap is binding, or is null when none is -- and then
    `available` is null too, because "unbounded" is not a number to subtract
    from.

    The LLM slot is held for one transport call, not across a retry's backoff
    sleep: a request waiting to retry a 429 should not be occupying the capacity
    it is waiting to use.

    A full pool does not refuse a dispatch: the input is persisted and queued.
    This call is how you see that backlog instead of inferring it from refusals.
    """
    return _guard(_get_client().capacity)


@mcp.tool()
def wait_any(session_ids: list[str], timeout: float = 600.0) -> dict:
    """Block until one of these sessions settles, or the timeout elapses.

    This is the blocking wait of a foreground launch: dispatch several sessions,
    then wait once instead of polling `status`. `wait_all` is the same call that
    requires every id.

    The result groups ids by outcome:

    - `completed` -- finished; the item carries the same fields `result` does.
    - `partial` -- stopped with usable output (an error or limit after work).
    - `failed` -- terminal with no usable output.
    - `stopped` -- paused, so it can be resumed or finalized.
    - `missing` -- unknown or deleted; a wait for a deleted session does not
      wait forever.
    - `still_running` -- the timeout ended the wait. These are NOT failures and
      carry no partial output; wait again or read `status`.

    `timed_out` says whether the deadline, not completion, ended the wait. No
    outcome means "someone must approve this" -- the caller resolves every
    case.

    A session is reported settled only after the same terminal condition has
    held across two samples, because `finished` is provisional while a run may
    continue for a stop hook or a message that arrived during its final step.
    Expect at least one poll interval (about two seconds) of latency.

    Do not pass a large `timeout` expecting this call to hold open that long:
    it is capped internally (900s by default) well under the idle ceiling
    some transports between an orchestrator and this server impose on a call
    that sends nothing back for too long -- a `timeout` above the cap is
    truncated to it and returns `still_running` there, not held further. Call
    again on `still_running` rather than raising `timeout` to work around
    this. And prefer not holding your own turn on this call at all for
    anything expected to run long: poll `status` between other work, or
    background a poll loop, rather than blocking here.
    """
    return _guard(_get_client().wait, session_ids, mode="any", timeout=timeout)


@mcp.tool()
def wait_all(session_ids: list[str], timeout: float = 600.0) -> dict:
    """Block until every one of these sessions settles, or the timeout elapses.

    Same result shape as `wait_any`; see that description for the buckets and
    for the internal safe-ceiling cap on `timeout` -- it applies here too. A
    timeout returns the unfinished ids under `still_running` with `timed_out`
    true -- never as failures, and never with partial output presented as a
    final answer.
    """
    return _guard(_get_client().wait, session_ids, mode="all", timeout=timeout)


@mcp.tool()
def finalize(session: str, summary: bool = False) -> dict:
    """Stop a session now and return the outcome it has.

    session is the short id or the full UUID.

    This is the "wrap up now" verb. It lets the in-flight step reach a safe
    boundary, blocks any further tool starts, and returns the same shape `result`
    does, including `state`. It is not rollback: a command already running may
    still be running, so an external effect is reported as unknown rather than
    as undone. Repeating it for the same input returns the same outcome.

    `summary` asks for a tools-disabled wrap-up produced by a thinking/high
    call charged to the run's remaining iteration allowance. It is off by
    default and the daemon may refuse it. When no summary can run -- summaries
    disabled deployment-wide, or no allowance left -- the partial record is
    returned and `summary` is absent rather than invented. A run stopped by its
    iteration limit is already `partial`; this does not dress it up.

    `control` has no `finalize` action; use this tool.
    """
    return _guard(_get_client().finalize, session, summary=summary)


@mcp.tool()
def transcript(session: str, limit: int = 30, cursor: str | None = None) -> dict:
    """Read what a session actually did, condensed.

    Returns events oldest first: messages, each action with its tool, short
    intent, event id and -- for file actions -- the path and line range, each
    observation truncated with its event id, and each error with its sanitized
    code and detail. Errors used to be dropped here, which made a session that
    failed look like a clean stop. next_cursor pages backwards into older
    events; pass it back as cursor.

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
def read_evidence(
    session: str,
    limit: int = 100,
    max_pages: int = 50,
    cursor: str | None = None,
) -> dict:
    """Project what files a session read, from its persisted observations.

    This is derived, not stored: it reads the same successful `file_editor`
    view observations the transcript does, so it works for sessions that ran
    before this tool existed. It answers "did it actually read that file",
    which a session's own account does not.

    COST AND PAGING. It fetches up to `limit` raw events per request (capped at
    100 by the daemon) until the daemon runs out of events or it has made
    `max_pages` requests. A long session is several requests and this call waits
    for all of them, so raise max_pages deliberately. `complete` says whether
    the end of the event log was reached; when it is false, pass `next_cursor`
    back as `cursor` to continue.

    WHAT IT CAN AND CANNOT CLAIM. Each read reports three stages separately,
    because they are not the same thing:

    - `observed` -- the tool returned the content and the result was persisted.
      This is the only stage the evidence itself proves.
    - `delivered` -- the content was actually serialized into an LLM request.
    - `understood` -- the model acted on what it received.

    `delivered` and `understood` are `unknown` unless a paging writer recorded
    them on the event. A view that was observed but never delivered is a real
    failure mode, and this projection does not paper over it.

    RANGES AND EOF. `version`/hash, requested and returned line and character
    ranges, EOF and truncation come from optional metadata on the observation.
    Where a source event did not record one, the value is null -- never guessed.
    Ranges are merged only between reads with the same explicit `version`; a
    read with `version_known` false is never merged with another. FileEditor
    character offsets are line-relative and are grouped under
    `line_char_ranges`; partial-line spans are never promoted to whole-line
    coverage. `repeated_reads` flags the same returned span observed more than
    once, so ordinary continuation pages are not mislabeled as re-reads.

    Example use: after checking `artifacts`, call this to see whether the session
    read the file it claims to have reviewed, and whether it reached EOF or
    stopped partway.
    """
    return _guard(
        _get_client().read_evidence,
        session,
        limit=limit,
        max_pages=max_pages,
        cursor=cursor,
    )


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
      separated by commas: `keep=evidence for the guide, round=3,
      superseded-by=a1b2c3d4`. A key with an empty value removes it. Keys are
      lowercase alphanumeric, optionally hyphen-separated -- no leading,
      trailing or double hyphens, no underscores or uppercase. Tags merge
      with what is already there, and show up in `list` and `status`.

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

    `outcome` types the listing, so an empty answer is never mistaken for a
    failure. `empty` is a positive result: the filter ran and the session wrote
    nothing. It is distinct from `unavailable` (no workspace, or its path is not
    a directory here), `unfiltered` (`filtered` is false), `partial` (the walk
    or a `stat` failed; `scan_errors` counts how many), `truncated` (more than
    two hundred files matched) and `failed` (the walk itself raised). `complete`
    is true only for a fully filtered, untruncated scan. An empty `files` with a
    non-zero `total_scanned` is therefore an `empty` outcome, not a broken call.
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
