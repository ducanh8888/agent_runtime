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

## `paused` is not a synonym for `residue`

A probe left a paused session behind on every run, so the tidy-up was obvious:
list the sessions, delete the paused ones. That loop deleted one and then
crashed on an emoji in a title before reaching the second, which was
`6ff256c9` -- the session `P3_RESULT.md` names in a heading as the one not to
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
