# AgentRT

**An MCP runtime that extends Claude Code with persistent, parallel background sub-agents.**

Claude Code remains the orchestrator: it plans the work, dispatches independent
pieces, checks the results, and decides what to merge. AgentRT runs those pieces
as daemon-owned sessions on your machine. Each session has an assigned workspace,
a configurable model endpoint, and an event log you can inspect after the
orchestrator exits. Codex can use the same MCP interface.

Use AgentRT when a task is long-running, can be split across workers, or needs a
result you can collect later. Keep short, conversation-local lookups with your
orchestrator's native sub-agents. AgentRT does not replace them; it adds a
separate runtime and lifecycle for background work.

## What it provides

- **Background sessions.** The local daemon holds session state independently of
  Claude Code or Codex. Close the orchestrator and reconnect later by session ID.
  A daemon restart interrupts in-flight work; its history remains available for
  recovery.
- **Parallel workers.** Dispatch independent tasks together with
  `dispatch_many`, then inspect capacity and collect each result. Actual throughput
  depends on your machine and endpoint.
- **Workspace isolation.** Use a dedicated writable Git worktree per writer, a
  detached snapshot pinned to a commit for review, or a shared directory when
  you intend to coordinate writes yourself.
- **Inspect and recover.** Read results, changed-file inventories, transcripts,
  read evidence, and usage; send follow-ups, interrupt, stop, resume, finalize,
  or fork a session. An agent's final report is a claim, not verification.
- **Bring your own endpoint.** Configure an API key, base URL, and model name for
  an OpenAI-compatible Chat Completions endpoint, directly or through a router.
  The endpoint owns provider/model selection and fallback; AgentRT does not.

### Compared with native sub-agents

Native sub-agents are convenient for bounded work within the orchestrator's own
workflow. AgentRT is useful when you want daemon-owned sessions you can address
again from a different client process, run against a separately configured
endpoint, or place in isolated worktrees with inspectable artifacts and recovery
controls. Both can be used in the same project. An AgentRT session incurs startup
and model spend, so it is not the right choice for every quick lookup.

## Architecture

```text
Claude Code / Codex / script  (plan, dispatch, verify)
       │ MCP (stdio)              │ CLI
       ▼                          ▼
   agentrt-mcp                  agentrt
       └──────────┬───────────────┘
                  │ authenticated local HTTP
                  ▼
          persistent local daemon
          ├── session A: workspace + agent + event log
          ├── session B: workspace + agent + event log
          └── ...
                  │ OpenAI-compatible Chat Completions
                  ▼
          configured provider or router (BYOK)
```

The daemon owns execution and persisted history; MCP and CLI are client surfaces,
not places where sessions live. `result`, `transcript`, and `read_evidence` project
from the event log. The daemon binds a local ephemeral port and authenticates
clients. See [architecture](docs/reference/architecture.md) and
[daemon behavior](docs/reference/daemon-behavior.md).

## Quick start

From a checkout of this repository, with [uv](https://docs.astral.sh/uv/)
installed:

```bash
uv tool install packages/agentrt-runtime    # installs agentrt and agentrt-mcp
agentrt config                               # find the state directory
```

Add the following to `<state_dir>/.env` (or set them in the process environment):

```dotenv
AGENTRT_API_KEY=your-endpoint-key
AGENTRT_BASE_URL=https://your-endpoint.example/v1
AGENTRT_DEFAULT_MODEL=your-model-id
```

Use the model name and base URL expected by your endpoint. Keep the `.env` file
outside the repository and do not commit credentials. You can run
`tools/omniroute_probe.py` to check whether a proposed endpoint handles the
agent's tool-calling loop before relying on it.

Start the daemon explicitly, then register the MCP server in your orchestrator:

```bash
agentrt daemon start
claude mcp add agentrt -- "$(command -v agentrt-mcp)"
# Or, for Codex:
codex mcp add agentrt -- "$(command -v agentrt-mcp)"
```

In Claude Code, ask it to split independent work into AgentRT sessions, dispatch
with isolated worktrees, and verify the changes before integrating them. MCP tool
descriptions include the dispatch, wait, verification, and recovery guidance.
The CLI exposes the same runtime for scripts and manual use:

```bash
agentrt dispatch "Make a focused change and commit it" \
  --workspace /path/to/git-repo \
  --workspace-mode isolated_worktree --require commit
agentrt wait <id>                    # run in the background for long tasks
agentrt result <id>
agentrt artifacts <id>
agentrt transcript <id>
```

The installed tool is a snapshot: after editing source, reinstall it with
`uv tool install packages/agentrt-runtime --reinstall`; a running daemon also
needs a restart to pick up changed code. Do not restart it while sessions are
running unless you intend to interrupt them.

## Working with sessions

1. **Split and dispatch.** Give each worker a self-contained task and a distinct
   workspace. `dispatch_many` accepts shared defaults for batches. For a Git
   repository, `isolated_worktree` creates a writable branch
   `agentrt/<session-id>`; `snapshot` creates a detached worktree pinned to HEAD.
   The default `shared` mode writes directly to the specified directory.
2. **Wait or monitor.** Background `agentrt wait <id> [<id> ...]` for a completion
   signal, or run `agentrt watch <id> [<id> ...]` for state changes. Sessions keep
   working without the waiting client.
3. **Verify.** Compare `artifacts` and the diff with the task, read `transcript`
   for the steps taken, and run your own checks. `result.completed_cleanly` says
   whether the final response was intact; it does **not** certify the code.
   For an isolated writer, verify that `agentrt/<session-id>` in the source repo
   advanced before merging: an agent can switch branches despite instructions.
   `--require commit` checks that worktree HEAD advanced, not which branch owns
   the commit.
4. **Recover or continue.** Check `status` and `result` for the current outcome.
   Send a correction, interrupt work in flight, stop at a safe boundary, resume
   a paused session, or fork a session with `dispatch_from`. A daemon restart
   marks interrupted runs as errors; their persisted history can be continued
   with `send`.

## Interfaces

**MCP tools:** `dispatch`, `dispatch_many`, `dispatch_from`, `list`, `status`,
`result`, `transcript`, `artifacts`, `read_evidence`, `usage`, `capacity`,
`control`, `finalize`, `profiles`. The stdio MCP server does not push completion
notifications; use a background CLI wait or monitor instead of holding an MCP
call open.

**CLI:**

```bash
agentrt list --limit 50
agentrt status <id>
agentrt result <id>
agentrt artifacts <id>
agentrt transcript <id>
agentrt usage <id>
agentrt watch <id> [<id> ...]
agentrt wait <id> [<id> ...]          # exit 0 when settled; 3 on timeout
agentrt send <id> "Continue with the failed test"
agentrt interrupt <id>
agentrt stop <id>
agentrt resume <id>
agentrt daemon status
```

IDs can be shortened to an unambiguous prefix. The CLI emits JSON by default;
`--text` requests a concise human-readable form. Use `agentrt --help` for all
options, including tags, context files, attachments, and session deletion.

## Permissions and security

| Preset | Terminal | File editor |
|---|---|---|
| `readonly` | no | view only; reports may be written outside the workspace |
| `inspect` | no | view only, plus guarded search, narrow Git and metadata reads |
| `workspace` (default) | yes | writes confined to the workspace by the file editor |
| `broad` | yes | unconfined |

**These presets are not an OS sandbox.** A terminal can access anything the
host user can access, regardless of the file editor's path checks. Treat
`workspace` and `broad` sessions as processes running with your user authority;
do not give untrusted tasks or credentials to them on the assumption that a
workspace path contains them. See [permissions and their limits](docs/reference/security-permissions.md)
and [SECURITY.md](SECURITY.md).

## Project status and development

AgentRT is open-source, pre-1.0 (`agentrt-runtime` 0.1.0), under active
development and used in real multi-session workflows. Interfaces and operational
behavior may change; check the [active plan](docs/plans/omniroute-migration.md)
and [documentation index](docs/README.md) for current status. To work on it:

```bash
./packages/.venv/bin/python -m agentrt.runtime.cli --help
./packages/.venv/bin/python -m pytest packages/tests/runtime/ -q
```

[AGENTS.md](AGENTS.md) documents the repository conventions and verification
requirements.

## Upstream relationship

AgentRT is a hard fork of [OpenHands' `software-agent-sdk`](https://github.com/OpenHands/software-agent-sdk)
at `f47083cc`, with `openhands.*` renamed to `agentrt.*`. The fork supplies the
agent loop, tools, workspaces, and agent server (`packages/agentrt-sdk`,
`agentrt-tools`, `agentrt-workspace`, and `agentrt-server`).
`packages/agentrt-runtime` — daemon, CLI, MCP surface, and permission guards —
is this project's own code.

## License

MIT — see [LICENSE](LICENSE), which retains the OpenHands copyright notice.
