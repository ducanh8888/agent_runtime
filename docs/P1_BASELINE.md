# P1 — Test baseline before the namespace rename

Taken at commit `0187ce7` (vendored tree with the litellm pin bumped to 1.93.1), on Windows 11, Python 3.13.15, from `packages/`.

Command:

```
uv run pytest -p no:cacheprovider -n auto --timeout=180 --timeout-method=thread \
  -q --tb=no -rf --junitxml=baseline.xml
```

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
