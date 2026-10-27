# Iteration-budget end-to-end check (Windows)

Date: 2026-10-27
Revision: `main` at `ed244d1`, plus the lease fix on `win/lease-pid-liveness`
(section 2). Host: Windows, Python 3.13.15, `packages/.venv`.

The Windows half of plan item 6.8; the Linux half is
[`iteration-budget-e2e-linux.md`](iteration-budget-e2e-linux.md). A scratch
daemon was started from source with its own state directory; the operator's
daemon and state were not used. The provider keys were copied into the
scratch `.env` without printing their values.

```text
AGENTRT_STATE_DIR=%LOCALAPPDATA%\Temp\ib-e2e-state
packages/.venv/Scripts/python.exe -m agentrt.runtime.cli daemon start
```

`Win32_Process` showed the daemon as
`E:\agent_runtime\packages\.venv\Scripts\python.exe -m agentrt.runtime.server_launch`
on an ephemeral loopback port.

## 1. Test gate on Windows

Commands, from `packages/`:

```text
.venv/Scripts/python.exe -m pytest tests/sdk/conversation tests/sdk/context tests/sdk/hooks tests/runtime tests/tools/task tests/tools/terminal/test_pool_recovery.py -q
.venv/Scripts/python.exe -m pytest tests/agent_server -q
```

| Selection | Passed | Failed | Errors |
|---|---|---|---|
| SDK conversation/context/hooks, runtime, task, pool recovery | 1815 | 42 | 0 |
| `tests/agent_server` | 2279 | 10 | 11 |

To separate what the iteration-budget merge did from what this host always
did, the files with failures were run again at the pre-merge base `6b88f3a`
in a detached worktree, with `PYTHONPATH` pointed at that worktree's packages.
Without `PYTHONPATH` the venv's editable install silently resolves
`agentrt.*` to the main checkout, so the first such run tested the wrong
code; the import path was printed to confirm the second.

Result: **every failure in a file that exists at the base also fails at the
base.** These failures are environmental and were there before this plan:
the 14 ACP `test_conversation_send_message.py` cases (the run-scoped reap
reads `tools_map` on an uninitialized ACP agent), the 26 prompt snapshots,
`test_memory.py` persistence-dir header, the `wsproto` live-server fixture
(server never starts), SIGTERM traps, POSIX file permissions, home-directory
favourites, and plugin catalog layout.

Two failures are in tests the merge added:

- `tests/tools/terminal/test_pool_recovery.py::test_failed_pool_initialize_does_not_leave_pool_and_next_call_recovers`
  constructs a tmux terminal and has no guard; tmux is not installed here.
  Fixed by a `skipif(shutil.which("tmux") is None)`; the other two tests in
  the file pass.
- `tests/agent_server/test_conversation_service.py::test_restart_resumes_conversations_after_non_graceful_shutdown`
  is a real Windows defect, together with the pre-existing
  `test_conversation_lease.py::test_claim_takes_over_when_previous_owner_pid_is_dead`.
  Section 2.

## 2. Defect: a crashed daemon's leases block its own restart on Windows

`conversation_lease._is_pid_alive` used `os.kill(pid, 0)`. Measured on this
host:

```text
os.kill(2**31-1, 0)  -> OSError [WinError 87] The parameter is incorrect
os.kill(<live child>, 0) -> no exception
```

The function treated any `OSError` other than `ProcessLookupError` as "alive",
so on Windows **no dead owner was ever recognised**. A daemon that dies
without releasing its leases (crash, `taskkill /F`, power loss) leaves every
running conversation's lease valid for `DEFAULT_LEASE_TTL_SECONDS = 45`;
a restart inside that window cannot hydrate them, which is exactly the case
plan lane D (resume after restart) exists for. On Windows `os.kill` with
signal 0 is also not a probe: 0 is `CTRL_C_EVENT`.

Fix (vendored server, `AgentRT fork (Windows lease takeover)`): on
`sys.platform == "win32"`, open the process with
`PROCESS_QUERY_LIMITED_INFORMATION` and read `GetExitCodeProcess`; dead only
when `OpenProcess` fails with `ERROR_INVALID_PARAMETER` or the exit code is
not `STILL_ACTIVE`. Any other failure (access denied) still counts as alive,
the same conservative rule as before. POSIX is unchanged.

Evidence:

- Without the fix: the two tests above fail on Windows. With it, both pass,
  and the rest of `test_conversation_service.py` plus
  `test_conversation_eviction.py` (139 tests) pass.
- New guard `test_pid_liveness_tracks_a_real_child_process`: a live child
  reports alive and survives the probe, the same PID reports dead after it
  exits, and `2**31 - 1` reports dead. Passes on Windows; it is a guard for
  both directions, not the reproduction.
- `ruff check`, `ruff format --check` and `pyright` (0 errors) on the touched
  files.

## 3. Lane A — uncapped skill-loading audit

```text
dispatch --workspace E:/agent_runtime --permission readonly --title win-skills-audit "Read the configured cross-repo-testing and feature-release skills in this repository and report what they require and whether they repeat."
```

Session `d1211220` finished without `max_iterations` (default 100,000):
`iterations_used: 11`, `condensation_steps: 0`, a 3,060-character report of
both skills. It was also interrupted by the lane B kill and resumed on its
own (its `termination_reason` reads `daemon_restarted`). Like the Linux run
it produced no condensation, so it says nothing about the 2.2-2.3 census
shares. The CLI has no `--skills` flag; preload (plan E) is reachable only
through the client/MCP `dispatch` and was not exercised here.

## 4. Lane B — restart recovery, on Windows

```text
dispatch --permission workspace --title win-restart-check "Run six separate sequential terminal calls, each command: Start-Sleep -Seconds 8; Write-Output step-N ..."
```

With session `6472b584` running (2 iterations), the scratch daemon's process
tree only (venv launcher `50872` and its interpreter `17200`, identified by
the `--port` in their command lines) was killed with
`taskkill /PID 50872 /T /F` at 12:22:46 and restarted; it was serving again
at 12:23:04, **18 s later, inside the 45 s lease TTL.** No `send` or `resume`
was issued.

The transcript shows steps 2-4, the `step-5` action, a `DaemonRestarted`
error ("the run will resume automatically"), `step-5` again, `step-6`, and
the final message "All six steps completed sequentially. Step 5 required a
retry after the terminal session restarted". `status: finished`,
`bucket: completed`. Lane D passes on Windows, including the lease takeover
of section 2.

A `wait` on `d1211220 dc8be0b5` started before the kill kept waiting across
it and collected both sessions afterwards, without reporting either as
`missing` (live evidence for #41 across a daemon restart).

As on Linux, a run that finished after a restart reports
`termination_reason: daemon_restarted` and `completed_cleanly: false`
(`conversation_error_after_request`) next to `status: finished`.

## 5. Lane C — capped partial result: does not pass

```text
dispatch --permission workspace --max-iterations 3 --title win-cap-check "Perform a long exploratory task that cannot finish in three agent steps ..."
```

Session `dc8be0b5`: three terminal actions, then `MaxIterationsReached`.
`result` returned `state: partial`, `termination_reason: cap`,
`result_length: 899`, but **no `resume_hint`**, and `wait` bucketed it
`failed`.

Cause, from the transcript: the cap wrap-up (plan 4.B.2) did run, since the
last agent event was an action, and its answer is the message after the cap
error. That answer is itself a tool call written as text:
`<tool>{"name":"proxy_terminal","arguments":{"command":"Write-Output '--- runtime package files ---'; ..."}}</tool>`.
The wrap-up sends the condensed view (full of tool calls) to the model with
tools disabled; this router model answered by writing its next tool call
as markup. `_run_final_summary` persists any non-empty text, and the
client's `_looks_like_tool_call` then correctly refuses to call it usable,
so `resume_hint` is withheld and the bucket is `failed`.

This is a different failure from the Linux one (empty result read before the
wrap-up finished, fixed by `5d49d74`); that fix held here, since the result
was read after the wrap-up. It is model-dependent and not Windows-specific.
Not fixed in this record: it touches the vendored wrap-up and a fix that
retries would spend another model call, both owner decisions. Recorded as
D20 in the plan.

## 6. Cleanup

The scratch daemon was stopped, `%LOCALAPPDATA%\Temp\ib-e2e-state` and the
base worktree were removed. The operator's daemon and state were not touched
by sections 3-5.
