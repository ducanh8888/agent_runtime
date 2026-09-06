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
no PROOF.txt was created                            PASS
no file escaped to C:\Windows\Temp                  PASS
provider key never reached the readonly session     PASS
file editor refused to write outside the workspace  PASS
file editor still works inside the workspace        PASS
sibling directory is outside                        PASS -- refused
parent traversal is outside                         PASS -- refused
different case is inside                            PASS -- allowed
plain child is inside                               PASS -- allowed
UNC path refused                                    PASS -- refused
extended-length path refused                        PASS -- refused
unknown preset name is refused                      PASS
ALL ADVERSARIAL CHECKS PASSED
```

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
and no preset grants a task tool, so no session can spawn one. The registration
path that read `.md` definitions out of a workspace is now gated. Intersection
logic would guard a door with no handle on it; it belongs with whatever enables
sub-agents.

Each of these is a decision not to write code, recorded so the next person does
not read the plan and assume they were forgotten.

## Residual risk, stated plainly

A `workspace` or `broad` session can read the provider credential in the state
directory, because it has a shell. Dogfooding this repository — writing code,
running tests — needs exactly that. So the credential remains exposed to any
session doing real work, and the mitigation is choosing what to dispatch, not a
preset. `readonly` is genuinely safe and is the right default for review,
summarisation, and any question that only needs reading.
