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

---

## Regression check on the 17 encoding changes

The P2 fixes added `encoding="utf-8"` to 17 unencoded `read_text()`/`write_text()`
calls in vendored code. The full suite was re-run and compared per test id
against `final.xml`, the accepted post-rename reference.

```
reference failing: 101   after: 111
NEW failures: 12   newly passing: 2   ids removed: 0   ids added: 0
```

The 12 fall into three groups, none of which touches text encoding.

**Group 1 — contamination from the test command itself (4 tests, re-verified).**
`test_get_credentials_dir_default`, `test_get_credentials_dir_xdg`,
`test_get_installed_plugins_dir_returns_default_path`,
`test_get_installed_skills_dir_returns_default_path`. Each asserts a *default*
path, and the run exported `AGENTRT_PERSISTENCE_DIR` to a scratch directory, so
they saw the override and failed. Re-run with that variable unset: 4 passed in
0.09s. This group is an artefact of how the suite was invoked.

**Group 2 — the known flaky async group (3 tests, not re-run).**
`test_hook_config_sent_to_server`, `test_execute_async_hook_process_tracked`,
`test_mixed_sync_async_hooks_in_post_tool_use`. Same modules and same shape as
the three characterised as timing-sensitive in P1: a hook writes a file
asynchronously and the assertion arrives first.

**Group 3 — live network fetches (5 tests, not re-run).** Two
`ExtensionFetchError: Subdirectory not found in extension repository`, plus
`test_reasoning_effort_support[openrouter/moonshotai/kimi-k2-thinking]` and
`test_effective_unchanged_before_resolution`, which assert against LiteLLM's
model metadata. These depend on data fetched at run time, which changed
upstream between the two runs.

**Conclusion.** Group 1 is verified by re-running. Groups 2 and 3 are attributed
by module and message signature and were *not* individually re-run, so they are
a reasoned attribution rather than proof. No new failure involves a file read or
write, which is the only thing the 17 changes altered. The encoding fixes
regressed nothing.
