# Measured daemon behaviour

Endpoint names are not behaviour. Everything below was observed against the
running daemon, because each item is something a reasonable person would infer
wrongly from the name alone. `docs/api_context` output gives paths and field
names; this gives what they do.

Re-derive with `python tools/api_context.py` plus the probes described here if
the vendored server is ever updated.

## The port is chosen, not reserved

A restart can land on a different port (observed: 38887 → 40785 after the
2026-09-12 cutover). Clients read the port and token from `daemon.json` on each
call; anything that cached them from a previous process will talk to a dead
port. The stdio MCP servers are the ones to watch: they are long-lived children
of an orchestrator, so they hold both the code they started with and, for a
while, the port they first read.

## Lifecycle verbs

Observed transitions on a session executing a 50-second shell command:

| Call | HTTP | `execution_status` after |
|---|---|---|
| `POST /pause` | 200 | `paused` |
| `POST /run` | 200 | `running` |
| `POST /interrupt` | 200 | `paused` |
| `POST /events` with `run: false` | 200 | `paused` — unchanged |
| `POST /events` with `run: true` | 200 | runs, then `finished` |
| `POST /goal/stop` | 200 | unchanged |
| `POST /goal/resume` | **400** | `{"detail": "no_resumable_goal"}` |
| `DELETE /api/conversations/{id}` | 200 | session gone |

**`goal/*` is a different subsystem.** Those three endpoints serve objectives
started by `POST /goal` with an `objective` body. They are not the stop and
resume verbs for an ordinary conversation, and calling them on one fails.
Mapping a `stop` tool onto `goal/stop` by name — the obvious reading — produces
a tool that never works.

The real verbs are `pause` to suspend, `run` to continue, `interrupt` to cancel
what is executing right now.

### File-editor root is not the terminal's current directory

The guarded `file_editor` resolves writes against the dispatched workspace root;
changing directory in a terminal does not move that root. Never ask a session to
create its own sibling worktree and then work there through `file_editor`.
Pre-create the worktree and dispatch with `workspace` set to its path. An incident
on 2026-09-21 asked three sessions to create sibling worktrees from one shared
checkout; their changes instead mixed in that shared checkout.

### interrupt and pause are genuinely different verbs

Measured against an identical fixed script appending one line per second, so
the two runs compare the same command:

| Call | POST duration | Ticks before / just after / +15s | In-flight command |
|---|---|---|---|
| `interrupt` | 2.1s | 3 / 4 / 4 | killed |
| `pause` | **28.3s** | 3 / 31 / 46 | kept running |

Both end at `paused`, but only `interrupt` stops the work, and `pause` blocks
the caller until the agent reaches a safe boundary. A front end that presents
`pause` as "stop" must say that the call can take tens of seconds and that the
running command finishes regardless.

An earlier attempt at this comparison let each session compose its own loop and
measured two different commands; a third run with a script written in advance
produced the table above. The lesson is narrow but real: when timing is the
thing under test, the command has to be fixed, not described.

**`interrupt` really cancels in-flight work.** A session was set running a shell
loop appending one line per second to a file. At interrupt the file had 3 lines;
3 seconds later 3; 11 seconds later still 3. The child process is killed, not
merely orphaned while the agent loop stops.

**A step holds the session lock; `run` and `finalize` no longer wait on it.**
The run loop holds the conversation lock across a step, including a tool call.
A tool stuck on a hung command therefore held it indefinitely, and `POST /run`
on that running session, or `POST /finalize`, waited out the client's 120 s
timeout as if the daemon were down (issue #5). `run` now answers 409 at once
when a run is in flight (the client reports `already_running: true`).
`finalize` waits at most `FINALIZE_STEP_WAIT_SECONDS` (10 s) for the step to
let go, pausing under the lock when it does; otherwise it answers 409
`step_in_progress` and pauses nothing. `interrupt` does not take the lock and
remains the way to cancel such a step. Verified by server tests against a held
lock, not yet on a live hung command.

**`send` needs `run: true`.** The `run` field defaults to `false`, which appends
the message and leaves the agent where it was. Against a paused or finished
session that is a silent no-op: HTTP 200, message stored, nothing happens. Any
`send` that is meant to make the agent act must set it.

## Events — the transcript source

`GET /api/conversations/{id}/events/search`

- `limit` **caps at 100**. The OpenAPI schema carries `"lte": 100`, which is not
  a key FastAPI validates, so 101 or more raises inside the handler and returns
  **500**, not 422. A caller must clamp; the server will not.
- `sort_order` is an enum: `TIMESTAMP` (oldest first, the default) or
  `TIMESTAMP_DESC`. Lowercase `asc`/`desc` return 422.
- `page_id` is the cursor; a page carries `next_page_id`, null on the last page.
- Filters: `kind`, `source` (`agent`, `user`, `environment`), `body`
  (case-insensitive substring), `timestamp__gte` / `timestamp__lt`.
  **`kind` is the fully-qualified class path**, not the short name: the server
  compares `f"{cls.__module__}.{cls.__name__}"`, so
  `?kind=agentrt.sdk.event.llm_convertible.action.ActionEvent` matches and
  `?kind=ActionEvent` matches nothing. A wrong `kind` returns an empty page
  rather than an error, so this fails silently -- measure it rather than
  trusting a filter that looks right.
- `GET .../events/count` returns a bare integer.
- `GET .../events` (no `/search`) is a **batch fetch by id** and requires a
  request body; called bare it returns 422.

## Admission, capacity and the spend ledger

`POST /api/conversations` queues rather than refuses when the run pool is full:
the conversation is stored with `admission_state: queued` and a scheduler admits
it oldest-first. `GET /api/conversations/capacity` reports `running`, `limit`,
`available`, `queued`, `limiting_dimension` and the in-flight LLM count; a
disabled cap is reported as null rather than as a large number. Submissions may
carry an `idempotency_key`: the same key with the same submission returns the
existing conversation, a different submission is a 409.

`GET /api/conversations/spend-archive` is the lifetime ledger: deleting a
session folds its per-model token totals there first, so a lifetime total does
not fall when sessions are removed. `DELETE` on the same path is the only
operation that lowers it. A fold that fails is recorded in `unarchived` rather
than hidden.

## Several worker processes (`AGENTRT_WORKERS`)

One server process is one event loop, and in practice one core. Each step
re-sends and re-serializes a session's whole history, so about 50 long sessions
saturate it (#22). With `AGENTRT_WORKERS=N` (N > 1), `agentrt daemon start`
launches a front process (`agentrt.runtime.front`) on the usual port, and the
front starts N ordinary servers on private ports. Unset or 1 is the
single-process daemon.

- **Ownership is on disk.** Worker 0 keeps `conversations/`; worker k uses
  `conversations-wk/`. A conversation belongs to the worker whose directory
  holds it, and a fork stays with its source.
- **Routing.** New conversations are placed round-robin. Every
  `/api/conversations/{id}/...` call goes to the owner.
- **Merged reads.** `search` and `count` are merged across workers. `search`
  returns its own `page_id` cursor, so keep treating `page_id` as opaque.
  `capacity` is summed and adds a `workers` list.
- **Settings.** Writes under `/api/settings` go to every worker.
- **Restarts.** The front answers `/health` itself and restarts a worker that
  exits. That worker's in-flight runs come back as after a daemon restart; the
  other workers are unaffected.
- **The front dies, its workers die.** Linux uses `PR_SET_PDEATHSIG`; Windows
  puts each worker in a kill-on-close job object, so a front killed with
  `taskkill /F` no longer leaves its workers serving their directories beside
  the next front's (#24). Measured: two workers survived a hard kill before,
  none after. A worker's own children (its PowerShell terminals) are in the
  job too and end with it.
- **Report files** of a `readonly`/`inspect` session live under the owning
  worker's directory; `artifacts` looks there (#31).
- **Shared ledger.** Profiles, settings and the spend ledger stay shared in the
  state directory. Ledger updates take a file lock.

## Named models for specialized sessions (`AGENTRT_LLM_PROFILES`)

Set `AGENTRT_LLM_PROFILES` (process env or the state directory's `.env`) to
comma-separated `name=model` pairs at the same router, for example:

```bash
AGENTRT_LLM_PROFILES="audit=agentrt-audit,hard=agentrt-hard"
```

Profile names must consist of lowercase letters, digits, or hyphens and cannot
be `default`. `AGENTRT_HARD_MODEL` is still supported as shorthand for
`hard=<model>`.

For each configured name, the runtime:

- saves an LLM profile named `<name>` for that model;
- saves a copy of each permission preset bound to it (`<preset>-<name>`, such as
  `workspace-audit`), with the same tools and guidance as the preset;
- adds the model to the deployment policy's `extra_models`.

A dispatch with `llm_profile="<name>"` (e.g. `llm_profile="audit"`) runs the
whole session, and any fork of it, on that model, and tags the session with
`llm=<name>`. The model is chosen per session and never per call, so the
provider's prompt cache stays warm. Unset names are refused like any other
unknown LLM profile. `AGENTRT_HARD_CONDENSER_MAX_TOKENS` raises the history cap
for `hard` sessions only. Unset, sessions use the default cap.

Each call's usage record (`agentrt usage <session>`) carries
`provenance.router`, copied from OmniRoute's response headers. That records
which upstream actually answered (`x-omniroute-model`) and the router's
`x-correlation-id`. The combo fails over per call, so one session can be
served by several upstreams.

## Usage and cost

`metrics` on a conversation is **null**. Real figures are at
`stats.usage_to_metrics.<service_id>.accumulated_token_usage`, with
`prompt_tokens`, `completion_tokens`, `cache_read_tokens`, `reasoning_tokens`.

The direct DeepSeek candidate reports prompt, completion, cache-read and
reasoning tokens. Its current LiteLLM price table does not map
`deepseek-flash`, so `accumulated_cost: 0.0` means that no positive cost was
recorded, not that the call was free. H6 owns explicit unknown-cost semantics.

## Things that bite on Windows

Session titles are agent-generated and routinely contain emoji. Printing one to
a cp1252 console raises `UnicodeEncodeError`, so any front end that writes
session data to stdout must reconfigure it to UTF-8 first. For the same reason
every file the runtime reads back for a client (a report through `artifacts`)
is read as UTF-8 explicitly; the platform default there is cp1252.

A child process started inside the stdio MCP server inherits its stdin, which
is the JSON-RPC pipe. On Windows such a child blocks on it: `dispatch` with
`require="commit"` spawned `git` and hung until the client's 1800 s idle
timeout (#36). `agentrt-mcp` now moves the protocol onto a private descriptor
and points stdin at the null device before serving, so a child reads EOF;
the runtime's own children also pass `stdin=DEVNULL`. Measured through a real
stdio client: the same call hung past 20 s before, answered in 0.03 s after.

A repository created from an elevated shell is owned by
`BUILTIN\Administrators`, and git refuses it to any other account unless it is
in `safe.directory`. The server's readonly git runner drops global config, so
it used to refuse such a repository even when the user had trusted it, and
`snapshot`/`isolated_worktree` dispatch failed with "Not a git repository".
The user's `safe.directory` entries are now honoured (see
[security-permissions.md](security-permissions.md)).

A dispatched `workspace`/`broad` session's workspace gets a `.git` directory
created in it; a `readonly`/`inspect` one does not (next section).

## Event shapes

A 5-turn session produced 21 events in five kinds. Counts from that session:

| Kind | Source | n | Carries |
|---|---|---|---|
| `ConversationStateUpdateEvent` | environment | 9 | `key`, `value` |
| `ActionEvent` | agent | 5 | `tool_name`, `thought[]`, `reasoning_content`, `action`, `tool_call` |
| `ObservationEvent` | environment | 5 | `tool_name`, `observation.content[]` |
| `SystemPromptEvent` | agent | 1 | `system_prompt.text`, `tools` |
| `MessageEvent` | user or agent | 1 | `llm_message.role`, `llm_message.content[]` |

A sixth kind, `InterruptEvent`, appears only in a session that was actually
interrupted, so a probe that never interrupts will not see it. Treat the list
above as the kinds a normal run produces, not as exhaustive: condensation should
skip unknown kinds rather than fail on them.

Text is never a plain string. It is a list of content blocks, each
`{"type": "text", "text": ...}`, reached at `llm_message.content` on a
`MessageEvent`, `observation.content` on an `ObservationEvent`, and `thought`
on an `ActionEvent`.

### What a transcript must drop

Two fields dominate the payload and neither helps the orchestrator:

- `SystemPromptEvent.system_prompt.text` — kilobytes of fixed instructions,
  identical in every session.
- `ActionEvent.reasoning_content` — the model's private deliberation, routinely
  longer than the code it produced. **Dropped by default, available on request**
  (`transcript(..., include_reasoning=True)`, H8 item 10, 2026-09-17). The
  request form exists because the two fields differ in a way this section
  originally missed: `thought` is frequently *empty* on this deployment while
  `reasoning_content` on the same event is populated, so dropping the latter
  could leave a tool call in the transcript with no visible intent at all. The
  default is unchanged, so the cost this section describes is still not paid by
  a reader who does not ask.

`ConversationStateUpdateEvent` is internal bookkeeping — nearly half the events
here — and carries nothing a reader wants.

Returning events raw would put all of that into the context of whoever asked
for a transcript, which is the opposite of the point. Condensation keeps: user
and agent messages, each action as its tool name plus its short `thought`, and
each observation truncated.

One iteration is one call to `agent.step()` in the conversation run loop: one
agent step, normally one LLM response. Tool calls returned together in that
response are executed within the same step and do not each consume another
iteration. Each `run()` resets `iterations_used` to zero before looping, so a
`send` that starts a run gets a fresh per-run allowance.

## Iteration limits

`max_iterations` is optional for `dispatch`: the runtime sends 100,000 by
default (overridable with `AGENTRT_DEFAULT_MAX_ITERATIONS`), effectively no
turn limit for ordinary work. The conversation model's own default is 500, but
that is not the default used by a runtime dispatch. Set a smaller value only
when you need a hard per-run budget.

At the cap, `execution_status` is `"error"` with code
`MaxIterationsReached`, but the run returns its last usable output as a
`partial` result. You do not need to set `max_iterations` to get usable output:
`termination_reason: "cap"` identifies the cut, and a `resume_hint` on a
usable partial explains how to continue. `status`, `result`, and settled
`wait` items surface `termination_reason` and `condensation_steps` when
available; `iterations_used` counts work steps, not condensations. On older
daemons missing fields stay absent, rather than being reported as null.

There is no proactive warning before a cut: the former three-steps-before-cap
notice was removed by design in favor of partial output on exhaustion. The
budget is **per run**. A `send` after exhaustion starts a fresh allowance of
the same size, so a limit of 5 with three follow-ups is up to twenty steps.

## Tags

`tags` is a **`dict[str, str]`**, not a list. What happens when it is not
depends on the shape, and neither is a clean "that is not a tag":

- tags as a list, or a value that is a scalar with no length (`5`, `null`,
  `true`), is a **500** -- the validation walks the mapping and calls `len()`.
- a value that is itself a **list element** (`{"k": [1, 2]}`) is a sanitized
  **422**, because the shape check catches it first.

Either way a client should coerce rather than send a near-miss and hope.

`PATCH /api/conversations/{id}` **replaces** the whole map rather than merging
into it, so the obvious "add one tag" call silently drops every tag already
there. Merging means reading first.

`GET /api/conversations/search` returns `tags` on each item, so a listing can
show them without a request per row.

## Workspace kinds are a union that has to be imported

`BaseWorkspace` is a discriminated union keyed on `kind`. A request naming
`kind: "DockerWorkspace"` is rejected during validation with an `assertion_error`
whose message is **empty**, and the daemon log shows a validation error with no
other trace.

The cause is not a missing import, which is what this section used to say.
`StartConversationRequest.workspace` is typed as the **concrete**
`LocalWorkspace` (`agentrt/sdk/conversation/request.py`), not as the union, so
validation reaches `LocalWorkspace`'s own `kind` assertion rather than the
union's member list. Validating the same payload against `BaseWorkspace`
instead produces a *helpful* message naming the accepted kinds -- which is the
tell. The next section, on the create path being narrower than the read path,
is describing this same fact; the two are one thing, not two.

## The create path is narrower than the read path

`StartConversationRequest.workspace` is typed `LocalWorkspace`. The conversation
*response* model uses `BaseWorkspace`, which is what makes container workspaces
look supported from outside. Reading the OpenAPI schema is what settles it:

    "workspace": {"$ref": "#/components/schemas/LocalWorkspace-Input"}

So the daemon can describe a container workspace and cannot be asked for one.
`../research/docker-recon.md` records what happens when that field is widened.

## Workspace files

`GET /api/conversations/{id}/workspace/{path}` serves one file, 200, 404
`{"detail": "File not found"}` when absent. The content type is **guessed from
the filename extension** (`FileResponse` via `open_guarded_file_response`,
which passes no `media_type`): a `.md` file comes back `text/markdown;
charset=utf-8`, and a file with an unknown extension comes back
`application/octet-stream` rather than `text/plain`.

`GET /api/conversations/{id}/workspace` — the root — **does not list the
directory.** It is a static mount and answers 404 `{"detail": "No index.html in
directory"}`. No endpoint lists a workspace's *files*;
`GET /api/file/search_subdirs` lists **immediate subdirectories** only, which is
not the same thing and is not what `artifacts` needs.

So `artifacts` cannot enumerate over HTTP. It reads the workspace path from the
conversation record and walks that directory on the local filesystem, which is
sound here for the reason the daemon exists at all: it runs on this machine, and
the workspace is a real local directory. It reports only what changed since the
session started -- see below for why, and for what that misses. Content is still fetched through the
file route, so the daemon stays the one thing that reads session state.

The server resolves workspace paths and refuses traversal, symlink escapes and
aliases to its protected state files. Clients should still reject absolute and
escaping paths before constructing a URL so a normalising HTTP client cannot
change what the caller intended to address.

### The daemon runs `git init` in a writer's workspace

Starting a new `workspace` or `broad` conversation initialises the working
directory as a git repository if it is not one already, and does nothing if it
is. Observed, then confirmed in `event_service.py`: the `/api/git/changes`
endpoint needs a real repository to compute against. No commit is made.

A `readonly` or `inspect` conversation, and any resumed conversation (whose
agent the server does not know at that point), no longer does this (#38): a
read-only audit pointed at the parent of several repositories turned that
parent into an empty repository. The git routes answer `[]` for a directory
that is not a repository. This is an AgentRT edit in the vendored server; it
keys on the `permission` tool parameter the runtime's presets carry.

The runtime client does not create a missing workspace: `dispatch` refuses a
path that does not exist unless `create_workspace` is set (shared mode only), so
a typo is not turned into an empty repository (issue #3).

Two consequences. A directory a writer is pointed at for the first time acquires a `.git`,
which is why `artifacts` prunes that name. The current server exposes guarded
`/api/git/changes`, `/api/git/diff` and archive routes; older installed builds
may lack them, so `server_info`/version must be checked before relying on them.

It also settles how `artifacts` should answer "what did this session write". Git
would answer a different question: what differs from the last commit, which in a
real repository includes whatever the user had uncommitted before dispatching,
and in a freshly initialised one includes every pre-existing file as an
addition. Modification time against the session's `created_at` is narrower and
is the question actually being asked. What git would catch and this does not:
deletions, and copies that preserve timestamps.

### limit counts raw events, not condensed ones

Roughly half of a session's events are `ConversationStateUpdateEvent` and are
dropped, so a page of `limit` raw events yields far fewer. Measured: a 21-event
session returns 11 condensed. A caller asking for 30 typically receives 11 to
15, and `next_cursor` -- not the number returned -- is what says whether older
events remain.
