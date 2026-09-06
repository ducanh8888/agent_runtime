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

The limit is real, though. Without a terminal there is no `ls` and no `grep`, so
a `readonly` session cannot *find* anything it was not told about. Naming the
files in the task is not a nicety for this preset, it is the precondition.

## Iterating on the MCP surface needs an orchestrator restart

The MCP server is a child of Claude Code and holds the code it was started with.
Every change to the tool surface is invisible until Claude Code restarts or
reconnects. The CLI has no such problem, which is why every check in `tools/`
drives the client core directly.

Not a defect — it follows from MCP being a stdio child — but it shapes how the
work goes: build and verify through the CLI, and treat the MCP layer as
something confirmed separately.

## One bug that was mine, not the product's

A transcript rendered `# P4 — permission` as `# P4 â€” permission`, which looks
exactly like a file being read as cp1252. It was not. The file editor detects
UTF-8 on that file with 0.96 confidence and reads it correctly; the mojibake
came from a display script of mine that piped the CLI's UTF-8 output into
`sys.stdin.read()`, which decodes with the console codepage on Windows.

Recorded because the wrong conclusion was one step away and would have sent a
change into vendored encoding code that was working correctly. On Windows,
suspect the pipe before the program.
