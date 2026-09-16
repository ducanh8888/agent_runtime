# Permissions and what they actually enforce

AgentRT runs coding agents against real directories, so it is worth being exact
about what a permission preset stops and what it does not. This page describes
the guard as it is implemented, including the cases where it is intentionally
weaker than the name suggests.

**The short version: a preset constrains an agent's ordinary behaviour. It is
not an OS sandbox, and with a terminal present it is not a security boundary.**
The vendored code's own docstrings say this, and it bears repeating next to the
presets, which would otherwise read as enforcing more than they do.

## The four presets

Set with `--permission` at dispatch (`readonly`, `inspect`, `workspace`,
`broad`). The default is `workspace`.

| Preset | Terminal | File editor | Notes |
|---|---|---|---|
| `readonly` | **no** | view only, inside the workspace | One of the two presets that keeps a session away from the provider credential |
| `inspect` | **no** | view only, inside the workspace | Adds one schema-validated `inspect` tool: structured search, narrow `git status/diff/log/show`, executable and environment metadata |
| `workspace` | yes | confined to the workspace | **A terminal defeats path confinement** — `python -c` opens any file the user can |
| `broad` | yes | unconfined | No confinement at all |

`readonly` and `inspect` are the two presets that may not change files. That is
enforced in the path guard (`check_path`) rather than by a shell allowlist, so
the file editor's own `writing` flag is what decides.

The `inspect` tool is a *guarded read runner*, not a sandbox: it refuses to read
the runtime's own credential, and every path it touches goes through the same
workspace guard. It does not widen the guard.

## What the file-editor guard does

The vendored `FileEditorExecutor` takes a `workspace_root` that reads like a
sandbox and is not one: the editor uses it to suggest paths in error messages,
and its `allowed_edits_files` check is skipped entirely for `view`. So the file
editor is confined by a wrapper in `agentrt.runtime.guarded_tools`, not by the
vendored code.

Two details make the wrapper meaningful rather than decorative:

- **The editor is handed the path that was checked**, not the string the agent
  passed. Handing over the original string and noting that `validate_path`
  passes paths through unchanged is true and not sufficient — the string checked
  and the file opened can still differ through a relative name or a component
  the OS normalises later.
- **A refusal is an observation, not an exception.** The agent can read an
  observation and choose differently; an exception would end the session, which
  turns a denied path into a crash.

Path resolution uses `resolve` rather than `normpath`, `commonpath` rather than
`startswith`, and `normcase` — the three ways this goes wrong on Windows being
`C:\ws` appearing to contain `C:\ws2`, a junction pointing elsewhere that
`normpath` never notices, and `C:\Foo` versus `c:\foo` being the same directory.

## Protecting the provider credential

A workspace is not supposed to be able to read the runtime's own credential
files. Path confinement alone does not achieve that, because a *hard link* is a
second name for one file and no path resolution reveals it: `resolve()` returns
the name it was given, that name is inside the workspace, and the bytes belong
to a file that is not. A workspace holding a link to the credential therefore
reads it through an approved path — demonstrated against a real `readonly`
session, which cannot create such a link but does not need to when the
directory it was pointed at already has one.

The guard closes that by comparing **device and inode** against the set of
runtime-owned files that may hold credentials or private agent state — `.env`,
`daemon.json`, `settings.json`, `secrets.json`, the profile and
provider-connection files, and per-conversation `meta.json` /
`base_state.json`. Identity rather than path, because identity is the thing a
second name cannot disguise. The set is recomputed per call, so a credential
rewritten after startup does not stop being recognised.

Three honest limitations:

- A **symlink retargeted between the check and the open** is a race this cannot
  win. Closing it needs a sandbox, not a better guard.
- On a filesystem that reports **no inode** — FAT, some network shares —
  `st_ino` is 0 and the hard-link protection is simply *off*, rather than
  weakened into something that looks like it is working. Such a volume gets the
  behaviour that existed before it: path confinement, blind to aliases.
- This protects the runtime's own credential files. It says nothing about the
  rest of the machine, which is what the terminal presets already concede.

## Reports from read-only sessions

A `readonly` or `inspect` session cannot write inside the workspace it was
dispatched into — but it can produce a report. The daemon provisions a
directory outside the workspace for exactly that, and the guard falls back to
it before refusing a write. `artifacts` reads it back the same way it reads
workspace files, so the caller does not need to know which root a file came
from. Presets that can already write the workspace get no reports root.

## Concurrent writers

Two sessions working in the same directory are subject to a shared-writer limit
(`AGENTRT_MAX_SHARED_WRITERS`). It counts only writers AgentRT manages, so an
editor or an unrelated process in the same directory is not counted and not
blocked. `capacity` reports the current state, including which cap is binding.

## Secrets

Provider credentials live in the state directory (`.env`, or the profile
files), never in the repository. AgentRT does not print them: `config` reports
provenance per setting with values redacted, and transcripts exclude the
system prompt and, by default, the model's private deliberation. Tools that
need a secret read it from the runtime's secret registry rather than from a
value pasted into a prompt.

## Reporting a vulnerability

See [SECURITY.md](../../SECURITY.md). This page describes what is enforced; it
is not a claim that the enforcement is sufficient for a hostile workload.
