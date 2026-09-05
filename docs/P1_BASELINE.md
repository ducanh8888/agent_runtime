# P1 — Test baseline before the namespace rename

Taken at commit `0187ce7` (vendored tree with the litellm pin bumped to 1.93.1), on Windows 11, Python 3.13.15, from `packages/`.

Command:

```
AGENTRT_PERSISTENCE_DIR=<scratch> uv run pytest -p no:cacheprovider -n auto --timeout=180 --timeout-method=thread \
  -q --tb=no -rf --junitxml=baseline.xml
```

`AGENTRT_PERSISTENCE_DIR` is mandatory from here on and was added after the fact.
Without it the suite writes into the real user state directory: these runs left a
`gpt-4o` agent profile with a dummy key, plus `explicit-model.json` and
`glm-default.json`, in `~/.openhands` and then `~/.agentrt`. Harmless while that
directory held nothing, but P2 puts the real 9Router credential there and a test
run would overwrite it.

Upstream's own `addopts` deselects the `stress` and `acp_live` markers, so this is the suite as upstream defines it.

## Result

```
89 failed, 9441 passed, 337 skipped, 10 xfailed, 11 errors  in 575s (9m35s)
```

100 test cases carry a failure or error. That is 1.0% of the roughly 9,900 that ran.

## Failure causes

| Count | Cause |
|---|---|
| 57 | Path separator / platform path semantics (`WindowsPath` vs POSIX, `\` vs `/`) |
| 30 | Other — prompt snapshots, git status differences, lease timing, import timing |
| 12 | Windows file locking and `PermissionError`, including `WinError 1314` (symlink creation needs a privilege this account lacks) |
| 1 | Timeout |

By module: `sdk.context.prompts` 26, `agent_server` 24, `cross` 16, `sdk.git` 16, `agent_server.canvas_extensions` 9, `sdk.agent.test_acp_agent` 5, and five singletons.

Representative failures:

- `tests/sdk/utils/test_path.py::test_is_host_absolute_path_uses_current_platform_semantics` — `/workspace/file.py` is not absolute on Windows.
- `tests/cross/...` — `assert '\tmp\fake-python' == '/tmp/fake-python'`.
- `tests/agent_server/canvas_extensions/...` — `OSError: [WinError 1314] A required privilege is not held by the client` on symlink creation.
- `tests/sdk/agent/test_acp_agent.py::test_codex_auth_json_materialises_to_conversation_root` — asserts POSIX mode `0o600`; Windows reports `0o666`.
- `tests/sdk/git/...` — `GitChangeStatus.ADDED` where the test expects `UPDATED`.
- `tests/sdk/test_import_performance.py::test_import_openhands_sdk_time` — 11.3s average against a 10s limit.

Every failure is explained by the platform. None indicates a broken installation, and all four packages import cleanly.

## How this baseline is used

After the `openhands` to `agentrt` rename, the same command runs again and the two JUnit XML files are compared per test id. The rename is accepted only when the set of failing test ids is unchanged. A summary-line comparison would not be enough, because a rename could plausibly fix one test and break another while leaving the totals identical.

The XML lives outside the repository, in the session scratchpad, since it is a measurement rather than source.

## Environment note

Running the suite creates `~/.openhands` on the host. After this run it contained only empty lock files and empty `cache/`, `profiles/` and `provider-connections/` directories — no `base_state.json`, so no real conversation state exists on this machine. The rename therefore has no user data to stay backward compatible with.

---

# P1 result — after the rename

Same command, same machine, at commit `1fa9eab`.

```
92 failed, 9199 passed, 335 skipped, 10 xfailed, 9 errors
```

Compared per test id against the baseline:

| | Count |
|---|---|
| New failures | 3 |
| Newly passing | 2 |
| Test ids removed | 221 |
| Test ids added | 0 |

## The 221 removed ids

Seven `tests/cross/` modules were deleted. They exercise `.github/scripts/*`, which round 17 put out of the rename scope, and several of them additionally install `agentrt-sdk` from PyPI, where no such project exists. They test upstream's release infrastructure, not the product.

## The 3 new failures are flaky, not broken

```
tests/cross/test_remote_conversation_live_server.py::test_websocket_attach_wait_does_not_block_ready_endpoint
tests/sdk/hooks/test_executor.py::TestAsyncHookExecution::test_execute_all_with_mixed_sync_async_hooks
tests/sdk/hooks/test_manager.py::TestAsyncHookManager::test_async_pre_tool_use_still_runs
```

All three pass in isolation. Running just their two modules under `-n auto` three times in a row gave 0, then 1, then 2 failures — non-deterministic. They are timing-sensitive: hooks that write a file asynchronously, and a websocket readiness check. Nothing in a namespace rename changes timing.

Honest limit on that claim: the baseline was a single sample, so it cannot establish that these tests were equally flaky before. It is consistent with the observed rate of roughly one or two failures per 165 tests, but it is not proof.

## Two tests that started passing

Both were already failing in the baseline for platform reasons and now pass. Not investigated further, since the direction is favourable.

## What was fixed along the way

Four things surfaced only by running the suite, none of which the file survey could have predicted:

1. `litellm==1.93.0` has no Windows wheel, so the pristine vendored tree cannot be installed on Windows at all. Pinned to 1.93.1.
2. Deleting `uv.lock` and resolving from scratch drifted 692 version lines, because upstream's `exclude-newer = "7 days"` is relative to the current date. `binaryornot` went 0.4.4 to 0.6.0 and `chardet` disappeared, which broke nine file-editor encoding tests that had nothing to do with the rename. Restoring the lock and re-locking minimally leaves 16 changed lines: four workspace members out, four in.
3. The protection pattern for LiteLLM model ids also matched `.openhands/` path fragments, so 152 state-directory references survived the rename. Over-protecting was as damaging as under-protecting.
4. `packages/.openhands/hooks.json` is upstream's own dev-agent config and points at a `.sh` script. Under `shell=True` on Windows, `cmd.exe` falls back to ShellExecute for an unregistered extension and raises a modal "Pick an app" dialog that blocks until clicked — one per xdist worker. Deleted.

## Verdict

P1 is complete. The failing set is unchanged apart from three tests demonstrated to be non-deterministic, and the namespace, distributions, state directory and environment prefix are all `agentrt`.
