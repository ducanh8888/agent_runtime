# Agent Runtime — Implementation Plan

Date: 2026-09-06.
Basis: [Pass 1](RESEARCH_PASS_1.md), [Pass 2](RESEARCH_PASS_2.md), [OSS Gap Reassessment](RESEARCH_OSS_GAP_REASSESSMENT.md), [Pre-Plan Decisions](PRE_PLAN_DECISIONS.md).

Target: the runtime is finished when it is usable day to day, not when the phase list is ticked off. Phases order the work; they do not define done.

Guiding rule: **reuse first, write only what no donor supplies.** Every task is labelled `REUSE` (runs as-is after vendoring), `PORT` (donor source adapted), or `NEW` (no donor exists). Anything not needed to reach a phase's done criteria is out of scope.

## Status

`CLAUDE.md` at the repository root is the entry point: where to start reading,
how to run the checks, and the working conventions this project was built with.

All four phases are closed. `P1_BASELINE.md`, `P2_RESULT.md`, `P3_RESULT.md` and
`P4_RESULT.md` record each one and are historical: they describe what was true
when the phase ended, not necessarily what is true now. For current behaviour
read the tool descriptions in `mcp_server.py`, which are the product's
documentation, then `DAEMON_BEHAVIOUR.md` for what the daemon does that its API
does not reveal, and `ORCHESTRATOR_GUIDE.md` for what using it has taught.
`FRICTION_LOG.md` collects the defects ordinary use found after the phases
ended, `DOCKER_RECON.md` the sandbox investigation.

`MIGRATION.md` covers moving to another machine: what is in git, what exists
only in the state directory, and what changes on Linux. `LINUX_BASELINE.md` is
what the checks actually measured once that move was made, and replaces
`P1_BASELINE.md` for any comparison run on Linux.

The rest are records of how the work got here and are not maintained against
the code: `RESEARCH_PASS_1.md`, `RESEARCH_PASS_2.md` and
`RESEARCH_OSS_GAP_REASSESSMENT.md` predate the plan, `PRE_PLAN_DECISIONS.md`
holds the decisions that shaped it, `P1_BASELINE.md` the vendored test baseline,
and `P4_RECON.md` what was probed before P4 was written. Read them for why
something is the way it is, not for what it currently does.

**Active follow-on plan (2026-09-11):**
[DeepSeek hardening](DEEPSEEK_HARDENING_PLAN.md) specifies H0–H7 for direct
DeepSeek with thinking enabled/high, reliable reads/results, waiting,
workspace isolation, batch admission and images/accounting. All H phases are
pending; this is planned work, not a claim about the installed runtime.
[The self-audit](DEEPSEEK_HARDENING_AUDIT.md) is its dated evidence and decision
record. The new plan supersedes this document's original exclusions only for
that follow-on scope (not cron, sandboxing or a second lifecycle owner). Keep
the P1–P4 sections and result reports below as history.

Built after the phases, on request: `max_iterations` on dispatch, session tags,
and installation as a package (`uv tool install`). One criterion was withdrawn
rather than met -- see section 8.

## 1. What is being built

A local agent runtime that Claude Code and Codex drive through MCP. Sessions run in the background, survive the orchestrator exiting, and can later be listed, inspected, steered, stopped, resumed and harvested.

```
Claude Code / Codex ──stdio──> agentrt-mcp ──┐
                                              ├─127.0.0.1+token──> agentrtd ──> 9Router
OS scheduler / operator ─────> agentrt CLI ──┘                    (daemon, owns      (only
                                                                   running sessions)  remote hop)
```

Everything is local except the provider call. `agentrtd` exists solely because a session must outlive the MCP process; it is a background process, not a hosted service. The CLI and the MCP server are two front ends over one client core.

## 2. Ownership

| Concern | Owner | Basis |
|---|---|---|
| Agent loop, tools, LLM calls, history, events, workspace, persistence, lifecycle | Vendored OpenHands (`agentrt.sdk`, `agentrt.server`, `agentrt.tools`, `agentrt.workspace`) | `REUSE` — Pass 2 §11 |
| Background execution and session registry | Vendored agent server | `REUSE` |
| Orchestrator-facing surface | `agentrt.mcp` and `agentrt.cli` over one client core | `NEW` — no donor |
| Daemon bootstrap, token auth, logging | `agentrt.daemon` | `NEW` on top of the `REUSE`d server |
| Permission enforcement | Guarded tool executors | `PORT` from Pydantic Harness + Deep Agents |
| Bounded executor / cancellation | Vendored `parallel_executor` | `PORT` from Deep Agents |

One owner per concern. No second agent loop, no second provider abstraction, no second event store.

## 3. Layout

`agent_runtime/` is the git repo (`main`).

```
docs/            the three research reports, PRE_PLAN_DECISIONS, this plan
packages/        vendored OpenHands, renamed to agentrt.*
  agentrt-sdk/  agentrt-server/  agentrt-tools/  agentrt-workspace/
src/agentrt/     client.py  daemon.py  cli.py  mcp/  policy/
tests/           our own tests; vendored tests stay inside packages/
```

Runtime state lives outside the repo and outside every workspace: `~/.agentrt` on Linux/macOS, `%LOCALAPPDATA%\agentrt` on Windows. It holds sessions, events, LLM profiles, provider keys, `daemon.json` (port, token, pid; mode 0600), and `daemon.log`.

## 4. Orchestrator surface — 8 tools

**Shipped as 8, not the 12 below.** The five rare lifecycle verbs collapsed into
one `control` tool taking an `action`, by decision: a flat core of the things an
orchestrator reaches for constantly, with the rest grouped behind one name. The
original twelve-row table is kept underneath because the *operations* did not
change, only how many tool names they occupy.

| Tool | Behaviour |
|---|---|
| `dispatch` | Required: `task`, `workspace`. Optional: `permission` (default `workspace`), `title`, `max_iterations`. Returns immediately with the short id. |
| `list` | Sessions with id, title, status, timestamps and tags. |
| `status` | One session: state, timestamps, workspace, `max_iterations`, tags. |
| `transcript` | Events condensed, oldest first, with a cursor for older ones. |
| `result` | The agent's closing summary — a claim, not evidence. |
| `artifacts` | What the session created or modified since it started, newest first; or one file's content. |
| `control` | `send`, `interrupt`, `stop`, `resume`, `delete`, `tag`. |
| `profiles` | The permission presets and what each grants. |

Two rows of the original design were not built as written. `dispatch` has no
`llm_profile`, `tools`, `skills` or `mcp_servers`: one profile per preset is
resolved inside the daemon, which is what keeps the credential out of the
dispatching process. And `artifacts` does not return "files the agent declared
in its final result" — a session's own list is a claim like any other, so it
reports what actually changed on disk since the session started.

<details>
<summary>The original twelve-tool design</summary>

One tool per operation. Session ids are short (`a3f9c1`); every tool also accepts the underlying UUID. The CLI exposes the same operations as subcommands, plus `daemon start|stop|logs`.

| Tool | Behaviour |
|---|---|
| `dispatch` | Required: `task`, `workspace`. Optional: `permission` (default `workspace`), `llm_profile`, `tools`, `skills`, `mcp_servers`, `title`. Returns immediately with the short id. |
| `list` | Sessions with id, title, status, timestamps. |
| `status` | One session: status, current activity, error if any. |
| `transcript` | Last N events condensed to text, plus a cursor for older or raw events. |
| `send` | Appends an instruction and returns immediately, stating it is queued for the next processing boundary. |
| `interrupt` | Cancels current execution so a new direction can be sent. History retained. |
| `stop` | Ends the session but keeps everything readable and resumable. |
| `resume` | Continues a paused, stopped or errored session. |
| `result` | Final response. |
| `artifacts` | Files the agent declared in its final result, plus content retrieval. |
| `delete` | Permanently removes a session. The only destructive tool. |
| `profiles` | Lists permission presets and LLM profiles so the model does not guess names. |

Default tool set inside a session: terminal, file editor, task tracker. Delegate, browser and MCP tools are attached only when `dispatch` asks. Skills come from the runtime's own directory and from the workspace; `~/.claude/skills` is not read.

</details>

## 5. Permission model

Three presets, `workspace` by default, each overridable per dimension:

- `readonly` — read inside the workspace, no writes, no shell.
- `workspace` — read/write inside the workspace, shell, git.
- `broad` — everything the process can do, minus dispatcher authority.

**Enforced in v1:** filesystem paths, shell availability, tool map, MCP availability. **Declarative only:** network and git — recorded on the profile and honoured by a sandbox layer if Docker is later added, but not enforceable in-process, since a rule over shell text is bypassed by `python -c`. P4's checklist does not test them.

Enforcement points, all available without a sandbox:

1. **Tool map** — the profile decides which tools exist. `REUSE`.
2. **Guarded executors** — path and access checks live directly inside the file editor and terminal executors, which is where the donor code was written to sit. `PORT` from Pydantic Harness (`_resolve_path`, `_check_access`, `_safe_resolve`, `_resolve_walk_entry`) and Deep Agents (`FilesystemPermission`, `_check_fs_permission`). One rule evaluator and one path normalisation, not two stacked: deny wins, and anything outside the allowed roots is denied. Upstream hooks are deliberately **not** used for policy — they only come in `command`, `prompt` and `agent` flavours, so each tool call would cost a subprocess or a model call.
3. **Skill inline commands** — reroute `skills/execute.py` through the same guarded terminal path instead of `subprocess.run(shell=True)`. Syntax preserved. `PORT` of the content/runner split from Microsoft `_skills.py`.
4. **Sub-agents** — a sub-agent receives the intersection of its definition and the parent profile. Its `hooks` field is stripped, as are hooks declared in the workspace or in skill metadata: hooks are shell commands, so they are an escalation path that no intersection rule covers. Workspace hooks are not loaded automatically today (`load_hooks_from_workspace` is only reachable through the `/hooks` endpoint), so this is a matter of never forwarding them. `NEW`, small.
5. **Dispatcher authority** — the daemon requires the token; the token lives outside every workspace and is stripped from tool environments; requests carrying `parent_conversation_id` are rejected; a worker's MCP config cannot point at agentrt. `REUSE` (`check_session_api_key`, `sanitized_env`) plus `NEW` guards.

Stated plainly: without a sandbox this constrains the model, not a determined process. A worker granted `broad` on a machine where the operator is administrator can reach the token. That limit follows from the authority granted, not from a missing technique. Docker remains an optional later layer.

## 6. Phases

Each phase ends in something runnable. Nothing outside a phase's done criteria is built in that phase.

### P1 — Vendor and rename

1. `git init -b main` in `agent_runtime`. Commit 1: move the four markdown files into `docs/`. Commit 2: pristine copy of `repos/software-agent-sdk` into `packages/`, unmodified, LICENSE kept, upstream ref `f47083cc` recorded.
2. Make the vendored tree installable on Windows before measuring anything. Upstream pins `litellm==1.93.0`, one of exactly three releases with no Windows wheel; its only Windows path is an sdist that builds a Rust extension. The pin moves to `1.93.1` — the next patch, which publishes `cp313-win_amd64` — in its own commit, and the `exclude-newer-package` cutoff moves with it.
3. Run the **full** vendored test suite from that commit and record pass/fail per test as the baseline, via `--junitxml` so the comparison is per-test rather than by summary line. Measured size: 695 files, roughly 8,200 test functions; `stress` and `acp_live` are deselected by upstream's own `addopts`. Environment failures are absorbed by comparing against the baseline rather than against zero — which matters, because upstream is evidently validated on Linux (Python 3.13 pin, Linux-only wheels, tmux) and a stable group of Windows failures is expected.
4. Rename `openhands.*` to `agentrt.*`: package names, imports, entry points, the `.openhands` config directory, environment-variable prefixes. Keep the four-package split.
5. `uv lock` — renaming workspace members invalidates their own lock entries; third-party pins survive.
6. Re-run the full suite.

**Done:** results match the baseline exactly. Any new failure is fixed or explained before P2 starts.

### P2 — Daemon, CLI and MCP walking skeleton

`NEW`, but thin — the daemon wraps the vendored server rather than reimplementing it.

1. `agentrt.daemon`: start the vendored agent server on `127.0.0.1:0`, generate a token, write `daemon.json` atomically under a lock, log to `daemon.log`.
2. Bootstrap inside the client core: read `daemon.json`, health-check, otherwise spawn detached (`DETACHED_PROCESS | CREATE_NO_WINDOW` on Windows, `start_new_session=True` on POSIX) and wait for readiness. Concurrent clients must converge on one daemon.
3. `agentrt.client`: the shared core. `agentrt.mcp` (stdio) and `agentrt.cli` are two thin front ends over it. This phase ships `dispatch`, `status`, `result`, plus `daemon start|stop|logs` on the CLI.
4. One LLM profile pointing at 9Router — Anthropic-compatible route for Claude models, OpenAI-compatible for the rest. Endpoint, model id and key are supplied at this step.
5. Register the MCP server in Claude Code and in Codex.

**Done:** from Claude Code, dispatch a real task, close Claude Code, reopen it, and see the session finished with a usable result. When the daemon fails to start, `daemon.log` says why.

### P3 — Full lifecycle

Mostly wiring over endpoints that already exist.

1. The remaining nine operations, on both front ends. `REUSE` for the underlying calls; `NEW` only for the surface layer.
2. Short-id to UUID mapping.
3. Transcript condensation plus cursor.
4. Artifact convention: the system prompt asks the agent to list important files in its final result; `artifacts` returns those and serves content through the existing workspace route.

**Done:** the full loop — dispatch, detach, list, transcript, send, interrupt, resume, result, artifacts — works from Claude Code. Codex is deferred; the MCP surface stays standard so adding it later needs no redesign.

### P4 — Permission, dispatcher boundary, cancellation

1. The five enforcement points from §5, in order. Section 5 names the donor for each, so this is porting and wiring, not design.
2. Bounded executor: `PORT` the Deep Agents pattern into the vendored `parallel_executor` — an instance-held executor with a semaphore instead of a per-call `with ThreadPoolExecutor`, so a timed-out worker holds a slot rather than blocking shutdown on the event loop.
3. A configurable cap on concurrent sessions; exceeding it fails `dispatch` with a clear message instead of degrading the machine.

**Done:** an adversarial checklist passes — a `readonly` session cannot write; a skill with an inline command cannot act outside its profile; a sub-agent definition planted in the workspace cannot widen authority, including through its `hooks` field; a `broad` session cannot create another session through shell, HTTP or MCP. Separately, a deliberately uncooperative tool does not delay control of an unrelated session.

## 7. Deliberately out of scope

Scheduling — the OS scheduler calls the CLI, so no cron belongs in the daemon. Also out: sandboxing, a 50–100 session benchmark, cost accounting and metrics, browser and MCP tools in the default tool set, session retention or garbage collection, cutting unused OpenHands modules, and any second lifecycle owner such as Temporal, DBOS or LangGraph.

## 8. Risks accepted

| Risk | Consequence |
|---|---|
| No sandbox on either OS | Profiles constrain the model, not a determined process |
| Network and git are declarative | A profile can record them, but nothing enforces them until Docker is added |
| Hard fork | Upstream fixes will not arrive; the vendored tree is ours to maintain |
| Windows has no tmux pane pool | Per-session terminal concurrency differs from Linux |
| Sessions are kept forever | The catalog grows until `delete` is used |
| A few identifiers keep the upstream spelling | `agent_kind`, `OpenHandsCloudWorkspace` and some enum values are legacy identifiers, not a half-finished rename |
| Shared workspaces are not coordinated | Two sessions on one repo can overwrite each other; sequencing is the orchestrator's job |
| A session can start another session | Withdrawn from P4's done criteria by decision. The daemon's token sits in `daemon.json`, so any session with a shell can read it and call the API directly; no in-process check closes that, only a sandbox would. See `P4_RESULT.md`. |
