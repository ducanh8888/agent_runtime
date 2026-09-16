# AgentRT

**A local runtime for background coding-agent sessions: dispatch a task, close
your terminal, come back later and collect the result.**

## Why this exists

Running a coding agent in your terminal ties the work to the terminal. Close
the laptop, lose the shell, or let the orchestrating process exit, and the run
dies with it — and long tasks are exactly the ones you do not want to babysit.

AgentRT moves the session into a small local daemon. The thing you talk to (a
CLI, or an MCP server inside your editor or agent) is a thin client; the
session is not. It keeps running across client restarts, and you collect it
later by id.

That makes it a good fit for work that is long, separable, or worth detaching
from the conversation that started it — a repo-wide review, a migration, a test
sweep, or a second opinion from a fork of an existing session.

It is **not** a sandbox and not a hosted service: it runs agents on your
machine, against your directories, using your provider credentials.

## Core capabilities

- **Persistent sessions.** Dispatch, exit, reconnect. Sessions survive the
  orchestrator process ending.
- **Full lifecycle control.** `send`, `interrupt`, `stop`, `resume`,
  `finalize`, `delete` — each leaving the session resumable, and recorded where
  the agent itself can see it when it resumes.
- **Session forking.** `dispatch_from` starts a new session from another one's
  event history, optionally bounded to a point in that history, so a reviewer
  can start from the writer's actual work rather than a summary of it.
- **Permission presets.** `readonly`, `inspect`, `workspace`, `broad`, with a
  path guard around the file editor. What each one really enforces is
  [documented precisely](docs/reference/security-permissions.md), including
  where a terminal defeats confinement.
- **Result and usage collection.** Request-scoped results, token/cost usage per
  session, and a condensed transcript of what the session actually did.
- **Artifacts.** Read what a session changed on disk, rather than trusting its
  own account of it.
- **Bounded waiting.** `wait` blocks until a session settles, with a documented
  safe ceiling instead of a call that hangs.

## Architecture at a glance

```
orchestrator (Claude Code, Codex, a script)
        │
        ├── agentrt        CLI, machine-facing
        └── agentrt-mcp    MCP server, stdio
                │
                ▼
        daemon (persistent)  ──  session: workspace, agent, event log
```

The daemon owns the sessions and binds an ephemeral local port. Everything
persistent lives in a state directory (`AGENTRT_STATE_DIR`, default
`~/.agentrt`), one event log per session.
[Architecture](docs/reference/architecture.md) has the process boundaries, the
on-disk layout and the lifecycle in full.

## Quick start

Needs [uv](https://docs.astral.sh/uv/) and a provider credential.

```bash
uv tool install packages/agentrt-runtime   # gives `agentrt` and `agentrt-mcp`

agentrt config                             # prints state_dir, and the resolved profile
cp .env.example <state_dir>/.env           # then fill in your provider credential
```

Then dispatch a task and come back for it:

```bash
agentrt dispatch "summarise the module layout, then run the test suite" \
  --workspace ~/src/my-project --title "layout review"
```

`dispatch` returns a short id. Exit whenever you like; the session keeps going.

## CLI

```bash
agentrt list                              # known sessions, newest first
agentrt status  4a7da681                  # lifecycle + request scope
agentrt wait    4a7da681 --timeout 300    # block until it settles
agentrt result  4a7da681                  # the answer, and how it ended
agentrt usage   4a7da681                  # tokens and cost for the session
agentrt artifacts 4a7da681                # files it actually changed
agentrt transcript 4a7da681               # condensed: what it did, step by step
agentrt interrupt 4a7da681                # stop now, keep it resumable
agentrt send    4a7da681 "now fix the two failures" 
agentrt delete  4a7da681                  # remove the session and its history
```

Ids may be shortened to an unambiguous prefix. `--text` is a top-level flag —
`agentrt --text status <id>` — and renders one line instead of JSON.

`agentrt wait` exists to be backgrounded: it exits **0** when every requested
outcome settled, **3** when the deadline ended the wait (so a timeout is a
successful call with an unfinished answer, not an error), and 1/2 when the call
itself failed. A script can check the exit code rather than parse output.

## MCP

`agentrt-mcp` is a stdio MCP server. Point a client at it and the tools appear:

```bash
claude mcp add agentrt -- "$(command -v agentrt-mcp)"
codex  mcp add agentrt -- "$(command -v agentrt-mcp)"
```

The registered tools are `dispatch`, `dispatch_from`, `dispatch_many`, `list`,
`status`, `result`, `usage`, `capacity`, `wait_any`, `wait_all`, `finalize`,
`transcript`, `read_evidence`, `artifacts`, `control`, `profiles`.

Their docstrings are the product documentation — they travel with the tool into
whatever agent is calling it — so start there rather than in `docs/`.

## Common workflows

**Detach a long job and collect it later**

```bash
agentrt dispatch "run the full suite and bisect any failure" --workspace .
# ... later, from anywhere
agentrt list && agentrt result <id>
```

**Get a second opinion on work in progress**

```
dispatch_from(session="<writer id>", task="read the diff and look for defects",
              from_event_id="<an event id from transcript>")
```

The fork inherits the writer's history and workspace, so the reviewer starts
from the work itself. Chains are bounded (default 3 generations).

**Review without letting it write**

```bash
agentrt dispatch "audit the dependency tree for CVEs" --workspace . \
  --permission readonly
```

A `readonly` session cannot write the workspace, but it can produce a report;
the daemon provisions a directory for that outside the workspace, and
`artifacts` reads it back.

**Pin a run to a commit**

```bash
agentrt dispatch "reproduce the reported failure" --workspace . \
  --workspace-mode snapshot
```

`snapshot` creates a detached worktree at the repository's current HEAD, so the
run is isolated and reproducible; `status` reports the pinned commit.

## Permissions

| Preset | Terminal | File editor |
|---|---|---|
| `readonly` | no | view only, inside the workspace |
| `inspect` | no | view only, plus structured `inspect` (search, narrow git, metadata) |
| `workspace` | yes | confined to the workspace |
| `broad` | yes | unconfined |

A terminal defeats path confinement, so `workspace` and `broad` constrain
ordinary behaviour rather than containing a determined one.
[Permissions and what they actually enforce](docs/reference/security-permissions.md)
is the honest version, including the credential guard's known limits.

## Project status

**Pre-1.0** (`agentrt-runtime` 0.1.0). Phases H0–H7 are complete, including a
production cutover; H8 (consumer-reported defects) and H9 (parity with native
sub-agent primitives) are partly implemented. The
[hardening plan](docs/plans/deepseek-hardening.md) tracks exactly which items
are done, which are open, and which were deliberately declined — read it rather
than inferring status from this line.

No release branches, no backports; changes land on `main`.

## Documentation

- [Documentation index](docs/README.md) — start here, grouped by task.
- [Architecture](docs/reference/architecture.md) — processes, state on disk, lifecycle.
- [Daemon behaviour](docs/reference/daemon-behavior.md) — what the API does not tell you.
- [Permissions](docs/reference/security-permissions.md) — what the guard enforces, and what it does not.
- [Orchestration guide](docs/guides/orchestration.md) — how to dispatch and verify.
- [Testing](docs/guides/testing.md) — which checks to run, and when.

## Development

```bash
./packages/.venv/bin/python -m agentrt.runtime.cli --help   # POSIX
./packages/.venv/Scripts/python.exe -m agentrt.runtime.cli --help   # Windows
```

The suite is run with the workspace venv; see
[docs/guides/testing.md](docs/guides/testing.md) for narrow selections, the
full gate, and the known environment-dependent failures.
[AGENTS.md](AGENTS.md) has the conventions, invariants and definition of done.

## Upstream relationship

AgentRT is a **hard fork** of [OpenHands'
`software-agent-sdk`](https://github.com/OpenHands/software-agent-sdk) at
`f47083cc`, with the `openhands.*` packages renamed to `agentrt.*`.

The fork supplies the agent loop, events, tools, workspaces and the agent
server (`packages/agentrt-sdk`, `agentrt-server`, `agentrt-tools`,
`agentrt-workspace`). `packages/agentrt-runtime` — the daemon, CLI, MCP
surface, permission guard and profiles — is this project's own code, and is
where essentially all of the work described above lives. Where the two differ
in behaviour, the vendored packages' own docstrings still describe their
upstream intent.

Upstream is MIT-licensed, and that licence and attribution are preserved.

## License

MIT — see [LICENSE](LICENSE), which retains the OpenHands copyright notice.
