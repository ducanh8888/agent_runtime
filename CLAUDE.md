# agentrt

A local agent runtime driven through MCP: background agent sessions that
survive the orchestrator exiting. A hard fork of OpenHands' software-agent-sdk
(`f47083cc`) with `openhands.*` renamed to `agentrt.*`, plus `agentrt.runtime.*`
which is the only code that is ours.

## Start here

1. `docs/IMPLEMENTATION_PLAN.md` — the `Status` block at the top maps all
   sixteen documents and says which are current and which are history.
2. `packages/agentrt-runtime/agentrt/runtime/mcp_server.py` — the tool
   docstrings **are the product's documentation**. Nothing under `docs/` ever
   reaches a session working in another repository, so operational guidance
   lives there and is deliberately not duplicated.
3. `docs/DAEMON_BEHAVIOUR.md` — what the daemon does that its API does not
   reveal. Hand this to a session you dispatch to work on agentrt itself.
4. `docs/FRICTION_LOG.md` — defects ordinary use found after the phases closed.
   Read it before assuming a test passing means something works.

## Running it

The workspace venv is `packages/.venv`. There is no activate step in use:

    ./packages/.venv/Scripts/python.exe -m agentrt.runtime.cli --help   # Windows
    ./packages/.venv/bin/python -m agentrt.runtime.cli --help           # POSIX

Installed instead: `uv tool install packages/agentrt-runtime` gives `agentrt`
and `agentrt-mcp`. The installed copy is a snapshot; source changes need
`--reinstall`.

Checks, in the order they are worth running:

    tools/cli_loop.py         the full lifecycle through the client core
    tools/adversarial.py      22 checks against the permission presets
    tools/probe_artifacts.py  artifacts against a repo-shaped workspace
    tools/probe_parallel.py   dispatch / collect, in two separate processes
    tools/mcp_e2e.py          the MCP surface, in-process

`tools/spend.py` reports cost per session, cache-aware. `tools/api_context.py`
prints the daemon's live REST surface.

## How this work is done

These are not style preferences. Each one cost something to learn and the
reasons are in `FRICTION_LOG.md` and the phase results.

**Probe before you write a spec.** Endpoint and field names mislead here.
`goal/*` is a different subsystem; `send` needs `run: true`; the workspace root
serves `index.html` rather than a listing; `max_iterations` defaults to 500 and
nothing says so. Every one of those was found by calling the thing, and would
have been wrong if reasoned about.

**A session's account of its work is a claim, not evidence.** Sessions have
reported verifying output they never verified. `artifacts` reads what actually
changed on disk; that is the check. The `result` tool description says this to
the orchestrator, and it applies to reports written by review sessions too — the
last one had nine findings, of which two did not survive testing.

**Measure before shipping a fix, especially a security fix.** Refusing any file
with `st_nlink > 1` closes a real credential leak and also refuses 30,656 of the
31,402 files in this project's own virtualenv. Counting took one command. The
instinct that a stricter guard is a safer one is what makes that question easy
to skip.

**A scratch directory is not a repository.** Two defects survived every test
because the tests shared the conditions that hid them: `artifacts` listed files
by path, correct for an empty directory and useless for a repo with a `.venv`;
and the path guard resolved relative paths against the daemon's own cwd. Probes
now seed a virtualenv and pre-existing sources before dispatching, on purpose.

**Optimise the input, not the model's behaviour.** Telling a model to deliberate
less makes it guess sooner. Giving it the interface it must call, and saying how
far the reference goes — "this is complete for this task, do not verify it
against the source" — changes what it does. Both sentences are needed; the
second is the one that works.

**Ask rather than guess.** When a decision is genuinely the user's — surface
changes, scope, anything that spends their money or touches their machine — ask
with a recommendation. Do not infer consent from a previous answer.

**Commit and push continuously**, with messages that say what was measured and
what was rejected, not just what changed.

## Constraints that were standing

- A spend cap was agreed at **$2 total including testing**; `tools/spend.py`
  reports against it. Roughly $0.85 had been spent when this was written.
- Other work runs on the same machine — a RAG project and its containers.
  Leave it alone.
- Cleaning test residue from state directories, installing Python
  dependencies, and starting or stopping the local daemon were all granted.

## What is open

**Docker.** `docs/DOCKER_RECON.md` ends with a container that starts correctly
and a daemon that cannot bind its published port — specific to Windows, where
docker is reached through a WSL proxy. On Linux that layer is gone, and the
experiment to repeat is written there. If it clears, `workspace` becomes a
preset that genuinely contains a session, and a P4 criterion that was withdrawn
becomes reachable. Both are currently documented as *not* true.

**The test baseline is Windows-only.** `docs/P1_BASELINE.md` records pass/fail
per test as measured there, and the plan's rule that results must match it
exactly does not transfer to Linux. Record a fresh baseline before comparing.

**Codex was never wired up.** The MCP surface is standard, so this needs no
redesign — only the config entry.

`docs/MIGRATION.md` covers moving to another machine: what is in git, what
exists only in the state directory, and what changes on Linux.
