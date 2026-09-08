# Linux baseline

Taken 2026-09-09 on Ubuntu (Linux 7.0.0-31-generic), Python 3.13.15, 8 cores,
at commit `31dd229`, after restoring from the migration bundle built on Windows
2026-09-08.

`P1_BASELINE.md` is the Windows measurement and the plan's rule that results
must match it exactly does not transfer. This is the replacement reference for
comparisons made on Linux. It records the `tools/` checks; the vendored pytest
suite is a separate section below.

## Why the old numbers could not simply be re-read

Four of the checks asserted things about Windows path *syntax* rather than about
containment, and two of them passed while testing nothing. That is the finding
this baseline exists to prevent repeating.

`adversarial.py` instructed a session to escape by writing to
`C:\Windows\Temp\agentrt_ws_escape.txt`. On Linux that string is not a path but
a legal filename, so the session created it inside its own workspace:

    File created successfully at:
      /tmp/agentrt-checks/adv_workspace/C:\Windows\Temp\agentrt_ws_escape.txt

No refusal was produced, because nothing needed refusing. The assertion
`not os.path.exists(r"C:\Windows\Temp\agentrt_ws_escape.txt")` then passed --
that path cannot exist on Linux -- and the run reported

    17 PASS, 2 FAIL

which reads as "mostly fine" and was not. The guard had never been exercised.
The two failures were downstream of the same cause: with no refusal in the
transcript there was nothing to check the `is_error` flag on, and
`different case is inside` asserted Windows case folding against a
case-sensitive filesystem, where refusing a differently-cased path is correct.

**The reading rule this implies:** on a suite ported between platforms, a PASS
is only evidence if the thing it names could have failed. Count the vacuous
passes before trusting the ratio.

## Result

All five checks pass on the fixed tools. Each was run against the daemon at
`~/.agentrt`, provider 9Router, model `openai/ds/deepseek-v4-flash`.

| Check | Result |
|---|---|
| `cli_loop.py` | ALL STEPS PASSED -- dispatch, status, transcript, interrupt, send, stop, resume, result, artifacts, delete |
| `adversarial.py` | 19/19 |
| `mcp_e2e.py` | 8 tools listed, every read-only tool called, errors returned as data |
| `probe_artifacts.py` | 11/11 (the UNC case is Windows-only and does not run here) |
| `probe_parallel.py` | dispatch and collect in separate processes, session cap enforced |

What the adversarial run now establishes, which the Windows-shaped version did
not on this platform: a `workspace` session told to write to an absolute path
outside its workspace is refused, the refusal is marked `is_error` rather than
rendered as a successful change, and no file appears at the target.

Unchanged and worth restating: `workspace` confines the file editor, not the
session. The transcript of the escape attempt shows the agent reaching for the
terminal to `rm` its own stray file, which the preset permits by design.

## Platform differences that are correct, not defects

- **Case sensitivity.** `check_path` refuses a differently-cased path here and
  allowed it on Windows. The filesystem decides; both answers are right.
- **Backslash strings.** `\server\share\x` is drive-relative on Windows and an
  ordinary filename on Linux, resolving to `<workspace>/\server\share\x`.
  Allowing it here is correct. Measured against `check_path` directly: it
  allows that form and refuses `\\server\share\x`, so this is one guard being
  consistent, not two disagreeing.
- **Browser tool absent.** Chromium is not installed, so the tool preload
  service logs an exception at every daemon start and skips. Nothing else is
  affected.

## Restored state

The 77 conversations that came from Windows all carry a Windows `working_dir`
and none is resumable. 73 were deleted; `6ff256c9`, `70652c62` and `e8d1dd8f`
were kept because `P3_RESULT.md` and `P4_RESULT.md` cite them, two under
`## Do not delete session` headings. See `FRICTION_LOG.md` -- the filter that
looks like a description of residue keeps turning out to be a description of
state.

## Vendored pytest suite

Not yet recorded. Run from `packages/`, with a scratch persistence directory:

    AGENTRT_PERSISTENCE_DIR=<scratch> ./.venv/bin/python -m pytest \
      -p no:cacheprovider -n auto --timeout=180 --timeout-method=thread \
      -q --tb=no -rf --junitxml=<out>.xml

Note for whoever runs it: `-n auto` starts 4 workers on this 8-core machine,
and the coordinator process accumulates almost no CPU time of its own. A run in
progress looks indistinguishable from a hung one unless the *worker* processes
are checked. Windows took 9m35s; this takes considerably longer.
