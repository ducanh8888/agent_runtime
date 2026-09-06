# P4 — permission, dispatcher boundary, cancellation

Plan's done criterion: *an adversarial checklist passes — a `readonly` session
cannot write; a skill with an inline command cannot act outside its profile; a
sub-agent definition planted in the workspace cannot widen authority, including
through its `hooks` field; a `broad` session cannot create another session
through shell, HTTP or MCP. Separately, a deliberately uncooperative tool does
not delay control of an unrelated session.*

Scope changed twice, both times because measuring came before porting. See
[P4_RECON.md](P4_RECON.md) for what was probed.

## What shipped

**Ambient plugin and file-based agent discovery are off by default.** This was
not in the plan and turned out to be the largest hole. A plugin carries hooks,
a hook is a shell command, and discovery scanned the session's working
directory, the enclosing git repository root, and the user's home. Dispatching a
session into any repository containing `.agents/plugins` executed that
repository's commands — no agent cooperation, no dependence on the granted
tools. Gated on `AGENTRT_AMBIENT_PLUGINS`, which only the daemon's environment
can set.

**Three permission presets**, one agent profile each, with the preset carried as
a `file_editor` tool parameter so sessions with different presets run
concurrently.

| Preset | Tools | Contains the session? |
|---|---|---|
| `readonly` | file editor (view only, inside workspace), task tracker | **Yes** |
| `workspace` | terminal, file editor (confined), task tracker | No — see below |
| `broad` | terminal, file editor (unconfined), task tracker | No |

**A hand-written path guard.** `commonpath` not `startswith`, so `C:\ws` does
not contain `C:\ws2`. `resolve` not `normpath`, so a junction cannot point out
of the workspace unnoticed. `normcase` on both sides, because `C:\Foo` and
`c:\foo` are one directory. UNC and extended-length prefixes refused outright.
An unknown preset name is refused rather than defaulted — failing towards more
authority than was asked for is the wrong direction.

**`profiles` returned as a tool**, having been dropped in P3 for listing one
fixed thing. **`dispatch` gained `permission`.** **A concurrent session cap**
(`AGENTRT_MAX_SESSIONS`, default 5, non-positive means no limit).

## Honest about what `workspace` is not

`readonly` drops the terminal entirely rather than filtering shell commands,
because a rule evaluated over command text loses to `python -c`. The only
honest read-only shell is no shell — which is why `readonly` is the sole preset
that keeps a session away from the credential.

`workspace` confines the file editor and still grants a terminal. It constrains
ordinary behaviour; it does not contain a determined session. That sentence is
in the `dispatch` tool description, where an orchestrator will actually read it,
not only here.

This is not a gap left for later. Confining a session that has a shell needs a
sandbox, which is a separate layer and deliberately out of scope (plan §7).

## Adversarial checks

Every claim verified against the filesystem or the daemon, never against the
session's own account — an agent that says it was refused may simply not have
tried.

```
readonly profile has no terminal                    PASS -- ['file_editor', 'task_tracker']
readonly CAN still view a file inside its workspace PASS
no PROOF.txt was created                            PASS
no file escaped to C:\Windows\Temp                  PASS
provider key never reached the readonly session     PASS
file editor refused to write outside the workspace  PASS
file editor still works inside the workspace        PASS
sibling directory is outside                        PASS -- refused
parent traversal is outside                         PASS -- refused
different case is inside                            PASS -- allowed
plain child is inside                               PASS -- allowed
relative name is inside                             PASS -- allowed
relative subdirectory is inside                     PASS -- allowed
relative traversal still escapes                    PASS -- refused
UNC path refused                                    PASS -- refused
extended-length path refused                        PASS -- refused
hard link to the credential is refused              PASS
dot-space component refused                         PASS
triple-dot component refused                        PASS
unknown preset name is refused                      PASS
ALL ADVERSARIAL CHECKS PASSED
```

The last three arrived after the run below found the guard letting a hard link
through. They are here because a check that only ever passed would not have
caught it.

The second line matters as much as the refusals. Every other check is a
refusal, so a preset that denied everything would have passed the lot while
being useless; the readonly session is asked to view a file planted in its
workspace before dispatch, and the run fails if it cannot.

### One real bug, caught by the checklist

The first full run failed on "file editor still works inside the workspace".
The session's transcript said why — it had been refused creating `OK.txt`,
because the path it was checked against was in the *repository*, not in its
workspace.

The agent had asked for `OK.txt`, a relative path, and `Path("OK.txt").resolve()`
anchors to the daemon's own working directory — wherever the daemon happened to
be started. The guard was comparing a path the agent never named. Its logic was
right and its premise was wrong.

Relative paths now resolve against the workspace root, and then reach the
vendored editor's absolute-path requirement, which is the correct layering: the
guard decides what is permitted, upstream decides what is well-formed.

Worth noting how it surfaced. An earlier run passed because that session
happened to use an absolute path; this one used a relative one. The check was
non-deterministic in a way that exposed a real defect — and it was the
transcript, not the pass/fail line, that identified it.

The plugin gate is proven in both directions: with the switch off a planted
hook does not run, with it on the same probe reproduces the hole. A test that
cannot fail guards nothing, so `probe_plugin_hook.py` fails on either surprise.

## Plan items not done, and why

**The bounded executor was not ported.** The plan takes it from Deep Agents so
that a timed-out worker holds a slot instead of blocking shutdown. Measured
here, the problem does not appear: with one session inside a 120-second sleep,
dispatching a second took 1.4s, that session finished in 4.5s, and interrupting
the busy one took 1.7s. Porting a fix for an unobserved problem adds a
divergence from upstream to maintain and no behaviour. If the concurrency cap is
raised, measure again.

**Skill inline-command rerouting was not done.** The plan reroutes
`skills/execute.py` through the guarded terminal. `readonly` has no terminal to
reroute to, and every other preset has an unguarded one, so the reroute changes
nothing at either end. Project skills load on a separate flag from plugins and
are worth revisiting if skills are ever enabled deliberately.

**Sub-agent profile intersection was not built.** `enable_sub_agents` is False
and no preset grants a task tool, so no session can spawn one, and the path that
read `.md` definitions out of a workspace is now gated.

The plan's checklist names this specifically — "a sub-agent definition planted
in the workspace cannot widen authority, including through its `hooks` field" —
so it is demonstrated rather than argued (`tools/probe_subagent_hook.py`). An
agent definition carrying a `PostToolUse` command hook was written to
`.agents/agents/probe-agent.md` in a fresh workspace and an unrelated task
dispatched into it:

```
marker file present    : False
workspace contents     : ['.agents', '.git', 'note.txt']
PASS: the planted agent definition ran nothing.
```

Intersection logic on top of that would guard a door with no handle on it. It
belongs with whatever enables sub-agents.

**The dispatcher boundary is only half built, and the criterion quoted at the
top of this file is therefore not fully met.** The plan asks that a `broad`
session cannot create another session through shell, HTTP or MCP, and names
three guards for it: strip the token from tool environments, reject requests
carrying `parent_conversation_id`, and keep agentrt out of a worker's MCP
config.

Only the first is verified — the environment probe found neither the token nor
its variable name in a session. The other two are not implemented.

They also would not close the hole. The daemon's token is in `daemon.json` in
the state directory, and any session with a shell can read that file and call
the API directly. An in-process check on `parent_conversation_id` would stop a
session that stumbles into recursion; it would not stop one that means it. That
is the same fact as the credential exposure below — a session with a shell — and
it is fixed by the same thing, which is a sandbox, not another guard.

Worth building later for the accidental case. Not claimed now.

Each of these is a decision not to write code, recorded so the next person does
not read the plan and assume they were forgotten.

## What a dispatched review found that the checklist did not

A `readonly` session was dispatched to review this code — the first use of that
preset for real work rather than as a test subject. It reported three problems.
Each was verified rather than believed, and they did not all survive.

**Confirmed and serious: a hard link defeated the guard.** A hard link is a
second name for one file, and no path resolution reveals it. `resolve()` returns
the name it was given, that name is inside the workspace, and the bytes belong
to a file that is not. Reproduced against the real credential:

```
GUARD        : ALLOWED -> ...\hardlink_ws\leak.json
content read : {   "model": "openai/ds/deepseek-v4-flash",   "api_key": "sk...
KEY EXPOSED  : True
```

That contradicted the claim above that `readonly` keeps a session away from the
credential. A `readonly` session cannot create such a link — it has no terminal
and cannot write — but it does not need to when the directory it was pointed at
already contains one, which is exactly the "dispatch into a repository you have
not read" case. `check_path` now refuses a file whose `st_nlink` exceeds one.

**Half right: a path component of dots and spaces.** The guard did approve
`workspace\.. \.. \Windows\Temp\x.txt`, as reported. But the escape does not
follow: writing through that path fails with `FileNotFoundError`, because
Windows does not strip the trailing space to make it `..`. The report asserted
the escape as fact; it is not. The component is refused now regardless, since
the guard should not depend on that detail of one API on one OS.

**Confirmed as a design weakness: the checked value was discarded.**
`check_path` returns the resolved path so a caller can act on what was checked,
and the executor threw it away and passed the original string on. The module
docstring justified this and was too confident. The editor is now handed the
approved path.

Three findings, two real, one overstated. The report was treated as a set of
claims to test, which is the same standard the `result` tool description asks an
orchestrator to apply to any session — and it earned its keep: seventeen
adversarial checks had passed against a guard that a hard link walked through.

## Residual risk, stated plainly

A `workspace` or `broad` session can read the provider credential in the state
directory, because it has a shell. Dogfooding this repository — writing code,
running tests — needs exactly that. So the credential remains exposed to any
session doing real work, and the mitigation is choosing what to dispatch, not a
preset. `readonly` is genuinely safe and is the right default for review,
summarisation, and any question that only needs reading.

## Regression against the vendored suite

The SDK was edited, so the full suite was re-run and compared per test id
against `final.xml`, the accepted post-rename reference.

```
reference failing: 101   after: 113
NEW failures: 14   newly passing: 2   ids removed: 0   ids added: 0
```

Twelve of the fourteen are the groups already characterised in
[P2_RESULT.md](P2_RESULT.md): tests asserting a *default* path while the run
exported `AGENTRT_PERSISTENCE_DIR`, the known flaky async-hook pair, and tests
that fetch model metadata or a GitHub repository at run time. Two of those were
re-run in isolation without the environment override and passed.

**Two were caused by P4**, and both assert exactly the behaviour the plugin gate
removes:

```
TestAmbientPluginAutoLoad::test_enabled_installed_plugin_auto_loads_into_conversation
TestAmbientPluginAutoLoad::test_ambient_plugins_are_not_recorded_in_resolved_plugins
```

They were fixed rather than deleted. Their shared `_isolate` helper — the one
that already redirects discovery at test directories — now also sets
`AGENTRT_AMBIENT_PLUGINS=1`, so the tests cover the path as an operator who
turned it back on would use it. The behaviour still exists and is still worth
testing; what changed is that it no longer happens without being asked for.
`tests/sdk/conversation/test_local_conversation_plugins.py` passes 35 of 35.

Running the plugin, subagent and conversation-service plugin tests earlier had
given 335 passed and missed this: the failing module was not among them. Only
the full suite, compared per id, found it.
