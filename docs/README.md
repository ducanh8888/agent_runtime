# Documentation index

This directory separates current operating guidance from plans, research and
immutable result records. Machine consumers should read
[`manifest.json`](manifest.json) instead of inferring authority from filenames.

## Start here

1. **[Orchestration guide](guides/orchestration.md)** — how to dispatch work and
   verify it, including what dispatching actually costs.
2. **[Architecture](reference/architecture.md)** — the processes, the state on
   disk, and a session's lifecycle.
3. **[Daemon behaviour](reference/daemon-behavior.md)** — what the API does not
   tell you, measured.

## By task

**Understanding the system**

- [Architecture](reference/architecture.md) — processes, boundaries, state
  layout, lifecycle, forking, workspaces.
- [Daemon behaviour](reference/daemon-behavior.md) — measured runtime behaviour:
  the chosen port, lifecycle verbs, the event log, admission and capacity,
  usage and cost.

**Using it**

- [Orchestration guide](guides/orchestration.md) — dispatching, verifying, and
  interrupting; why a thinking session and a stuck one look identical.
- [MCP and REST surface](#mcp-and-rest-surface) — see below.

**Permissions and security**

- [Permissions and what they actually enforce](reference/security-permissions.md)
  — the four presets, the path guard, the credential guard, and their known
  limits.
- [SECURITY.md](../SECURITY.md) — scope, threat model, and how to report.

**Operating and debugging**

- [Daemon behaviour](reference/daemon-behavior.md) — the first place to look
  when a call behaves unlike its schema.
- [Moving this to Ubuntu](guides/migration.md) — what is in git, what exists
  only on the machine, and how to restore it.
- [Friction log](research/friction-log.md) — defects ordinary use found after a
  phase closed. Read it before assuming a passing test means something works.
- [Docker reconnaissance](research/docker-recon.md) — container workspace work,
  with the trace of what was tried and what is still open.

**Contributing and internal design**

- [Testing](guides/testing.md) — which checks to run, narrow selections first,
  and the known environment-dependent failures.
- [Hardening plan](plans/deepseek-hardening.md) — the active plan: which items
  are done, which are open, and which were deliberately declined. This is the
  authority on project status, not any summary elsewhere.
- [Initial runtime plan](plans/initial-runtime.md) — the historical P1–P4 plan.
- [Self-audit and evidence](research/deepseek-hardening-audit.md) — the audit
  the active plan was built from.
- [Result records](results/) — immutable phase and baseline evidence
  (`h0`–`h7`, `p1`–`p4`, Linux baseline).

## MCP and REST surface

There is no separate API reference, deliberately. The MCP tool docstrings in
`packages/agentrt-runtime/agentrt/runtime/mcp_server.py` **are** the API
documentation: they travel with the tool into whatever agent is calling it,
which a document in this directory cannot do. For the REST surface, use the
daemon's own OpenAPI schema (`tools/api_context.py` prints the live surface),
and read [daemon-behavior.md](reference/daemon-behavior.md) for the behaviour
the schema does not state.

## Layout

| Directory | Purpose | Authority |
|---|---|---|
| `guides/` | Procedures for operators and developers | Current guidance |
| `plans/` | Active and historical implementation plans | Check manifest lifecycle |
| `reference/` | Current measured behavior and contracts | Current reference |
| `research/` | Audits, reconnaissance and decision history | Informative/historical |
| `results/` | Immutable phase and baseline evidence | Evidence for its revision |

## Naming and lifecycle contract

- Paths use lowercase kebab-case. Phase result IDs stay lowercase (`h0`, `p4`).
- `manifest.json` is the canonical document registry. Every Markdown document
  except this index must appear exactly once, and entries are sorted by `id`.
- `lifecycle` is one of `active`, `current`, `complete`, or `historical`.
- `authority` is one of `normative`, `reference`, `evidence`, or `informative`.
- Result records are append-only evidence. Correct broken links or metadata,
  but put new facts in a new result/current document rather than rewriting what
  an old revision observed.
- Use relative Markdown links. Do not add uppercase filenames, spaces,
  duplicated status indexes, or links to planned result files that do not exist.
- When moving or adding documentation, update `manifest.json`, root entrypoints,
  and every internal link in the same commit.
- Historical research may link into the optional, untracked `repos/` source
  corpus. That exception is declared in `manifest.json`; all other local links
  must resolve in a normal checkout.

Validate documentation changes from the repository root:

```bash
python3 tools/check_docs.py
```
