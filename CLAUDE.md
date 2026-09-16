# CLAUDE.md

[AGENTS.md](AGENTS.md) is the canonical instruction file for this repository:
layout and ownership, invariants, workflow, test/lint commands, rules for the
vendored trees, and the definition of done. Read it first. This file adds only
what is specific to running Claude Code against this checkout.

## Read order

1. [AGENTS.md](AGENTS.md) — the rules.
2. [docs/README.md](docs/README.md) and its machine-readable
   [manifest.json](docs/manifest.json) — the documentation registry.
3. The active plan named by `active_plan` in that manifest — what is being
   worked on, what is done, and what was deliberately declined.
4. `packages/AGENTS.md` and `packages/agentrt-server/AGENTS.md` before editing
   anything under those trees. Those are **upstream** guidance carried along by
   the fork, not this project's conventions.

## Two things about this checkout that bite

- **A running daemon executes the installed snapshot, not the source tree.**
  `uv tool install packages/agentrt-runtime` copies the code; a source edit is
  not live until `--reinstall` *and* a daemon restart. Before concluding that a
  fix did not work, check which copy the daemon is actually running. For a
  smoke test against source, start a separate daemon with its own
  `AGENTRT_STATE_DIR` rather than restarting the one in use.
- **Pyright resolves imports from `packages/`**, not the repository root.
  Running it from the root reports unresolved imports for code that is fine.

## Where the rest lives

- Which checks to run, and the failures that are environmental rather than
  yours: [docs/guides/testing.md](docs/guides/testing.md).
- What the daemon does that its API does not reveal:
  [docs/reference/daemon-behavior.md](docs/reference/daemon-behavior.md).
- Defects that ordinary use found after a phase closed, and why each rule in
  AGENTS.md exists: [docs/research/friction-log.md](docs/research/friction-log.md).
  Read it before assuming a passing test means something works.
- Container workspace work, which is open and has a recorded trace:
  [docs/research/docker-recon.md](docs/research/docker-recon.md).
