# P4 reconnaissance — what is already true

Probing before porting, on the P3 principle. Each item below either removes work
from the plan or adds work it did not anticipate. Nothing here is implemented
yet; this is the scope P4 should actually have.

## Already satisfied — two plan items shrink

**Session environments are clean.** A session was dispatched with the task of
dumping `os.environ` and the whole transcript searched for the daemon token, the
provider key, and even the variable names:

```
daemon token                     not present
provider api key                 not present
AGENTRT_SESSION_API_KEYS name    not present
AGENTRT_9ROUTER name             not present
```

The vendored `sanitized_env` already does this. Enforcement point 5 keeps its
other guards — rejecting `parent_conversation_id`, keeping agentrt out of a
worker's MCP config — but the environment half is done.

**Sessions do not block each other.** With one session inside a 120-second
sleep: dispatching a second took 1.4s, that second session finished in 4.5s
total, and interrupting the busy one took 1.7s.

The bounded-executor port (plan §6 P4.2) addresses a problem this daemon does
not exhibit at two concurrent sessions. It should not be ported on the strength
of the donor having it. Measure again at the concurrency cap before deciding.

## The file editor has no containment at all

`FileEditorExecutor.__init__` takes `workspace_root`, which reads like a
sandbox. It is not one. `FileEditor` uses it only for *path suggestions* in
error messages — its own docstring says "Root directory that serves as the
current working directory for relative path suggestions." No code compares a
target path against it.

`allowed_edits_files` is an exact-file allowlist, and `__call__` skips it
entirely when `command == "view"`, so reads are unrestricted regardless.

`validate_path` requires an absolute path and passes it through unchanged, so
the editor opens exactly `Path(action.path)`. A guard that resolves the same
string sees the same file — there is no separate resolution step to diverge
from. That makes a guarded executor registered under the same tool name sound,
rather than the "wrapper approves X, executor opens Y" hazard it would be if the
editor derived paths itself.

## Injection point confirmed

- `register_tool(name, ...)` **replaces** an existing name (it logs a duplicate
  warning and overwrites; there is a TODO to make it raise).
- `resolve_tool` reads the registry **first**; `BUILT_IN_TOOL_CLASSES` is only a
  fallback for unregistered names. Re-registering under `terminal` or
  `file_editor` therefore takes effect.
- `Tool(name=..., params={...})` reaches `create(cls, conv_state, **params)`, so
  a permission preset can travel per tool spec.

## Wider than the plan thought — workspace plugins carry hooks

Plan §5 item 4 worried about a sub-agent definition's `hooks` field. The
automatic path is broader.

`_ensure_plugins_loaded` calls:

```python
load_available_plugins(
    work_dir=self.workspace.working_dir,
    include_user=True,
    include_project=True,
)
```

Both flags default to False and both are explicitly True here. Plugins carry
hooks, which are shell commands, and `_merge_runtime_plugin_hooks` merges them
into the conversation. So a session that writes a plugin into its **own
workspace** may cause hooks to run, without any sub-agent involved — and
`include_user=True` means `~/.agents/plugins` counts too, which a session with a
terminal can write to.

**This is read from the source, not yet demonstrated.** It is the first thing
P4 should verify empirically, because if it holds it is a larger hole than the
one the plan set out to close, and if it does not, the reason why is worth
knowing.

Separately confirmed by reading: `_register_file_based_agents` runs
**unconditionally** in `_ensure_agent_ready`, regardless of
`enable_sub_agents`. A `.md` agent definition in a session's workspace is
registered into a process-global registry. With no task tool in the profile the
session cannot invoke it — but the registry is shared across sessions in one
daemon, so a definition planted by one session is visible to another.

## Confirmed as the plan described

`load_hooks_from_workspace` is called only from `hooks_router.py`, the HTTP
endpoint. Conversation startup never calls it. Workspace `hooks.json` is not
auto-loaded, exactly as the plan states — the plugin path above is a different
route to the same place.

## Suggested P4 shape

1. Verify the workspace-plugin hook path. It reframes the phase if real.
2. Three agent profiles in `bootstrap.py`; `dispatch` gains `permission`,
   default `workspace`; the `profiles` tool returns because it now lists three
   things.
3. `readonly` first: no terminal tool at all, and a guarded file editor that
   refuses writes and refuses reads outside the workspace. Test it
   adversarially by dispatching a `readonly` session told to write a file, read
   `<state-dir>/profiles/default.json`, and list a directory — then read the
   transcript for three refusals.
4. `workspace` next: writes confined to the workspace root, terminal present.
   Be honest in the tool description that a terminal defeats path guards, which
   is why `readonly` is the only preset that protects the credential.
5. `broad` is "no guard, minus the dispatcher-authority checks".
6. Session cap last — a counter and a message.

The path guard itself is hand-written, not delegated, for the same reason
`bootstrap.py` was: `startswith` instead of `commonpath`, `normpath` instead of
`resolve()`, or a missing `normcase` on Windows each turn it into decoration.
