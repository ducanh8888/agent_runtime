# Architecture

What AgentRT is made of, and where the boundaries are. Behaviour that is
measured rather than designed lives in
[daemon-behavior.md](daemon-behavior.md); the permission model has its own
[reference](security-permissions.md).

## The shape: a daemon, and thin client surfaces

```
orchestrator (Claude Code, Codex, a script)
        │
        ├── agentrt        CLI, machine-facing
        └── agentrt-mcp    MCP server, stdio
                │
                ▼
        daemon (one process, persistent)
        ├── session A  ── workspace, agent, event log
        ├── session B
        └── ...
```

The daemon owns the sessions. A client asks it to *do* things; it does not hold
session state itself. That is the design decision everything else follows from:
**a session outlives the process that started it.** Close your terminal, or
have the orchestrator exit, and the run continues; reconnect and collect the
result. Nothing in the client surfaces needs to be alive for a session to make
progress.

The cost of that choice is stated in [daemon-behavior.md](daemon-behavior.md):
the client and the daemon are separate processes behind a local HTTP API, so
there is no in-process context sharing, and a client that talks to a daemon
older than itself can misread a field it expects.

## Two processes, two installs

| Piece | Package | Role |
|---|---|---|
| `agentrt` / `agentrt-mcp` | `agentrt-runtime` | The project's own code: daemon, CLI, MCP surface, permission guard, profiles |
| daemon's server | `agentrt-server` | Vendored: REST/WebSocket API, persistence, admission, run completion |
| agent loop | `agentrt-sdk` | Vendored: conversations, events, tools, LLM layer |
| tools | `agentrt-tools` | Vendored: terminal, file editor, task tracker |
| workspaces | `agentrt-workspace` | Vendored: local and container workspaces |

Everything except `agentrt-runtime` is a renamed fork of OpenHands'
`software-agent-sdk`; see the root [README](../../README.md#upstream-relationship).
`agentrt-runtime` is the layer that turns the vendored server into a local,
MCP-addressable runtime.

The daemon is started on demand by the first client call (`agentrt daemon
start` does it explicitly). It binds an **ephemeral port** and writes it, with
a bearer token and its pid, into `daemon.json` in the state directory — so two
daemons cannot collide, and a client re-reads that file when a transport error
suggests the daemon restarted. See
[daemon-behavior.md](daemon-behavior.md#the-port-is-chosen-not-reserved).

## State on disk

Everything persistent lives under the state directory: `AGENTRT_STATE_DIR` if
set, otherwise `~/.agentrt`. `agentrt config` prints the resolved path.

```
<state-dir>/
├── daemon.json            port, token, pid
├── daemon.log             daemon log
├── .env                   provider credentials (not in the repo, never printed)
├── profiles/              LLM profiles
├── agent-profiles/        permission presets as profile files
└── conversations/<id>/
    ├── meta.json          workspace, permission, title, tags
    ├── base_state.json    conversation state
    └── events/            the event log, one JSON file per event
```

The event log is the session's real memory. `transcript`, `read_evidence` and
`result` are all projections over it, which is why they stay correct for
sessions that ran before a given feature existed.

## A session's lifecycle

A session is created by `dispatch` (or `dispatch_from`, `dispatch_many`) and
moves through:

| Status | Meaning |
|---|---|
| `running` | The agent is working |
| `finished` | The run answering the newest input completed |
| `paused` | Stopped and resumable — by `stop`, `interrupt`, or a finalize |
| `error` | Stopped without finishing |
| `stuck` | The stuck detector fired |

`status` reports the lifecycle status; `result` reports the *request* scope —
whether the newest consumed input has an answer yet (`pending`, `final`,
`partial`, `unavailable`). The two are deliberately separate: a session can be
`running` while the previous request's answer is already `final`.

The verbs are `send`, `interrupt`, `stop`, `resume`, `finalize`, `delete`.
`interrupt` cancels the in-flight step immediately; `stop` reaches a safe
boundary; `finalize` stops at a boundary and returns the outcome. All three
leave the session resumable, and each is recorded where the agent itself can
read it when it resumes.

## Sessions can inherit from sessions

`dispatch_from(session, task)` forks an existing session: the new session
inherits its event history, agent, workspace and permission, and starts from
that context instead of from a summary. Only the task and its metadata are the
caller's to choose.

Two bounds exist, for two different failure modes:

- `from_event_id` — copy only the branch up to that event. For a source that
  ran far past the point you want continued from. Event ids come from
  `transcript`. An id the source does not have, or a daemon that does not
  report the bound back, is refused rather than silently yielding a
  full-history fork.
- `AGENTRT_MAX_FORK_DEPTH` — the chain is bounded (default 3 generations,
  matching Codex's `agent_max_depth`), refused before the fork is requested, so
  a runaway chain costs nothing.

## Workspaces

`--workspace-mode` selects how the working directory is handed to a session:

- `shared` (default) — read/write the directory directly. Two sessions in one
  directory are subject to the shared-writer limit.
- `snapshot` — the directory must be a git repo; the daemon creates a detached
  worktree pinned to its current HEAD, so the session is isolated and
  reproducible against that commit. `status` reports the pinned commit as
  `workspace_resolved_sha`.
- `isolated_worktree` — a separate worktree rather than a pinned one.

A `readonly` or `inspect` session cannot write inside the workspace it was
dispatched into, but can write a report to a directory the daemon provisions
outside it. See [security-permissions.md](security-permissions.md).

## Waiting, and why it is bounded

`wait_any` / `wait_all` block until sessions settle. Two things about them are
worth knowing before relying on them:

- A session is reported settled only after the same terminal condition held
  across **two** samples, because `finished` is provisional while a stop hook
  or a late message can put the run back to `running`.
- A large `timeout` is capped internally
  (`AGENTRT_WAIT_SAFE_CEILING_SECONDS`, 900s by default) and returns
  `still_running` at that boundary rather than holding the call open, because a
  transport can have its own undocumented idle ceiling that fires first. Call
  again on `still_running`; do not raise `timeout` to work around it.

For anything expected to run long, prefer polling `status` between other work
over one held-open call.

## What the runtime deliberately does not do

- **No OS sandbox.** Permission presets constrain ordinary behaviour, and a
  terminal defeats path confinement. See
  [security-permissions.md](security-permissions.md).
- **No in-process context sharing.** A `dispatch_from` fork inherits another
  session's history; it never shares memory with its parent.
- **No push transport.** The stdio MCP transport cannot push, so completion is
  observed by blocking or polling, not by callback. The CLI's `wait` exists so
  an orchestrator can background a process and get a real exit signal instead.
