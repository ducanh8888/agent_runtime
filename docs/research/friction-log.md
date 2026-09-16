# Friction log

What ordinary use turned up, as opposed to what the checklists tested. Kept
because the phases ended and "usable day to day" did not, and because most of
these were invisible to every test that passed.

## The guarded tool renamed itself

`ToolDefinition.__init_subclass__` derives `name` from the class name, so
`GuardedFileEditorTool` introduced itself to the model as `guarded_file_editor`
while every profile, tool description and system prompt said `file_editor`.

Nothing failed. The profile resolved, the guard worked, the adversarial
checklist passed seventeen out of seventeen. The only symptom was the name in a
transcript, seen while watching a session do ordinary work.

Fixed by assigning the parent's name back after the class definition.

**The general lesson:** a test that asserts on behaviour will not notice a
change in what the model is *told*. Reading a transcript is the only thing that
does.

## `readonly` is genuinely usable for review

The open question was whether a preset with no terminal could navigate a
repository at all. It can: given four file paths in the task, a `readonly`
session read all four and worked from them.

It also navigates on its own, which was not obvious: the file editor's `view`
command works on a directory and returns "the files and directories up to 2
levels deep", so a `readonly` session can explore without a shell. Watching the
transcript showed it walking into `agentrt-sdk` and `agentrt-tools` — files the
task never named.

An earlier draft of this entry claimed the opposite, that without a terminal the
session could not find anything it had not been told about. That was written
from what the preset omits rather than from what the session did, and the
transcript contradicted it within the hour. What remains true is narrower: there
is no `grep`, so finding a *string* rather than a *path* is still hard.

## Iterating on the MCP surface needs an orchestrator restart

The MCP server is a child of Claude Code and holds the code it was started with.
Every change to the tool surface is invisible until Claude Code restarts or
reconnects. The CLI has no such problem, which is why every check in `tools/`
drives the client core directly.

Not a defect — it follows from MCP being a stdio child — but it shapes how the
work goes: build and verify through the CLI, and treat the MCP layer as
something confirmed separately.

## A dispatched review found a real hole; two of its three findings did not hold

The first real use of `readonly` was reviewing the permission code itself. It
reported six problems; five were real.

The one that mattered most for security had gone past seventeen adversarial
checks: a hard link inside the workspace is a second name for a file outside it,
and no path resolution can see that. Reproduced against the real credential,
then fixed by comparing device and inode against the runtime's own credential
files -- the `st_nlink` version is the next entry.

The one that mattered most in practice was duller. A refusal was returned
without `is_error`, and the observation's rendering computes
`change_applied = command != "view" and not is_error` — so the guard refused the
write and then told the agent it had succeeded. Nothing about that is a security
boundary; it just meant the enforcement was invisible to the thing being
enforced against.

One finding was asserted as fact and was false: the guard did approve a path
component of `.. `, but writing through it fails, because Windows does not strip
the trailing space to make it `..`.

## The obvious fix for the hard link would have broken the runtime

Refusing any file whose `st_nlink` exceeds one is the general form of the fix
and it closes the hole. It also makes the file editor useless: `uv` hard-links
packages from its global cache, so 30,656 of the 31,402 files in this project's
own virtualenv have more than one name. An agent asking to read a library's
source would have been refused.

Counting before shipping took one command. The version that reached the
repository compares device and inode against the runtime's own credential files,
which is narrower and honest about being narrow.

**The general lesson:** a security fix is a change like any other, and "does
this break ordinary use" is a question with a measurable answer. The instinct
that a stricter guard is a safer one is what makes that question easy to skip.

Two things worth carrying:

**The value was in the finding, not the report.** Taking the report at face
value would have meant a rushed "fix" for the second item and an inflated
account of the first. Testing each claim took three commands and separated them.
This is the `result` tool description's own rule applied to the tool's own
output.

**A review agent will exceed its brief, and that was where the value came from.**
Given four file paths, it read more than twenty, including the plan and the
recon notes. It had to be interrupted to produce the report at all. For code
generation that behaviour wasted an hour; for review it is what found the hard
link, because the connection between "the guard resolves paths" and "the state
directory sits on the same volume" is not in any one file. The lesson is not
"stop agents exploring" but that the same behaviour is waste in one task and the
whole point in another.

## The verification step did not work on any real repository

`artifacts` is the answer to "did the session actually do it", and the `result`
tool description points at it in as many words. It listed every file in the
workspace, sorted by path, first two hundred.

For an empty scratch directory that is indistinguishable from the right answer,
and every workspace used while building this was one. Measured against a
repository it is not merely noisy. With a `.venv` at the root, all two hundred
returned files are dependency files and nothing the session wrote appears at
all, because `.` sorts before every letter. Simulated with this project's own
virtualenv file list placed at a repository root: the agent's output would have
been entry 31,512 of 31,514. Over this project's actual tree the ordering is
kinder and still wrong -- `tools/spend.py` is entry 33,300 of 33,301, behind the
`packages/` directory.

It now reports what changed since the session started, newest first, and prunes
dependency trees and tool caches. Two files came back from a four-hundred-file
workspace in `tools/probe_artifacts.py`, which seeds a `.venv` and pre-existing
sources *before* dispatching so the empty-directory case can never be the only
one tested again.

Three things fell out of building it:

**The wrong question passed every test because the test shared its premise.**
Nothing here was subtle. The defect is visible by reading the function — if you
read it while thinking about a repository, which nobody does while dogfooding a
runtime in scratch directories.

**A grace period was added and then removed by measurement.** Two seconds of
slack covers FAT's timestamp resolution. It also attributes a file the
orchestrator seeded immediately before dispatching to the session, which is the
common case: `tools/adversarial.py` seeds and dispatches microseconds apart. The
first test written caught it. Insuring the rare case by being wrong about the
common one is the wrong trade, and the FAT limitation is documented instead.

**Timezone was the trap worth naming.** The daemon reports `created_at` in UTC
with a `Z`; this machine runs at UTC+7. Comparing that to a local clock puts
every file on the wrong side of the boundary by seven hours, and the failure is
silent — the list is simply wrong, in a direction that depends on the hour.
Testing both directions, a file written before and a file written after, is what
distinguishes a working filter from one that happens to include everything.

## Nine findings from the second review, and what each turned out to be

Dispatched through MCP, `readonly`, against the `artifacts` rewrite and the
probes. Recorded in full because the disposition is the interesting part: a
report is a list of claims, and the work is deciding which ones are true.

**Fixed, and the one that mattered.** The `path` branch of `artifacts` asked
only whether a path was inside the workspace. `check_path` asks that and then
whether the file is one of the runtime's credentials under another name. So a
hard link in a workspace was refused to the agent and served to the
orchestrator -- the same leak as last time, through the other door, and into a
model's context rather than an agent's. Reproduced against the real credential
before fixing. The branch now calls `check_path` instead of reimplementing half
of it.

**Fixed, three more.** `artifacts` reports `filtered`, because an unparseable
`created_at` silently disabled the time filter and returned every file, which is
the behaviour the rewrite replaced. `probe_artifacts` hard-coded a Windows
absolute path, so its "absolute path is refused" case would have failed against
correct code on Linux. `probe_parallel` dispatched into empty workspaces, so its
"alpha wrote alpha.txt" check passed for a listing that filtered nothing at all.

**True and already documented.** Coarse filesystem timestamps can drop a
session's output on FAT: the no-grace trade-off, argued in `_started_at` and
recorded above. A second writer in the same workspace is attributed to the
session: that is the limitation the tool description now names fourth, and it
demonstrated itself an hour later when `artifacts` on this very session listed
six files, every one of them written by the orchestrator while it worked.

**True, unfixed, and worth stating.** `os.stat` follows file symlinks, so a
symlink in a workspace pointing outside it reports the target's size and
modification time. Metadata only -- reading the content still goes through
`check_path`, which resolves the link and finds it outside. Left alone because a
symlink placed in a workspace is the operator's decision, but it is not nothing.

**Checked and not true here.** The report hedged that `datetime.fromisoformat`
rejects a trailing `Z` before Python 3.11. It does, and this project requires
3.12; every form the daemon emits parses, measured. TOCTOU between the client's
check and the daemon's open is real as a race and does not matter under a threat
model where `workspace` already grants a shell -- the report said as much itself.

The session reported its own line numbers as unreliable before giving them,
having been unable to re-open the file. That is worth more than the findings it
got right: an agent that flags the weakness in its own evidence is one whose
other claims are worth the time it takes to check.

## The first dispatch through MCP failed on the task, not the runtime

Driving the tools as an orchestrator for real, the first dispatch carried two
mistakes, and both came from following the `dispatch` description as written.

**It told a `readonly` session to write a report file.** The description says to
choose `readonly` for review work, and four lines later says to name the files
the session should create. Both are good advice and together they produce an
instruction the session cannot carry out. It now says so where the reader will
be standing when it matters.

**It located code by function name in a 737-line file.** The session read the
whole file in its first seven seconds, then had to find `artifacts` again and
spent two five-minute turns viewing ranges around `_token` and `_resolve_session`
instead, reporting that "the range views seem to be returning odd output" -- the
tool was fine, the ranges were wrong. Three line ranges from one `grep -n` would
have cost a line of the task.

That second one is the goal's own direction applied to the goal: the fix is
better input, not a better-behaved agent. And it is now the pattern across every
review dispatch made here. `70652c62` had to be interrupted for reading twenty
files when it was given four. This one had to be interrupted for hunting through
one file it had already read. **Review work needs tighter input than code
generation, not looser**, because there is no failing test to pull it back.

The redirect itself worked exactly as the `control` description promises:
`interrupt` at 09:17:45, `send` with the line numbers at 09:17:55, and the
session came back with "I have what I need from my initial full read" and took
exactly the one look it was told it could. Ten seconds and one message to turn a
session around, against the ten minutes it had already spent going the wrong way.

## `paused` is not a synonym for `residue`

A probe left a paused session behind on every run, so the tidy-up was obvious:
list the sessions, delete the paused ones. That loop deleted one and then
crashed on an emoji in a title before reaching the second, which was
`6ff256c9` -- the session `../results/p3.md` names in a heading as the one not to
delete. It is paused because it was interrupted, which is exactly why it is
evidence.

Nothing documented was lost: the one that went, `12023d33`, is referenced
nowhere. That is luck, not method. The rule that would have prevented it is one
already written down -- look at what you are deleting before you delete it --
and the filter that felt like a description of residue was a description of
state.

Two things follow.

**A probe should clean up what it creates, at the time it creates it.** The
cap check now deletes its own holder in the same function that dispatches it,
so there is never a pile to tidy and never a reason to write a filter over
somebody else's sessions.

**"Do not delete" living in prose is the underlying problem.** Two sessions are
load-bearing evidence and the only thing protecting them is a heading in a
document, which no tool reads. The daemon accepts `tags` on dispatch and
`PATCH` updates them, so a durable marker is available and unexposed. Whether
to spend surface on it is a decision, not a fix.

## One bug that was mine, not the product's

A transcript rendered `# P4 — permission` as `# P4 â€” permission`, which looks
exactly like a file being read as cp1252. It was not. The file editor detects
UTF-8 on that file with 0.96 confidence and reads it correctly; the mojibake
came from a display script of mine that piped the CLI's UTF-8 output into
`sys.stdin.read()`, which decodes with the console codepage on Windows.

Recorded because the wrong conclusion was one step away and would have sent a
change into vendored encoding code that was working correctly. On Windows,
suspect the pipe before the program.

## `list --limit` above 100 is a 500, not a validation error

Restoring onto Ubuntu, `agentrt list --limit 200` returned

    {"error": "ClientError",
     "message": "{\"detail\":\"Internal Server Error\",\"exception\":\"\", ...}"}

The ceiling is exactly 100: `--limit 100` returns all 77 sessions, `--limit 101`
is a 500. Measured across 100, 101, 128, 150, 199, 200.

Three things make this worse than a wrong number. The `exception` field is
empty, so the response says only that something failed server-side. The CLI
passes `--limit` through without validation and `list --help` documents no
range, so nothing at the point of use suggests a bound exists. And the failure
mode for "I want to see everything" is silence rather than a clamp.

**The general lesson:** a bound the caller cannot see is a bound the caller will
cross. Either clamp it or say what it is; returning a 500 with an empty
exception does neither.

## The prose that protects sessions failed a second time

The entry above concluded that "do not delete" living only in a document is the
underlying problem, and recommended tags. Restoring onto Linux produced the
second instance within a week.

Every one of the 77 restored conversations carries a Windows `working_dir`
(`C:\Users\ADMIN\...`), so none is resumable and the obvious cleanup is "delete
everything Windows-origin". That filter selects 76 of 77 -- and three of them,
`6ff256c9`, `70652c62` and `e8d1dd8f`, are cited in `../results/p3.md` and
`../results/p4.md`, two under literal `## Do not delete session ...` headings.

What caught it was grepping the docs for eight-hex-digit ids and intersecting
that with the delete list, by hand, after the list was already built. Nothing in
the runtime would have objected.

**The general lesson:** the filter that looks like a description of residue
keeps turning out to be a description of state. `tags` already exists on
dispatch and through `PATCH`; until evidence sessions carry one, the protection
is a string in a Markdown file that no tool reads.

## Deleting sessions erases the spend ledger

`tools/spend.py` derives cost from the conversation store, because the daemon
reports usage and 9Router publishes no prices. That store is also what
`agentrt delete` removes. So cleaning up sessions silently rewrites the budget
record.

Measured today, either side of deleting 73 restored Windows conversations:

    before   COMBINED ~$0.8427
    after    COMBINED ~$0.3908

No money came back. The $2 cap is tracked against a number that fell by $0.45
because rows were removed from the thing doing the tracking, and it will
under-report by that much for the rest of the project.

The true figure is reconstructible only because the before-reading happened to
be taken: $0.8427 at restore, plus $0.0745 of sessions dispatched by the Linux
baseline runs, is about $0.92 of $2.

**The general lesson:** a ledger stored in the same place as the thing it
measures is not a ledger. Either spend must accumulate somewhere deletion does
not reach -- `tools/spend.json` already exists for the delegate calls and could
carry a running total for sessions -- or `delete` has to fold the session's cost
into that file before removing it. Until then, read `spend.py` before any
cleanup, not after.

## Codex reaches the MCP server but `exec` cannot approve the call

Wiring Codex took one command -- `codex mcp add agentrt -- <abs path to
agentrt-mcp>` -- and the absolute path matters: `~/.local/bin` is not on the
PATH a spawned stdio server inherits, and `codex` itself is already broken the
same way on this machine (its shebang runs `env node`, and `node` lives under
nvm).

`codex doctor --all` then reports `✓ mcp 1 server (1 stdio) · 0 disabled`, which
is config validation and proves nothing about the connection. The first real
run failed:

    mcp: agentrt/profiles started
    mcp: agentrt/profiles (failed)
    MCP tool call requires approval, but approval policy is never

That is not the sandbox, which was the obvious suspect given `codex doctor`
reports `restricted fs + restricted network`. `codex exec` sets
`approval: never`, meaning *never ask*, so every tool call that would need
approval fails outright. An interactive session prompts instead and works.
Non-interactive use needs either project trust in `~/.codex/config.toml` or
`--dangerously-bypass-approvals-and-sandbox`.

With approvals bypassed, the whole path works: Codex dispatched a session with
`max_iterations` 6 into `/tmp/codex_ws`, polled `status` to `finished`, and
`artifacts` reported `CODEX.txt`. Checked against the filesystem rather than the
transcript, per the rule above: the file exists and contains `wired`.

**The general lesson:** "started" then "(failed)" is a connection that worked.
Read the reason before re-checking the wiring -- the sandbox was innocent and
half an hour could have gone into it.

## The MCP server reports the wrong version

`initialize` returns `serverInfo.name = "agentrt"` with
`serverInfo.version = "1.28.1"`, which is the version of the `mcp` library, not
of agentrt. FastMCP fills it in when the server does not set one. A client
cannot tell which agentrt it is talking to, which is the one thing that field
exists for. Cosmetic until two versions are in use somewhere.

## A workspace with side effects gets built twice, and the second time is not a copy

Found repeating the Docker experiment on native Linux. `_create_conversation`
persists a new conversation by dumping the just-validated `request.workspace`
to JSON and splatting it into `StoredConversation(id=..., **request_data)`.
`StoredConversation.workspace` is typed `BaseWorkspace`, so Pydantic validates
that dict as a new model instead of accepting the live object already sitting
in memory. For `LocalWorkspace` that is a no-op -- `model_post_init` just
touches a directory -- so it has never been visible. `DockerWorkspace` starts a
container in `model_post_init`; validating its dump starts a *second* one,
with the same `host_port` (already fixed by the first), and the second
correctly finds that port taken -- by the container the first, successful
construction is still running.

The client sees only the second failure: `RuntimeError: Port <port> is not
available`, reported as a 500. Nothing about that message says a container is
running. `docker ps` was the only way to find the four orphaned, running
containers this produced across four attempts -- one per request, all healthy,
none referenced by anything, since the exception happens before `stored` is
ever assigned. `agentrt list` shows nothing for them.

This is also a correction of the Windows-era `docker-recon.md`, which saw the
identical port error there and concluded it was specific to docker being
reached through a WSL proxy. It reproduced on a machine with no WSL and no
proxy, so that theory was wrong -- the port conflict was real on Windows too,
caused there by the same double construction, just never traced past the
error message to the container it left running.

**The general lesson:** a discriminated-union field that is safe to
re-validate from a dump for one member is not safe for all of them. The
members that matter are the ones with a `model_post_init` that does something
in the world -- and those are exactly the ones a quick look at the harmless
member would miss.

## Fixed: the double-construction that orphaned Docker containers

`_create_conversation` now excludes `workspace` from the JSON dump it builds
for `StoredConversation` and reattaches the live `request.workspace` object
afterward, instead of letting it round-trip through a dict. The mechanism this
relies on already existed: `DiscriminatedUnionMixin._validate_subtype` (in
`agentrt/sdk/utils/models.py`) short-circuits with `if isinstance(data, cls):
return data` when the value handed to a workspace-typed field is already the
right type, skipping `model_post_init` entirely. Dumping to a dict first
defeated that short-circuit on every single dispatch -- a dict is never
`isinstance(data, cls)` -- which is why `LocalWorkspace` (whose
`model_post_init` only touches a directory) never surfaced this, and
`DockerWorkspace` (whose `model_post_init` starts a container) leaked one on
every attempt.

Verified by repeating the exact dispatch that produced four orphaned
containers before the fix: it now produces one, and `docker ps` stays at one
entry through the attempt. Regression-checked with `cli_loop.py` and
`adversarial.py`, both still fully passing, since this is on the path every
conversation create goes through, not only Docker's.

Widening `ConversationConfig.workspace` to accept the fix's beneficiary
(`DockerWorkspace`) was tried again on top of this and reverted again -- see
the entry above and `docker-recon.md`. The type stays narrow because nothing
downstream can use a wider one yet; landing it alone would only reintroduce
"looks supported from outside" for a different field.

## The first LLM call can hang forever, and `finalize` cannot touch it

Two sessions dispatched in the same instant (`b51ec288`, `28c02d3d`, both
20:40:55 UTC 2026-09-15) sat at `iterations_used: 0` for 7m34s with no
observation. `finalize()` returned 200 and a partial result at 03:48:29 local
without stopping anything -- the run stayed `running`. Only a manual
`interrupt()` at 03:48:40 actually ended it: the log shows
`interrupt(): cancelled in-flight arun() task`, proving the task was still
genuinely alive, not orphaned.

**First suspect, ruled out.** A burst of 142 `Failed to decrypt secret value`
warnings landed in the same two seconds the sessions were created, which looked
causal. It is not. Bucketing the full daemon log by restart boundary shows the
burst at *every* restart since 2026-09-09, scaling with the number of stored
sessions, including dozens of restarts where dispatches worked fine through
H4-H7. `AGENTRT_SECRET_KEY` is unset, so the cipher key is ephemeral per
restart by design -- already known, harmless (`docker-recon.md`). And decisively:
the LLM profile's own `api_key` is a plain `sk-...` string, never Fernet-
encrypted, so it was never a candidate for this cipher's failures at all.
Nearby in time was not the same subsystem.

**What the evidence actually shows.** Both conversations' event logs stop dead
after `ConversationStateUpdateEvent{key: consumed_user_message_id}` --
`SystemPromptEvent`, the user `MessageEvent`, state bookkeeping, then nothing.
Zero `ActionEvent`, zero `ObservationEvent`. The agent loop reached its first
LLM completion call and that call never returned. The daemon log carries no
retry, timeout, or error trace for the outbound call during the whole gap --
only the orchestrator's own periodic status polls. Scanned across all 75
stored sessions, this signature (reached `consumed_user_message_id`, zero
`ActionEvent`) appears in exactly these two and no others: not a systemic
defect, an isolated stall, most likely a transient provider/network condition
at that specific moment (both sessions submitted ~200ms apart, ~1m48s after a
daemon restart).

**Why `finalize` did not stop it.** `_pause_to_boundary()` is cooperative: it
retries `pause()` up to three times over ~0.6s total, then gives up and
returns whatever partial state exists with the run still `running` -- by
design, per its own comment. `pause()` sets a flag the agent loop checks
between steps; it has nothing to act on when the loop is blocked inside its
first, not-yet-returned LLM call. Only `interrupt()` reaches that: it cancels
the `arun()` task directly rather than waiting for a safe point.

**Not proposed: a general stall watchdog.** H7 already measured and rejected
one -- a session can legitimately produce nothing for minutes while composing
one long answer, and killing on silence would discard real work. `iterations_used
== 0` with zero persisted events is a different case: nothing has run yet, so
there is nothing to discard. The narrow fix that survives H7's own argument is
a **start deadline** (bounded time to the first event), not a general
stall timeout.

**Open, not fixed.** Two changes scoped, neither shipped yet:

- A start deadline distinct from the rejected stall watchdog, per above.
- `finalize` either escalates to `interrupt()` once its cooperative window is
  exhausted, or its docstring stops claiming "stop a session at a safe
  boundary" when the measured behavior is a best-effort 0.6s attempt.

Also worth having independent of root cause: the daemon logs nothing at all
about what an outbound LLM call is doing while it runs. That gap is what made
this take log archaeology instead of a status field.

## The cache-hit figure was wrong, and the bill was what settled it

A session's provider reported heavy prompt-cache misses, and `tools/spend.py`
agreed: 4% hit across the whole store, on a provider where the cached portion is
much cheaper. Two readings were possible -- caching is broken now, or the number
is dominated by history -- and they call for opposite work.

**The first answer I reached was wrong, and this entry keeps it because the way
it was wrong is the useful part.** Sorting sessions by creation time and reading
each one's `cache_hit_rate` showed a sharp break: everything created before
2026-09-16 10:17 UTC at **0.00**, everything after at **0.86-0.97**. That break
lines up with H8 item 1 (`39b89ea`, the reasoning-content resend), whose bug
rebuilt prior assistant tool-call turns without their `reasoning_content` -- so
the prompt differed from the previous one on every call and a prefix cache could
never hit. The story fitted: a silent, expensive defect, fixed, with the metric
recovering at the fix.

**The provider's own billing export falsifies it.** For 2026-09-16, same key and
same day:

| source | prompt tokens | cached | hit rate |
|---|---|---|---|
| the daemon's records | 39.8M | 13.1M | 32.8% |
| the provider's bill | 78.5M | 76.6M | **97.6%** |

The bill includes the sessions the API scored at **0.00** -- `2e1d9496` among
them, which this plan had already cited for a different reason. Their tokens were
served from cache and billed at the cheap rate.

**The two totals are not comparable, and saying so matters.** The billing export
is per *account key*, and the orchestrator shares that key with the daemon: this
session's own `ANTHROPIC_AUTH_TOKEN` is the same credential as the daemon's
profile `api_key`. So most of the traffic billed under that key on 2026-09-17 --
202.7M tokens, with **zero** daemon sessions created that day -- is the
orchestrator, not AgentRT. The prompt-token columns above therefore measure two
different populations, and the gap between them is not evidence of anything.

**The evidence is the miss count, and it survives that.** The daemon's records
for 2026-09-16 imply 39.8M − 13.1M = **26.7M misses** for its own sessions. A
daemon call is a subset of the key's calls, so the key's misses must be at least
the daemon's -- and the key's whole-day bill reports **1.9M misses**, with every
other row on the account adding only ~0.3M more. 1.9M cannot contain 26.7M. So
the daemon recorded misses the provider never billed as misses, whatever share
of the key belongs to the orchestrator.

The magnitude is the interesting part: a session with a stable, append-only
prefix misses almost nothing -- the orchestrator's own traffic shows that, at
99.6% cached on 09-17 -- so a recorded 26.7M misses is not a plausible reading
of a workload that was being cached. So nothing was "paid in full",
and the causation above is wrong: what changed at that moment was **what the
daemon recorded**, not what the provider did. H8 item 1 is a prompt-content fix;
a content fix cannot explain a bill that shows caching throughout.

Two further things the cross-check turned up, both worth knowing on their own:

- **The API under-reported cache hits, and it did not under-report tokens.**
  For that day the daemon accounted for 13.1M cached where the provider billed
  76.6M across the account; the miss-count argument above is what establishes
  the gap, since the two prompt totals are not comparable. An orchestrator
  reading `usage` therefore saw a worse cache rate than reality, which is what
  misled me. The live check above shows this is no longer the case, so it is
  history rather than a standing defect -- but it is the reason the earlier
  conclusion in this entry looked right.
- The cause of the recording change is **not established** from the records
  available. It is not a route or model-name change (the same model name is
  recorded on both sides of the break) and not the reinstall that followed it
  (that landed two hours later). Something between 08:33 and 10:17 UTC on
  2026-09-16 changed what the daemon wrote down, and neither the daemon log nor
  the store explains it.

**What survives.** Caching works, verified twice and independently: the bill
says 97.6%, and two completions sent against the provider with an identical
prefix returned `prompt_cache_hit_tokens 1920/2144` on the second. The SDK reads
the right field (`prompt_tokens_details.cached_tokens`), so a 0.00 reading is
not a field-name mismatch. And a plausible cause was killed by measurement
rather than by argument -- `<CURRENT_DATETIME>` renders `datetime.now()` per
prompt build, but sending the same prompt with a *deliberately changing* dynamic
block still hit ~92%, because the provider matches the longest common prefix and
the long static block ahead of it is unchanged. Nothing should be changed to
"fix" that.

**Is the accounting defect live? Checked, and no.** A fresh two-call session
dispatched against the running daemon records `cache_read=17,920` of
`prompt_tokens=26,250` -- 68%, which is what a two-call session looks like, since
its first call cannot hit. So the daemon records cache correctly now; the
sessions scoring 0.00 were recorded by an earlier build, and the difference
between those records and the bill is history rather than a present fault.

One inference made on the way to that answer did not hold and is worth recording
as such: those sessions have an empty per-call `token_usages` list, which looked
like the signature of an older code path -- but the *current* daemon leaves that
list empty too, and `usage_by_model` falls back to the accumulated bucket for
every session on this deployment. The empty list is the normal shape here, not
evidence of age, so it cannot date the change. The per-call records the code
expects are simply not written by the build in use, which is its own small
observation and not the defect it first appeared to be.

**What shipped, revised.** `tools/probe_cache.py` still guards, but its subject
is now honest: it checks whether the API's accounting is self-consistent, since
a session with many calls and a near-zero recorded rate means caching is not
happening *or* the accounting of it is broken -- and the second is what happened
here. Its docstring carries the correction, including the instruction to check
the bill before deciding which side is wrong. `tools/spend.py` prints a
recent-window rate beside the lifetime one for the same reason a single lifetime
number read as a statement about today.

The general lesson is the one this repository keeps relearning: a number the
system reports about itself is a claim. The metric moved exactly where a code
change landed, which is what made the wrong story persuasive; the bill was the
independent instrument, and it disagreed.
