# Moving this to Ubuntu

Written before a Windows reinstall. An OS reinstall erases the disk, so the
question is not "what should be backed up" but "what exists only here".

## What is already safe

Everything under version control: the vendored tree, `agentrt.runtime.*`,
`tools/`, `docs/`. Pushed to `origin/main`. A clone restores all of it.

## What exists only on the machine

The state directory -- `%LOCALAPPDATA%\agentrt` on Windows, `~/.agentrt`
everywhere else. Nothing in it is in git, on purpose: two of its files hold the
provider credential.

| Path | Size | Carry it? |
|---|---|---|
| `.env` | 867 B | **Yes — holds the API key** |
| `profiles/default.json` | 4 KB | **Yes — holds the API key** |
| `agent-profiles/*.json` | 20 KB | Yes. They are regenerated on first run, but with *new* UUIDs, and conversations record the id they ran under. Carrying them keeps that link. |
| `conversations/` | 48 MB | Yes if the session history matters. Four sessions are tagged `keep` and are cited by name in `P3_RESULT.md`, `P4_RESULT.md` and `FRICTION_LOG.md`. |
| `daemon.log` | 7.2 MB | No. Append-only diagnostics. |
| `cache/` | 7.4 MB | No. Rebuilt. |
| `daemon.json` | 85 B | No. Port and token of a daemon that will not exist. |

The bundle built for this move contains the first four and excludes the rest.

## Restoring on Ubuntu

1. Clone the repository.
2. Copy the bundle's `state/` into `~/.agentrt/` -- the path changes, the
   contents do not. `config.state_dir()` picks `~/.agentrt` on any non-Windows
   platform, and `AGENTRT_STATE_DIR` overrides it if you want it elsewhere.
3. `chmod 600 ~/.agentrt/.env ~/.agentrt/profiles/default.json`. On Windows
   these inherit the user profile's ACL and `_tighten` is a deliberate no-op; on
   POSIX it runs, but only on files the runtime writes itself, so files restored
   from a bundle keep whatever mode the copy gave them.
4. `uv tool install packages/agentrt-runtime` from the clone.
5. Point the MCP client at `agentrt-mcp`. Nothing else is needed: the tool
   descriptions are the documentation and travel with the server.

Verify in this order, because each step's failure looks different:

    agentrt config              # credential resolves, from the state dir
    agentrt daemon status       # starts, reports a port
    agentrt dispatch "write OK.txt containing ok" --workspace /tmp/ws \
        --permission workspace --max-iterations 6
    agentrt artifacts <short-id>

Then run `tools/adversarial.py` (22 checks), `tools/cli_loop.py`,
`tools/probe_artifacts.py` and `tools/probe_parallel.py`. They are the fastest
way to find out what Linux does differently.

## What is expected to change, and what to do about it

**The test baseline is Windows-only.** `P1_BASELINE.md` records pass/fail per
test as measured here, and the plan's rule is that results must match it
exactly. That rule does not transfer: upstream is evidently validated on Linux,
and the Windows failures it absorbs will not reproduce. Record a fresh baseline
on Ubuntu before comparing anything against it.

**Docker is the one worth retrying immediately.** `DOCKER_RECON.md` ends with a
container that starts correctly and a daemon that cannot bind its published
port -- a failure specific to this machine, where docker is reached through a
WSL proxy. On Linux that layer is gone. The experiment to repeat is written
there: widen `StartConversationRequest.workspace` to `BaseWorkspace`, restart
the daemon, and dispatch. If it works, `workspace` becomes a preset that
genuinely contains a session, and the withdrawn P4 criterion becomes reachable
again -- both of which are currently documented as *not* true.

**Two fixes stop mattering and should stay anyway.** The `.cmd` resolution in
`execute_command` is a no-op off Windows, and the 240-second daemon startup
ceiling was measured against a cold Windows import that took over a minute. Both
are harmless on Linux and both describe why they exist.

**tmux exists.** The accepted risk "Windows has no tmux pane pool" stops
applying, so per-session terminal concurrency will behave as upstream intends.
This is the one place where Linux behaviour is *less* tested by us than Windows,
because everything here ran without it.

**The litellm pin stays.** `1.93.1` was chosen because `1.93.0` publishes no
Windows wheel. On Linux either works; moving the pin buys nothing and loses the
record of why it moved.

## Not part of this bundle

Other work on this machine is not covered here and will be erased with
everything else: at the time of writing a `aff-sp-postgres-1` container was
running, and there is RAG work outside this repository. Neither is mine to
package, and neither is in this bundle.
