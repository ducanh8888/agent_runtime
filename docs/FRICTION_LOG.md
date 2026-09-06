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
then fixed with an `st_nlink` check.

The one that mattered most in practice was duller. A refusal was returned
without `is_error`, and the observation's rendering computes
`change_applied = command != "view" and not is_error` — so the guard refused the
write and then told the agent it had succeeded. Nothing about that is a security
boundary; it just meant the enforcement was invisible to the thing being
enforced against.

One finding was asserted as fact and was false: the guard did approve a path
component of `.. `, but writing through it fails, because Windows does not strip
the trailing space to make it `..`.

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

## One bug that was mine, not the product's

A transcript rendered `# P4 — permission` as `# P4 â€” permission`, which looks
exactly like a file being read as cp1252. It was not. The file editor detects
UTF-8 on that file with 0.96 confidence and reads it correctly; the mojibake
came from a display script of mine that piped the CLI's UTF-8 output into
`sys.stdin.read()`, which decodes with the console codepage on Windows.

Recorded because the wrong conclusion was one step away and would have sent a
change into vendored encoding code that was working correctly. On Windows,
suspect the pipe before the program.
