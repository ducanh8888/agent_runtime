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
