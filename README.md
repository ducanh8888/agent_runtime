# AgentRT

**Bring-your-own-key sub-agents for Claude Code (and Codex), over MCP.**
Claude plans, splits and verifies the work; AgentRT runs it — in the
background, in parallel, on your own model keys or router.

## The idea

Claude Code's native sub-agents run inside the conversation, on the same
Claude quota, and end when the conversation ends. That is the right tool for a
quick, focused lookup. It is the wrong tool for twenty parallel coding tasks
that each take an hour.

AgentRT adds a second kind of sub-agent. Connect its MCP server and Claude gets
tools to **dispatch** work to background sessions, **watch** them, **check**
what they actually changed, and **recover** the ones that fail. The sessions
run in a local daemon on whatever OpenAI-compatible endpoint you configure — a
provider directly, or a router such as OmniRoute or 9Router that picks models
and rotates accounts for you.

Claude stops being the worker and becomes the orchestrator.

|  | Native sub-agent | AgentRT session |
|---|---|---|
| Runs on | Claude's quota | Your key / router (BYOK) |
| Lifetime | Ends with the conversation | Survives it; collect later by id |
| Parallelism | A few, inside one turn | As many as your machine and provider allow |
| Waiting | Automatic notification | `agentrt wait` in the background (one notification) or `agentrt watch` under a monitor (event stream) |
| Isolation | Shared checkout | Per-session git worktree or pinned snapshot |
| Checking the work | Trust the reply | `artifacts` and `transcript` show what really changed |
| Control | None once started | `send`, `interrupt`, `stop`, `resume`, `finalize`, fork |

Use both. Keep native sub-agents for short lookups; send long, separable or
parallel work to AgentRT.

## How it works

```
Claude Code / Codex  (orchestrator: plans, dispatches, verifies)
        │  MCP (stdio)
        ▼
agentrt-mcp ── agentrt CLI
        │  local HTTP, token-authenticated
        ▼
daemon (persistent) ── session: workspace, agent, tools, event log
        │  OpenAI-compatible Chat Completions
        ▼
your endpoint: provider or router (OmniRoute, 9Router, …)
```

The daemon owns the sessions, so closing Claude Code does not stop them. Each
session keeps a full event log; results, transcripts and artifacts are read
from it. Details: [architecture](docs/reference/architecture.md).

## Quick start

Needs [uv](https://docs.astral.sh/uv/).

```bash
uv tool install packages/agentrt-runtime          # installs `agentrt` and `agentrt-mcp`
agentrt config                                     # prints the state directory
```

Put your endpoint in `<state_dir>/.env` — three settings, always together:

```bash
AGENTRT_API_KEY=...                          # your provider or router key
AGENTRT_BASE_URL=https://your-router.example # or a provider's endpoint
AGENTRT_DEFAULT_MODEL=default                # model id as that endpoint names it
```

AgentRT does not choose, rotate or fall back between models; your endpoint
does. `tools/omniroute_probe.py` checks whether a model handles AgentRT's
tool-calling loop correctly before you rely on it.

Connect the orchestrator:

```bash
claude mcp add agentrt -- "$(command -v agentrt-mcp)"
codex  mcp add agentrt -- "$(command -v agentrt-mcp)"
```

The tool descriptions are the operating manual: an orchestrator that connects
learns from them how to fan out, wait, verify and recover. Nothing in `docs/` is
needed at run time.

## The orchestration loop

1. **Split and isolate.** One session per independent piece of work. Give each
   its own workspace: `workspace_mode="isolated_worktree"` or `"snapshot"`, or a
   worktree you create first. Do not ask a session to create its own worktree
   elsewhere — its file editor only writes inside the workspace it was given.
2. **Dispatch.** `dispatch` or `dispatch_many`, with a permission preset,
   optional shared `context_files`, and `require="commit"` when the task must
   end in a commit.
3. **Wait without blocking.** Run `agentrt wait <ids>` as a background command
   for one notification when everything settles, or `agentrt watch <ids>`
   under a monitor for one line per event.
4. **Verify.** A session's account of its work is a claim. Check `artifacts`
   (files actually changed), `transcript` (what it actually did) and
   `result` (`completed_cleanly` says whether the final answer is intact).
5. **Recover.** `status` names the state and the fitting action: `send` a
   correction, `interrupt` and redirect, `resume`, or fork a reviewer with
   `dispatch_from`.

## MCP tools

`dispatch`, `dispatch_many`, `dispatch_from`, `list`, `status`, `result`,
`transcript`, `artifacts`, `read_evidence`, `usage`, `capacity`, `control`,
`finalize`, `profiles`.

## CLI

```bash
agentrt dispatch "run the suite and fix failures" --workspace ~/src/app
agentrt wait  <id> [<id> ...]         # exits when settled: 0 done, 3 timeout
agentrt watch <id> [<id> ...]         # one line per state change
agentrt status | result | transcript | artifacts | usage  <id>
agentrt send <id> "now fix the two failures"
agentrt interrupt | stop | resume | delete  <id>
agentrt list --limit 50
```

Ids can be shortened to an unambiguous prefix. `--text` gives one-line output.

## Permissions

| Preset | Terminal | File editor |
|---|---|---|
| `readonly` | no | view only; may write a report outside the workspace |
| `inspect` | no | view only, plus structured search, narrow git and metadata |
| `workspace` | yes | writes only inside the workspace |
| `broad` | yes | unconfined |

A terminal defeats path confinement: `workspace` and `broad` constrain ordinary
behaviour, they do not contain a determined agent. AgentRT is **not a
sandbox** — it runs agents on your machine, in your directories, with your
credentials. [What each preset really enforces](docs/reference/security-permissions.md).

## Status

Pre-1.0 (`agentrt-runtime` 0.1.0), used daily for multi-session work. The
[active plan](docs/plans/omniroute-migration.md) and its predecessor
[hardening plan](docs/plans/deepseek-hardening.md) record what is done, what is
open and what was declined.

## Documentation

- [Documentation index](docs/README.md)
- [Architecture](docs/reference/architecture.md)
- [Daemon behaviour](docs/reference/daemon-behavior.md) — what the API does not tell you
- [Permissions](docs/reference/security-permissions.md)
- [Orchestration guide](docs/guides/orchestration.md)
- [Testing](docs/guides/testing.md)

## Development

```bash
./packages/.venv/bin/python -m agentrt.runtime.cli --help
./packages/.venv/bin/python -m pytest packages/tests/runtime/ -q
```

[AGENTS.md](AGENTS.md) has the conventions, invariants and definition of done.

## Upstream

AgentRT is a hard fork of [OpenHands'
`software-agent-sdk`](https://github.com/OpenHands/software-agent-sdk) at
`f47083cc`, with `openhands.*` renamed to `agentrt.*`. The fork supplies the
agent loop, tools, workspaces and agent server (`packages/agentrt-sdk`,
`agentrt-server`, `agentrt-tools`, `agentrt-workspace`).
`packages/agentrt-runtime` — daemon, CLI, MCP surface, permission guards — is
this project's own code.

## License

MIT — see [LICENSE](LICENSE), which retains the OpenHands copyright notice.
