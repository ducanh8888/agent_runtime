# Iteration-budget end-to-end check (Linux)

Date: 2026-10-26
Branch: `ib/e2e-linux` at `854dd14`

This is the Linux half of plan item 6.8. I used a source-resolved runtime from
this worktree and an isolated state directory, `/tmp/ib-e2e-state`; the live
`/home/testosterone/.agentrt` daemon and state were not used. The provider
configuration was copied into the scratch `.env` without printing its values.

## Reproduction setup

The source runtime was run directly from the repository venv:

```text
mkdir -p /tmp/ib-e2e-state && chmod 700 /tmp/ib-e2e-state
grep -E '^AGENTRT_(BASE_URL|API_KEY|DEFAULT_MODEL)=' /home/testosterone/.agentrt/.env > /tmp/ib-e2e-state/.env
AGENTRT_STATE_DIR=/tmp/ib-e2e-state packages/.venv/bin/python -m agentrt.runtime.cli daemon start
```

`daemon status` showed the daemon process resolving to
`/home/testosterone/.agentrt/worktrees/ib-e2e/agent_runtime/packages/.venv/bin/python`
and using an ephemeral loopback port. All subsequent CLI commands used the same
`AGENTRT_STATE_DIR` and Python module invocation.

## A — uncapped skill-loading audit

Dispatch command:

```text
AGENTRT_STATE_DIR=/tmp/ib-e2e-state packages/.venv/bin/python -m agentrt.runtime.cli dispatch --workspace /home/testosterone/.agentrt/worktrees/ib-e2e/agent_runtime --permission readonly --title skills-audit 'Read the configured cross-repo-testing and feature-release skills in this repository and report what they require and whether they repeat.'
```

The session `2a7d72bf-2d87-41cf-a86a-52e6f66d7d54` finished without a
`max_iterations` argument. It used 6 iterations, had the default 100,000
iteration budget, and reported `condensation_steps: 0` and
`termination_reason: finished`. Thus the iteration count remained separate from
the condensation count (A.1/A.2). The transcript contained the final skills
review; this small session did not show a skill re-invocation repeat. It also
did not produce a condensation, so it cannot estimate the larger census's
condensation share or skill re-invocation rate in sections 2.2–2.3.

## B — restart recovery

The first attempt used `readonly` and could not execute terminal commands. I
record this because it is an operational gotcha, not a successful check. A
second terminal-capable `inspect` attempt resumed automatically but finished
without terminal access. The definitive run used `workspace` permission.

Dispatch command:

```text
AGENTRT_STATE_DIR=/tmp/ib-e2e-state packages/.venv/bin/python -m agentrt.runtime.cli dispatch --workspace /home/testosterone/.agentrt/worktrees/ib-e2e/agent_runtime --permission workspace --title restart-workspace-check-b 'Run six separate sequential terminal calls, each command sleep 8 && echo step-N, N=1 through 6. Report the completed steps after executing all six.'
```

While `cb7d4c0b-80f1-4dd2-a398-675445e24b66` was running, I identified the
scratch daemon PID with `ps -p PID -o pid=,args=` and killed only that PID:

```text
kill -9 <scratch-daemon-pid>
AGENTRT_STATE_DIR=/tmp/ib-e2e-state packages/.venv/bin/python -m agentrt.runtime.cli daemon start
```

After restart I used only `status`, `transcript`, `result`, and `wait`; no
`send` or `resume` was issued. The transcript contained a `DaemonRestarted`
error with detail saying the run would resume automatically. It then showed
`sleep 8 && echo step-1` through step 6 and their observations. The run reached
`finished` with a real result listing all six steps and `iterations_used: 8`.

This check passes lane D: the persisted run resumed on its own, preserved the
restart marker in history, and completed. The result also exposes
`termination_reason: daemon_restarted` and `completed_cleanly: false` even
though the run ultimately finished; this is worth noting for consumers, but it
did not prevent completion or the `wait` call from collecting it.

## C — capped partial result

Dispatch command:

```text
AGENTRT_STATE_DIR=/tmp/ib-e2e-state packages/.venv/bin/python -m agentrt.runtime.cli dispatch --workspace /home/testosterone/.agentrt/worktrees/ib-e2e/agent_runtime --permission workspace --max-iterations 3 --title cap-check-c 'Perform a long exploratory task that cannot finish in three agent steps: inspect the repository broadly, run several distinct read-only checks, and provide a detailed synthesis only after completing the exploration. Do not stop early.'
```

The session `7c7be6d5-7847-4eb2-96c0-094053d5d474` consumed all three
iterations and ended with `status: error`, `result_state: partial`,
`termination_reason: cap`, and an error code `MaxIterationsReached`. The
`result` endpoint returned `state: partial`, but the result text was empty
(`result: ""`, `result_length: 0`) and it returned no `resume_hint`. The
transcript shows the cap arriving immediately after a terminal observation,
before any final response was produced.

The `wait` endpoint was checked. It returned bucket `failed` and status `error`,
not bucket `partial`. Therefore this live check does **not** pass the full B.1/B.2
contract: cap termination and partial state are exposed, but the promised
non-empty partial result, `resume_hint`, and `wait` partial bucket were absent
for this real session. This may be task-shape dependent (no final text existed
to preserve), but it is a finding rather than an assumption.

## Cleanup

The scratch daemon was stopped with:

```text
AGENTRT_STATE_DIR=/tmp/ib-e2e-state packages/.venv/bin/python -m agentrt.runtime.cli daemon stop
rm -rf /tmp/ib-e2e-state
```

No live daemon or live state was touched.
