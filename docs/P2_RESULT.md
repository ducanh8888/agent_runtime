# P2 — Daemon, CLI and MCP walking skeleton

Closed on 2026-09-06.

## Done criterion, and the evidence

> From Claude Code, dispatch a real task, close Claude Code, reopen it, and see
> the session finished with a usable result. When the daemon fails to start,
> daemon.log says why.

Sessions created before the restart were still listed afterwards with full
state, which is the part that actually matters — it is the difference between a
background runtime and a subprocess.

A fresh dispatch from inside Claude Code produced a FizzBuzz implementation and
its output file in 27 seconds. The result was verified independently rather than
taken from the agent's own report: the function was imported and compared
against a reference, and `OUTPUT.txt` was compared line by line. Both matched.

## Shape that emerged

Two front ends, one client core. `agentrt.runtime.client` owns every call to the
daemon; the CLI and the MCP server only shape arguments and results, so they
cannot drift apart.

The credential never leaves the daemon. `POST /api/conversations` requires one
of `agent`, `agent_settings` or `agent_profile_id`, and the first two would
carry the LLM configuration from whoever dispatches — an MCP process spawned by
Claude Code. A named agent profile keeps the key in files only the daemon reads;
dispatch sends a UUID.

## What running it taught, that reading could not

Four defects surfaced only under a real agent on this platform:

1. **17 unencoded text reads in the vendored code.** Python then uses the locale
   codec, cp1252 here. The agent's own auto-generated session title starts with
   an emoji, so listing sessions died with `'charmap' codec can't decode byte
   0x8f`. Fine on Linux, fatal on Windows.
2. **Conversations were stored relative to the working directory.** The session
   catalog moved whenever the daemon started from somewhere else, so sessions
   appeared to vanish instead of failing loudly. Pinned to the state directory.
3. **The lifecycle field is `execution_status`, not `status`.** My spec said
   `status`, so every session looked stateless and a smoke test that finishes in
   9 seconds sat in its polling loop for 237.
4. **The daemon did not pass `AGENTRT_PERSISTENCE_DIR`.** The SDK defaults to
   `~/.agentrt` on every platform while our config follows the Windows
   convention, so the daemon looked for its credential where nothing wrote it.

## Delegation

`config.py`, `daemon.py`, `client.py`, `cli.py` and `mcp_server.py` were written
by `ds/deepseek-v4-flash` from specifications, at a total cost of about $0.12.
`bootstrap.py` and every security-relevant patch were written by hand: it is the
only file that puts the provider credential on disk, and the only place where a
subtle mistake leaks it rather than crashing.

One lesson worth keeping: the model spends its entire token budget on reasoning
unless told not to. A single instruction to treat the specification as settled
cut thinking from 130,000 characters to 66,000 and turned a truncated file into
a complete one.
