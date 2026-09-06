# Using agentrt — a guide for the orchestrator

You are the orchestrator. agentrt gives you background agent sessions that keep
running after your conversation ends. This is what it is for and how to use it
well.

## When to dispatch, and when not to

Dispatch when the work is **long, separable, and verifiable**. A session costs
about ten seconds of overhead before the agent's first action, so anything you
could finish in one or two tool calls is faster done yourself.

Good reasons to dispatch:

- The work will take longer than you want to sit and wait for.
- You want several independent pieces of work happening at once.
- The work should survive you closing the conversation.
- You want the work isolated in its own directory with its own history.

Bad reasons:

- To avoid a task you could do directly. A session is not cheaper than you.
- To parallelise work that touches the same files. Sessions share a workspace
  if you give them the same path, and nothing coordinates their writes.

## Writing a task

The agent gets your task text and nothing else. It cannot see your conversation,
your reasoning, or the file you were just looking at.

Write a task the way you would brief someone who has the repository but not the
discussion:

- **State the finished condition, not the steps.** "OUTPUT.txt contains the
  result of fizzbuzz(15), one entry per line" beats "run the function and write
  the output".
- **Name the files.** The agent will invent names otherwise, and you will not
  know what to look for.
- **Say how it can check itself.** "Verify by importing the function and
  comparing" produces work that has been tested. Without it you get work that
  merely looks finished.
- **Do not describe the tools.** It has a terminal, a file editor and a task
  tracker, and it knows.

## Workspace

`workspace` is an absolute path. It is created if missing.

Give each session its own directory unless you specifically want them sharing
one. Nothing coordinates concurrent writes: two sessions in the same directory
can overwrite each other silently, and sequencing them is your job.

Do not point a session at the agentrt repository itself. It contains `.env` with
the provider credential, and until permission profiles exist a session can read
anything you can.

## Reading back

`dispatch` returns immediately with a `short_id`. That id is how you refer to
the session from then on; the full UUID also works.

`status` tells you the lifecycle state. `finished` means the agent stopped on
its own; `error` means it failed and the message says why.

`result` returns the agent's closing summary. It is `null` while the session is
still working, so check `status` first to tell "still running" from "finished
with nothing to say".

**The result is the agent's own account of what it did. It is not evidence.**
An agent that says it verified its output has sometimes only said so. If the
work matters, check the artifacts yourself — read the file, run the test, diff
against what you expected. This costs you one tool call and is the difference
between believing and knowing.

## What it cannot do yet

- **No permission control.** Every session runs with the same authority you
  have. Do not dispatch work you would not run yourself.
- **No interruption or steering.** Once dispatched, a session runs to
  completion. You cannot redirect it or stop it mid-flight.
- **No listing through MCP.** `dispatch`, `status` and `result` only. Use
  `agentrt list` on the command line to see everything.

## When something is wrong

The daemon writes to `daemon.log` in the state directory
(`%LOCALAPPDATA%\agentrt` on Windows, `~/.agentrt` elsewhere). If a session
fails for no visible reason, that log holds the server-side traceback.

`agentrt daemon status` says whether the daemon is up and on which port.
`agentrt config` shows which model is configured, without revealing the key.
