# Working in this repository

Canonical instructions for anyone — human or coding agent — changing this
repository. `CLAUDE.md` is a thin compatibility layer over this file; when the
two disagree, this one is right.

Two rules override convenience, and both have cost something to learn:

1. **A claim is not evidence.** Your own summary of what you did, a handoff
   note, and a review report are all claims. Check the diff, run the
   reproduction, read the file. Report faithfully when something is unverified
   or failed.
2. **Ask before changing a decision that is not yours.** Surface changes to the
   MCP tool set, anything that spends money, and anything that touches vendored
   code are the user's calls. Ask with a recommendation; do not infer consent
   from a previous answer.

## What this repository is

AgentRT is a local runtime for background coding-agent sessions: a daemon holds
sessions that outlive the client that started them, and exposes them over a CLI
and a stdio MCP server. `docs/reference/architecture.md` has the real
description; read it before making structural changes.

## Layout and ownership

```
packages/
├── agentrt-runtime/    OURS. daemon launcher, CLI, MCP server, permission
│                       guard, inspect tool, profiles, config
├── agentrt-sdk/        VENDORED. agent loop, conversations, events, LLM layer
├── agentrt-server/     VENDORED. REST/WebSocket API, persistence, admission
├── agentrt-tools/      VENDORED. terminal, file editor, task tracker
├── agentrt-workspace/  VENDORED. local and container workspaces
└── tests/              Test suites, by subsystem
tools/                  Check scripts and probes (see below)
docs/                   Documentation (see its README and manifest)
```

The boundary matters more than the directory names: `agentrt-runtime` is this
project; everything else is a renamed fork (see "Vendored and upstream code").
When a change looks like it belongs in the runtime but the only place to make
it is a vendored package, that is a decision to surface, not to take.

## Vendored and upstream code

`packages/agentrt-{sdk,server,tools,workspace}` are a hard fork of OpenHands'
`software-agent-sdk` at `f47083cc`, with `openhands.*` renamed to `agentrt.*`.
`packages/AGENTS.md` and `packages/agentrt-server/AGENTS.md` are **upstream
guidance carried along by the fork** — they describe the upstream project's
conventions and its monorepo, not this one. They still apply to the vendored
trees; they are not the rules for `agentrt-runtime`.

Consequences:

- **Editing vendored code is a decision for the user, not a step in a task.**
  Every such change is one you will have to carry across future upstream
  revisions. Propose it with the reason and the alternative first.
- Never change the `openhands.*` → `agentrt.*` rename's direction, and never
  remove upstream attribution or the MIT licence.
- A vendored docstring may describe upstream intent that this fork does not
  implement. Say which one you mean rather than "fixing" the docstring to match
  local behaviour.

## Invariants

Break one of these and the product stops being what it says it is.

- **Sessions outlive the client.** Nothing may make a session's progress depend
  on a client process being alive.
- **The event log is the record.** `transcript`, `read_evidence` and `result`
  are projections over it. Do not make behaviour depend on data that only
  exists in a live process.
- **Persisted events are history.** Existing events keep their meaning; add a
  new event rather than reinterpreting an old one.
- **Permission presets are not a sandbox.** Never describe them as containment,
  and do not weaken `agentrt/runtime/permissions.py` or `guarded_tools.py` for
  convenience — a guard that is subtly wrong is worse than none, because it is
  trusted.
- **The daemon binds an ephemeral port and authenticates every client.** Do not
  hard-code a port, and do not make a client read the state directory's
  credential files as a shortcut.
- **MCP tools return failures as data**, via `_guard`, not as raised
  exceptions: an orchestrator can read a dict and act on it, and cannot read a
  protocol error. Tool docstrings are the product documentation — they travel
  with the tool into whatever agent is calling it.

## Development workflow

The workspace venv is `packages/.venv`. There is no activate step:

```bash
./packages/.venv/bin/python -m agentrt.runtime.cli --help        # POSIX
./packages/.venv/Scripts/python.exe -m agentrt.runtime.cli --help # Windows
```

Installing instead (`uv tool install packages/agentrt-runtime`) gives `agentrt`
and `agentrt-mcp`, but **that copy is a snapshot**: source changes need
`--reinstall`, and a daemon already running keeps executing the old code until
it is restarted. Do not smoke-test a source change against a daemon you did not
start from source.

`docs/guides/testing.md` is the authority on which checks to run: narrow
selections first, full suites at the gate, and the known environment-dependent
failures so you do not chase one that was already there.

```bash
# tests
./packages/.venv/bin/python -m pytest packages/tests/<area>/ -q

# lint and format
./packages/.venv/bin/python -m ruff check <paths>
./packages/.venv/bin/python -m ruff format --check <paths>

# types — run from packages/, not the repository root
cd packages && ./.venv/bin/python -m pyright <paths>

# documentation
python3 tools/check_docs.py
```

Do not use mypy. The check scripts in `tools/` exercise the product end to end
(`cli_loop.py` the lifecycle, `adversarial.py` the permission presets,
`mcp_e2e.py` the MCP surface); they are worth running for changes that cross
process boundaries.

## Changing a runtime or MCP API

- Additive first. A new optional parameter does not break a caller; a changed
  default does, and is a decision to surface.
- A daemon and a client can be different versions, because they are different
  installs. If the client depends on a field the daemon may not send, check
  that it arrived rather than reading a missing key as a value.
- Update the docstring for the tool, not only the code: the docstring is what
  the orchestrator reads.
- Record the decision in the active plan (`docs/plans/deepseek-hardening.md`)
  when you ship it, including what you measured and what you rejected.

## Security-sensitive areas

`packages/agentrt-runtime/agentrt/runtime/permissions.py` (path guard),
`guarded_tools.py` (the file editor wrapper), `inspect_tools.py`, and anything
that reads the state directory. `docs/reference/security-permissions.md`
documents what is enforced and where it is intentionally weak — keep it true.

Never print, commit, or paste into a prompt: provider credentials, the
contents of a state-directory `.env`, an unredacted profile payload, or the
model's private reasoning (it is excluded from transcripts by default; the
opt-in path is deliberate).

## Documentation

- `docs/README.md` is the index; `docs/manifest.json` is the machine-readable
  registry. A new or moved document must be added there **in the same commit**,
  and entries are sorted by `id`.
- Lowercase kebab-case paths; `lifecycle` is one of `active`, `current`,
  `complete`, `historical`; `authority` is one of `normative`, `reference`,
  `evidence`, `informative`.
- Result records are evidence for the revision they describe. Do not rewrite
  their conclusions; add a new record instead.
- Planned work is never described as implemented. `docs/plans/` is a plan;
  `docs/reference/` is measured behaviour. Say which one a reader is in.
- Run `python3 tools/check_docs.py` before committing documentation.

## Before committing

1. The defect was reproduced, and the test fails without the fix. A test that
   passes both ways is a guard for the future, not evidence for this change —
   say which it is.
2. Tests, lint and types pass, and you know what you ran.
3. Anything crossing a process boundary was exercised end to end if it can be.
4. The plan doc reflects what is now true, including what is still open.
5. The commit message says what was measured and what was rejected, not just
   what changed.
6. Every commit carries both trailers:

```
Co-Authored-By: Claude Code <noreply@anthropic.com>
Co-authored-by: openhands <openhands@all-hands.dev>
```

## Definition of done

A change is done when someone who did not make it can verify it from the
repository alone: the reproduction is recorded, the tests fail without it, the
lane it sits in is documented, and what it does *not* cover is stated next to
what it does.
