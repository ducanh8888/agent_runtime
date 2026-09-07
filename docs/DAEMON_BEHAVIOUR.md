# Measured daemon behaviour

Endpoint names are not behaviour. Everything below was observed against the
running daemon, because each item is something a reasonable person would infer
wrongly from the name alone. `docs/api_context` output gives paths and field
names; this gives what they do.

Re-derive with `python tools/api_context.py` plus the probes described here if
the vendored server is ever updated.

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
- Filters: `kind` (e.g. `ActionEvent`, `MessageEvent`), `source` (`agent`,
  `user`, `environment`), `body` (case-insensitive substring),
  `timestamp__gte` / `timestamp__lt`.
- `GET .../events/count` returns a bare integer.
- `GET .../events` (no `/search`) is a **batch fetch by id** and requires a
  request body; called bare it returns 422.

## Usage and cost

`metrics` on a conversation is **null**. Real figures are at
`stats.usage_to_metrics.<service_id>.accumulated_token_usage`, with
`prompt_tokens`, `completion_tokens`, `cache_read_tokens`, `reasoning_tokens`.

`accumulated_cost` is **0.0** and stays there: 9Router returns no price, so cost
has to be computed from tokens by the caller. `tools/spend.py` does this.

## Two things that bite on Windows

Session titles are agent-generated and routinely contain emoji. Printing one to
a cp1252 console raises `UnicodeEncodeError`, so any front end that writes
session data to stdout must reconfigure it to UTF-8 first.

A dispatched workspace gets a `.git` directory created in it.

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
  longer than the code it produced.

`ConversationStateUpdateEvent` is internal bookkeeping — nearly half the events
here — and carries nothing a reader wants.

Returning events raw would put all of that into the context of whoever asked
for a transcript, which is the opposite of the point. Condensation keeps: user
and agent messages, each action as its tool name plus its short `thought`, and
each observation truncated.

## Iteration limits

`max_iterations` on the conversation model **defaults to 500**. There is no
unlimited session, only one whose ceiling nobody chose. Confirmed by reading it
back off six sessions: the two dispatched with an explicit limit report it, the
other four report 500.

Running out lands the session in `execution_status: "error"` -- not `finished`
-- and the daemon carries **no message for it**. There is no counter and no
error field in the payload, so a session that exhausted its steps and one that
genuinely failed are indistinguishable from `status` alone. What separates them
is the transcript: exhaustion ends mid-task after about that many steps.

The agent is not warned before the cut, and the budget is **per run**. A `send`
after exhaustion starts a fresh allowance of the same size, so a limit of 5 with
three follow-ups is up to twenty steps.

## Tags

`tags` is a **`dict[str, str]`**, not a list. A list is a 500 from the daemon,
and so is a non-string value -- both arrive as an opaque server error rather
than as "that is not a tag", so a client should coerce.

`PATCH /api/conversations/{id}` **replaces** the whole map rather than merging
into it, so the obvious "add one tag" call silently drops every tag already
there. Merging means reading first.

`GET /api/conversations/search` returns `tags` on each item, so a listing can
show them without a request per row.

## Workspace kinds are a union that has to be imported

`BaseWorkspace` is a discriminated union keyed on `kind`, and a member of it
exists only once the module defining it has been imported. The server imports
the local and remote kinds and nothing else, so a request naming
`kind: "DockerWorkspace"` is rejected during validation with an
`assertion_error` whose message is **empty**, and the daemon log shows a
validation error with no other trace. Importing the class anywhere in the server
process is enough; `agentrt.runtime.server_launch` does it before handing over.

## The create path is narrower than the read path

`StartConversationRequest.workspace` is typed `LocalWorkspace`. The conversation
*response* model uses `BaseWorkspace`, which is what makes container workspaces
look supported from outside. Reading the OpenAPI schema is what settles it:

    "workspace": {"$ref": "#/components/schemas/LocalWorkspace-Input"}

So the daemon can describe a container workspace and cannot be asked for one.
`DOCKER_RECON.md` records what happens when that field is widened.

## Workspace files

`GET /api/conversations/{id}/workspace/{path}` serves one file, 200 with
`text/plain; charset=utf-8` for text, 404 `{"detail": "File not found"}` when
absent.

`GET /api/conversations/{id}/workspace` — the root — **does not list the
directory.** It is a static mount and answers 404 `{"detail": "No index.html in
directory"}`. There is no listing endpoint anywhere on the daemon.

So `artifacts` cannot enumerate over HTTP. It reads the workspace path from the
conversation record and walks that directory on the local filesystem, which is
sound here for the reason the daemon exists at all: it runs on this machine, and
the workspace is a real local directory. It reports only what changed since the
session started -- see below for why, and for what that misses. Content is still fetched through the
file route, so the daemon stays the one thing that reads session state.

Path traversal has to be guarded in the client. The obvious probe is
inconclusive — an HTTP client normalises `../` out of the URL before the server
sees it — so a caller must reject absolute paths and anything that escapes the
workspace root after normalisation, rather than assume the server does.

### The daemon runs `git init` in the workspace

Starting a conversation initialises the working directory as a git repository if
it is not one already, and does nothing if it is. Observed, then confirmed in
`event_service.py`: the `/api/git/changes` endpoint needs a real repository to
compute against. No commit is made.

Two consequences. A directory pointed at for the first time acquires a `.git`,
which is why `artifacts` prunes that name. And the changes endpoint that would
justify it is **not exposed by this build** — the live route table has no
`/api/git/*` at all — so nothing here consumes the repository the daemon makes.

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
