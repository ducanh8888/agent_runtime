# Running the checks without running everything

The full suites answer "did anything break anywhere". They are the wrong tool
for "did the file I just edited still work": the server suite alone takes about
five minutes, and a phase touches a fraction of it. Use a narrow selection while
working and run the full suites once, at the phase gate.

## Narrow selection

`tools/scope.py` maps an area to the tests that cover it and runs them from
`packages/`:

```bash
python3 tools/scope.py runtime server-workspace
python3 tools/scope.py --phase h4
python3 tools/scope.py --list
python3 tools/scope.py server-core -k conversation_service
```

Measured on this machine, from a warm venv:

| Selection | Tests | Time |
|---|---|---|
| `runtime` | 95 | ~6 s |
| `conversation` | 596 | ~33 s |
| `--phase h4` | 1119 | ~61 s |
| full server suite | 2199 | ~5 min |

Run several areas in one invocation only when they do not share a process-wide
registry. `--phase h4` is the exception that proves the rule: it is curated so
no selected file leaves a local `Tool` subclass behind.

## Full suites, at the gate

Always from `packages/`, so the repository's `addopts` markers apply:

```bash
cd packages
.venv/bin/python -m pytest tests/runtime tests/sdk/conversation -q
.venv/bin/python -m pytest tests/agent_server -q
```

Run from the repository root instead and the same command collects the `stress`
suite as well and does not finish. The `acp_live` and `stress` markers are
excluded by default; they are not part of a normal gate.

## Known environment-dependent failures

- `tests/tools/file_editor/utils/test_shell_utils.py::test_check_tool_installed_python`
  asserts that `python` is on `PATH`. This machine has `python3` only, so it
  fails before and after any change here. It is not a regression.
- A suite that runs `tests/sdk/conversation/local/test_execute_tool.py` and then
  `tests/agent_server/test_openapi_contract.py` in the same pytest process fails
  the OpenAPI export with "Local classes not supported": the test defines a
  `Tool` subclass and leaves it in the process-wide registry. Run the two as
  separate commands, which the area list above already does.
- A development `.env` above the repository is picked up by the configuration
  layer walk. Test fixtures isolate the state directory; the runtime test
  fixture also neutralizes the checkout layer, so a stray `.env` no longer
  changes a result.

## What a result record needs

A phase record under `docs/results/` states the revision, the exact command, the
observed outcome and any accepted-but-unfixed limitation. A worker's or a
review session's summary is a claim; the command output is the evidence.
