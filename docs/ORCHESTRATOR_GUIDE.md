# Using agentrt — notes for the orchestrator

**The tool descriptions in `mcp_server.py` are the reference, not this file.**
They reach an orchestrator working in any repository; a file under `docs/` does
not. Operational guidance belongs there and is deliberately not repeated here.

This file holds what does not fit in a tool description: why the surface is
shaped as it is, and what using it has actually taught.

## The principle

Optimise the input, not the model's behaviour.

Instructions about how much an agent should think are the expensive, unreliable
lever. Telling a model to deliberate less does not make it correct; it makes it
guess sooner. The cheap lever is context: an agent that can see the interface it
must call does not have to reason its way to one, and reasoning is billed at the
same rate as code.

`tools/api_context.py` and `docs/DAEMON_BEHAVIOUR.md` exist for that reason. The
first extracts the daemon's live REST surface; the second records behaviour that
the surface does not reveal.

## What dispatching actually costs

Measured against `ds/deepseek-v4-flash`, with `tools/spend.py`:

| Session | Fresh input | Cached input | Output | Estimate |
|---|---|---|---|---|
| FizzBuzz, specified and verified | 19,438 | 61,184 | 2,364 | ~$0.008 |
| Seven methods, ran an hour, wrote nothing | 161,360 | 2,153,600 | 36,578 | ~$0.12 |

**Read prompt-token counts with caching in mind or you will reach the wrong
conclusion.** A session re-sends its transcript every turn, so a long one
accumulates enormous prompt totals — but 91% of them here were cache hits,
billed at roughly a tenth of fresh input. Pricing prompt tokens at one flat
rate made the second session look six times more expensive than it was, and
turned a 2.6x difference against single-shot generation into an apparent 130x.

The real figures are small. An hour of agent work costs on the order of ten
cents. Cost is therefore not the reason to be careful about what you dispatch —
convergence is. The second session above spent its hour re-reading the vendored
server to confirm facts it had already been handed in writing, and produced
nothing. What it wasted was an hour, not a budget.

## Pointing at a document is not enough

The obvious economy is to put reference material in the repository and name the
path, rather than pasting it into the task. That is right, and it is not
sufficient.

An agent given "read `X`, treat it as authoritative" read `X` and then spent
most of an hour verifying it against source anyway. "Authoritative" describes
the document's standing; it does not tell the agent that reading further is
unnecessary. Both have to be said:

> `docs/DAEMON_BEHAVIOUR.md` is complete and authoritative for this task —
> routes, limits, enum values, field shapes. Do not verify it against the
> server source.

The second sentence is the one that changes behaviour. Note that this is still
optimising the input: it supplies a fact the agent lacked — that the reference
is exhaustive — rather than instructing it to think less.

## Interrupt early

`interrupt` kills work in flight and keeps the history, so redirecting a session
costs one message. A session heading the wrong way will not correct itself, and
letting it finish to see what it produces is the expensive option. Watch
`transcript` early rather than waiting on `result`.

## Verifying an agent's work

A session's `result` is its own account of what it did. Sessions have reported
verifying output they had not verified. `artifacts` reads the files it actually
wrote; that is the check, and it costs one call.

That sentence was false for most of this project's life, and the reason it went
unnoticed is worth more than the bug. `artifacts` listed every file in the
workspace by path and returned the first two hundred. Every workspace used
during development was an empty scratch directory, where that is indistinguish-
able from the right answer. Point it at a real repository and it is not: with a
`.venv` at the root, all two hundred are dependency files and nothing the
session wrote appears, because `.` sorts before every letter. It now lists what
changed since the session started, newest first, and prunes dependency trees.

## Configuration and state

Configuration resolves from the process environment, then `<state-dir>/.env`,
then a `.env` in a development checkout. State — sessions, profiles, the
credential, `daemon.json`, `daemon.log` — lives in `%LOCALAPPDATA%\agentrt` on
Windows and `~/.agentrt` elsewhere.

`agentrt config` prints what is configured without revealing the key.
`agentrt daemon status` says whether the daemon is up and on which port.
`agentrt daemon logs` tails `daemon.log`, which holds server-side tracebacks
when a session fails for no visible reason.

## Fanning out, and outliving the process that dispatched

Sessions do survive the orchestrator exiting, which is the reason this runs as a
daemon at all. Verified rather than assumed, in `tools/probe_parallel.py`: three
sessions dispatched from threads, the dispatching process exits while they are
still running, and a second process that has never seen them collects all three
by id, reads their output, and finds each one confined to its own workspace.

Two practical notes from doing it. Dispatch is not instant -- three took twelve
seconds to start, because each call waits for the daemon to create the
conversation, so an orchestrator should start them from threads and not one at a
time. And a session id is the whole handoff: write the ids down and any later
process can pick the work up, which is what makes "dispatch and come back" real
rather than a manner of speaking.

A concurrent cap (`AGENTRT_MAX_SESSIONS`, default 5, non-positive means no
limit) refuses a dispatch when that many are already running. **It is read in
the process that dispatches, not on the daemon** -- for MCP that is the server
Claude Code spawns, so it belongs in the MCP entry's environment. The refusal
message used to point at the daemon, which is the one place setting it does
nothing.

## Test workspaces are not repositories

Two defects here had the same shape, and neither was subtle once seen.

`artifacts` listed every file in the workspace by path and returned the first
two hundred. In an empty scratch directory that is the right answer. In a
repository with a `.venv` at the root, all two hundred are dependency files and
nothing the session wrote appears at all.

The permission guard resolved a relative path against the daemon's own working
directory. In a scratch directory started from the same place, that is the right
answer too.

Both passed every test, because the tests were written in the same conditions
that hid the defect. Neither needed a cleverer check to find — only a workspace
that looked like somewhere real work happens. `tools/probe_artifacts.py` seeds a
virtualenv and pre-existing sources before dispatching, and
`tools/probe_paths.py` uses a directory with diacritics and a filename with a
space, for that reason.

The general form, worth applying to whatever comes next: a scratch directory
agrees with a repository about almost everything, so it cannot tell you which of
your assumptions were about agents and which were about your own tidy
workspace.

## Limits worth knowing

- **Only `readonly` actually contains a session**, and even that is confinement
  by path. It has no terminal and its file editor may view inside the workspace
  and nothing else. But a path names a file, and a file can have more than one
  name: a hard link inside the workspace to something outside it was readable
  through the guard — a `readonly` session read the provider credential that
  way, without being able to create the link itself. The guard now compares
  device and inode against the runtime's own credential files, so that specific
  leak is closed and nothing wider is. A link to any *other* file outside the
  workspace is still invisible, and on a filesystem that reports no inode the
  check does nothing at all. Confinement by path cannot see
  aliasing it is not told about, so the workspace you point a session at is part
  of its authority.

  `workspace` confines the file editor but still grants a terminal, and
  `python -c` opens any file the user can — it constrains ordinary behaviour,
  not a determined session. `broad` confines nothing.

  This is not a gap waiting to be closed in-process. A rule evaluated over
  shell command text is defeated by any interpreter the machine already has.
  Confining a session that has a shell needs a sandbox, which is a separate
  layer and deliberately out of scope.

- **Ambient plugins and file-based agents are off by default.** A plugin's
  hooks are shell commands, and discovery scanned the session's workspace, the
  enclosing git repository root, and the user's home directory — so dispatching
  into a repository that carried `.agents/plugins` ran its commands, whatever
  tools the profile granted. `AGENTRT_AMBIENT_PLUGINS=1` restores upstream
  behaviour; leave it unset unless you trust every directory you dispatch into.
- **Shared workspaces are not coordinated.** Two sessions in one directory can
  overwrite each other, and sequencing them is the orchestrator's job.
- **Sessions are kept until deleted.** Nothing expires.
