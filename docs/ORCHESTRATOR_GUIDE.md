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

Measured on this project, one session against `ds/deepseek-v4-flash`:

| Session | Prompt tokens | Completion |
|---|---|---|
| Small, well-specified task (FizzBuzz, verified) | 80,622 | 2,364 |
| Seven methods against a documented interface | 857,156 | 17,744 |

The second session was interrupted before it wrote a line of code. It had spent
that budget re-reading the vendored server to confirm facts it had already been
handed in writing.

Prompt tokens dominate, and most of them are the session re-reading its own
context each turn. The lever that matters is therefore how many turns a session
takes, and the thing that most reliably inflates that number is an agent that
does not trust what it was given.

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

## Configuration and state

Configuration resolves from the process environment, then `<state-dir>/.env`,
then a `.env` in a development checkout. State — sessions, profiles, the
credential, `daemon.json`, `daemon.log` — lives in `%LOCALAPPDATA%\agentrt` on
Windows and `~/.agentrt` elsewhere.

`agentrt config` prints what is configured without revealing the key.
`agentrt daemon status` says whether the daemon is up and on which port.
`agentrt daemon logs` tails `daemon.log`, which holds server-side tracebacks
when a session fails for no visible reason.

## Limits worth knowing

- **No permission control yet.** Every session runs with the authority of the
  user who started the daemon. A session can read the credential in the state
  directory whatever workspace it is given. Moving the credential out of a
  repository reduces accidental exposure; it does not contain a session that
  goes looking.
- **Shared workspaces are not coordinated.** Two sessions in one directory can
  overwrite each other, and sequencing them is the orchestrator's job.
- **Sessions are kept until deleted.** Nothing expires.
