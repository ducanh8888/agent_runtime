# Claude Code native sub-agents — documented behaviour (research record)

Date: 2026-10-26. Evidence record for `../plans/iteration-budget.md`
section 11. Compiled from pages fetched that day; every fact carries the URL
it came from and, where useful, a verbatim quote. Items marked
**[inference]** are conclusions from the documented facts, not statements in
the docs. Do not rewrite conclusions here; add a new record if the docs
change.

Compiled from official Anthropic docs (code.claude.com/docs), the Agent SDK docs, and the
anthropics/claude-code CHANGELOG, fetched live in-session. Every fact cites the URL it was
fetched from and, where useful, a verbatim quote. Items marked **[inference]** are my
conclusions from the documented facts, not stated verbatim in the docs.

Primary source: https://code.claude.com/docs/en/sub-agents (the main subagents page).
Supporting: tools-reference, hooks, env-vars, cli-reference, headless, worktrees,
agent-view, agent-teams, cross-session-messaging, agent-sdk/subagents, agent-sdk/agent-loop,
and https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md

Note: https://platform.claude.com/docs/en/agents-sdk/subagents returns **404** — the SDK
docs now live at https://code.claude.com/docs/en/agent-sdk/subagents.

---

## 1. Lifecycle

### How a subagent is spawned

- Subagents are spawned by the **`Agent` tool** (renamed from `Task` in v2.1.63; `Task(...)`
  references still work as aliases).
  Source: https://code.claude.com/docs/en/sub-agents —
  "In version 2.1.63, the Task tool was renamed to Agent. Existing `Task(...)` references in
  settings and agent definitions still work as aliases."
  Also https://code.claude.com/docs/en/tools-reference — "`Agent` | Spawns a subagent with
  its own context window to handle a task."
- In SDK `tool_use` blocks the tool appears as `"Agent"` but as `"Task"` in the
  `system:init` tools list.
  Source: https://code.claude.com/docs/en/agent-sdk/subagents —
  'The tool appears as `"Agent"` in `tool_use` blocks but as `"Task"` in the `system:init`
  tools list.'
- Claude decides when to delegate based on the `description` field; users can force it by
  naming the subagent, @-mentioning it, or running the whole session as one agent via
  `--agent <name>` / the `agent` setting.
  Source: https://code.claude.com/docs/en/sub-agents ("Invoke subagents explicitly").

### What the parent passes on the Agent tool call

Documented parameters of the Agent call (assembled across pages):

- `subagent_type` — selects the agent definition. If omitted, the call gets the built-in
  `general-purpose` subagent; if that isn't registered it fails with
  `subagent_type is required`.
  Source: https://code.claude.com/docs/en/sub-agents — "An Agent tool call that omits
  `subagent_type` fails with `subagent_type is required` when the session has no
  `general-purpose` subagent to fall back on." Matching is "case- and separator-insensitive"
  (CHANGELOG 2.1.140).
- `prompt` — the delegation message / task description ("Claude composes a delegation
  message that summarizes the task"). SDK docs: "The only content you pass from parent to
  subagent is the Agent tool's prompt string."
  Sources: https://code.claude.com/docs/en/sub-agents ("What loads at startup"),
  https://code.claude.com/docs/en/agent-sdk/subagents ("What subagents inherit").
- `model` — per-invocation model override; it is first in the resolution order and persists
  when the subagent is resumed or sent a follow-up.
  Source: https://code.claude.com/docs/en/sub-agents ("Choose a model") — "A per-invocation
  `model` parameter also applies when the subagent is resumed or sent a follow-up message."
- `effort` — per-invocation effort override (requires v2.1.292+); stays in effect on resume.
  Source: https://code.claude.com/docs/en/sub-agents — "it can also pass an `effort`
  parameter for that invocation. The parameter overrides the `effort` field and stays in
  effect when the subagent is resumed. … requires Claude Code v2.1.292 or later."
- `name` — names the subagent so it is addressable for messaging/resume; with agent teams
  enabled a named spawn launches a **teammate** instead (unless the call is a fork or passes
  `isolation`).
  Sources: https://code.claude.com/docs/en/sub-agents ("Subagent names"),
  https://code.claude.com/docs/en/agent-teams ("How Claude starts agent teams") —
  "Claude launches a teammate when it calls the Agent tool with a `name` while agent teams
  are enabled, unless the call is a fork or passes `isolation` on the call itself."
- `isolation` — per-call, e.g. `isolation: "worktree"`; also usable on a fork.
  Source: https://code.claude.com/docs/en/sub-agents — "When Claude spawns a fork through
  the Agent tool, it can pass `isolation: "worktree"`…"
- `run_in_background` — exists on the "subagent tools" (implied: the Agent tool), removed
  when `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1`.
  Source: https://code.claude.com/docs/en/env-vars — "Set to `1` to remove the
  `run_in_background` parameter from the Bash, PowerShell, and subagent tools…"
- `subagent_type: "fork"` — spawns a fork (inherits the whole parent conversation). On by
  default since v2.1.302 everywhere; `claude mcp serve` never offers it.
  Sources: https://code.claude.com/docs/en/agent-sdk/subagents — 'Claude can start forks in
  your application with nothing configured, by calling the Agent tool with
  `subagent_type: "fork"`.'; CHANGELOG 2.1.302.

The exact Agent-tool JSON schema (required vs optional fields) is not published in the docs
I read — see Open questions.

### Own context window

- "Each subagent runs in its own context window with a custom system prompt, specific tool
  access, and independent permissions."
  Source: https://code.claude.com/docs/en/sub-agents.
- "Each subagent starts with a fresh, isolated context window. It doesn't see your
  conversation history, the skills you've already invoked, or the files Claude has already
  read."
  Source: https://code.claude.com/docs/en/sub-agents ("What loads at startup").
- Exception: a **fork** "inherits the entire conversation so far instead of starting fresh"
  (same system prompt, tools, model, history, shared prompt cache).
  Source: https://code.claude.com/docs/en/sub-agents ("Fork the current conversation",
  "How forks differ from other subagents" table).
- A subagent's context window is sized by **its own model**, not the parent's:
  "a subagent's context window is sized by its own model, not the parent's."
  Source: https://code.claude.com/docs/en/sub-agents ("What loads at startup").

### What comes back to the parent

- Only the **final result** returns: "The subagent works through its task autonomously, then
  returns its result to the parent conversation. The parent doesn't see the subagent's
  intermediate tool calls or outputs, only that final result."
  Source: https://code.claude.com/docs/en/tools-reference ("Agent tool behavior").
- Foreground: result is the Agent tool's result. Background: "A background subagent's
  results reach Claude as a completion notification in a later turn."
  Source: https://code.claude.com/docs/en/sub-agents ("Tools, permissions, and results of
  background subagents").
- In the SDK, the Agent tool result includes `agentId: <id>` (except Explore/Plan, which are
  one-shot and return no id). Status values observed: `status: "async_launched"` for a
  background spawn; `status: "completed"` when Claude waits.
  Source: https://code.claude.com/docs/en/agent-sdk/subagents — "the Agent tool result
  includes a text block containing `agentId: <id>`" and "The Agent tool result has
  `status: "async_launched"`, and the report arrives in a later turn."
- The result is post-processed before the parent sees it:
  - "Subagent output scanning": reports are scanned for instruction-shaped patterns;
    imitations of `<system-reminder>`, `Human:`/`Assistant:` get a backslash inserted; a
    `[harness: subagent output matched instruction-shaped pattern(s):` line is prepended for
    some patterns. "The scan never removes or rewords anything."
    Source: https://code.claude.com/docs/en/sub-agents ("Subagent output scanning").
  - The report arrives "under a header marking it as subagent output" stating that
    instructions/approval claims inside carry no authority.
    Source: https://code.claude.com/docs/en/sub-agents ("Subagent output scanning");
    CHANGELOG 2.1.277 — "Changed subagent results to reach the main agent under a header
    marking them as subagent output."
  - In auto mode, the report is delivered through the dedicated `SubagentHandback` tool and
    the classifier reviews it before delivery (v2.1.271+).
    Source: https://code.claude.com/docs/en/tools-reference (tool table) — "Delivers a
    subagent's final report to whichever conversation receives that subagent's result.
    Provided only in auto mode… the classifier reviews the report before it's delivered."
- The parent may summarize; verbatim passthrough isn't guaranteed:
  "The parent receives the subagent's final report, but may summarize it in its own
  response."
  Source: https://code.claude.com/docs/en/agent-sdk/subagents ("What subagents inherit").

### Follow-up messages / resume

- Each Agent call creates a new instance; to continue a finished subagent the parent uses
  **`SendMessage`** with the agent's ID or name as `to`. No agent-teams requirement.
  Source: https://code.claude.com/docs/en/sub-agents ("Resume subagents") — "Claude uses the
  `SendMessage` tool with the agent's ID or name as the `to` field to resume it.
  `SendMessage` doesn't require agent teams to be enabled."
- "When Claude sends a completed subagent a message with the `SendMessage` tool, the
  subagent resumes in the background without a new `Agent` invocation."
  Source: https://code.claude.com/docs/en/sub-agents ("Resume subagents").
- "Resumed subagents retain their full conversation history, including all previous tool
  calls, results, and reasoning."
  Source: https://code.claude.com/docs/en/sub-agents ("Resume subagents").
- Resuming "starts a new run of the agent under the same ID"; the run keeps the tool set
  from the first run and reuses the warmed prompt cache. `SubagentStart` fires again on
  resume.
  Sources: https://code.claude.com/docs/en/sub-agents; https://code.claude.com/docs/en/hooks
  ("SubagentStart … Runs when Claude spawns a subagent with the Agent tool, when Claude
  resumes a subagent").
- A subagent **you** stopped (`x` in `/tasks`, SDK `stop_task`) is not auto-resumed;
  messages to it are refused ("the agent was cancelled"). One stopped via `TaskStop` by
  Claude **can** be resumed once its stopped run exits.
  Source: https://code.claude.com/docs/en/sub-agents ("Resume subagents").
- Name-vs-ID safety: "`SendMessage` checks that a name still refers to the same agent it
  reached earlier… If a newer agent has taken the name, Claude Code refuses the send."
  Source: https://code.claude.com/docs/en/sub-agents.
- After a restart+resume of the session, names given **since the last compaction** still
  resolve; older names need the agent ID (find it in `agent-{agentId}.meta.json`). Before
  v2.1.299 no names survived restart.
  Source: https://code.claude.com/docs/en/sub-agents ("Message a subagent by name after a
  restart").
- Users can also resume a subagent interactively by typing into its open transcript while
  its row is in the subagent panel.
  Source: https://code.claude.com/docs/en/sub-agents ("Resume subagents").
- Subagents themselves can hold `SendMessage` and message/resume each other; an agent that
  a subagent resumes reports back to that subagent (in interactive sessions). Named agents
  get a "sibling roster" system-reminder at start listing `main` and other named agents.
  Source: https://code.claude.com/docs/en/sub-agents ("Resume subagents",
  "What loads at startup" — "Sibling roster").
- Messages from the launching agent are treated as normal task direction, but: "no message
  from any agent counts as your approval for a pending permission prompt, and no agent
  message can change a subagent's permission settings, CLAUDE.md, or configuration."
  Source: https://code.claude.com/docs/en/sub-agents ("Resume subagents").

### Does a subagent survive the parent process?

- "Subagents work within a single session."
  Source: https://code.claude.com/docs/en/sub-agents (top-of-page NOTE). For parallel
  independent *sessions* the docs point to background agents (`claude agents` / agent view),
  which are full sessions supervised by a daemon, not subagents.
  Source: https://code.claude.com/docs/en/agent-view.
- Agent-teams limitation (evidence that background work is bound to the owning process):
  "an in-process teammate's own subagents run in the foreground, because a teammate's
  background work can't outlive the lead's process."
  Source: https://code.claude.com/docs/en/agent-teams ("Limitations").
- In `claude -p`, after the final turn the process stays open waiting for background work
  (subagents included) until it completes or the idle-wait ceiling hits (default 10 min,
  `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`); then "Claude Code stops whatever is still
  running … and drops its partial result."
  Source: https://code.claude.com/docs/en/headless ("Background tasks at exit");
  https://code.claude.com/docs/en/env-vars.
- Within a *background session*, in-flight work including background subagents is handed to
  the session's next process when the supervisor restarts it (unless
  `CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF=1`). That is session-process restart, not
  outliving the session.
  Source: https://code.claude.com/docs/en/env-vars (`CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF`).
- **[inference]** A subagent cannot outlive its owning session. The transcript files persist
  on disk and can be resumed when the session is resumed, but the running agent itself dies
  with the session/process. Nothing in the docs describes a detached subagent surviving
  `claude` exiting.

---

## 2. Termination and limits

### Turn caps

- **`maxTurns` frontmatter / AgentDefinition field**: "Maximum number of agentic turns
  before the subagent stops. When the subagent reaches the limit, Claude Code returns its
  output marked as partial, and Claude can resume it to continue. The partial marking
  requires Claude Code v2.1.246 or later."
  Source: https://code.claude.com/docs/en/sub-agents (frontmatter table); identical text at
  https://code.claude.com/docs/en/agent-sdk/subagents (AgentDefinition table).
  CHANGELOG 2.1.246: "a subagent that stops at its `maxTurns` limit now returns its output
  marked as partial, with a hint to continue it via `SendMessage`, instead of appearing
  finished."
- **`--max-turns` CLI flag**: "Limit the number of agentic turns (print mode only). Exits
  with an error when the limit is reached. No limit by default." Applies to the session
  loop, not per-subagent.
  Source: https://code.claude.com/docs/en/cli-reference.
- SDK `max_turns` / `maxTurns` option: "counts tool-use turns only"; when hit, the SDK
  returns a `ResultMessage` with subtype `error_max_turns`.
  Source: https://code.claude.com/docs/en/agent-sdk/agent-loop ("Turns and budget",
  result-subtype table: "`error_max_turns` | Hit the `maxTurns` limit before finishing").
- A plugin (mod) that keeps spawning subagents could previously keep a headless run alive
  past `--max-turns` — fixed: "Fixed a plugin keeping a headless run going past
  `--max-turns`, Stop, or ended input by spawning subagents or re-running a finished one"
  (CHANGELOG 2.1.304).

### Token / cost budgets

- No per-subagent token or cost budget field exists in frontmatter. The only spend cap is at
  the session/query level: `maxBudgetUsd` (TS) / `max_budget_usd` (Py) / `--max-budget-usd`.
  "The budget cap covers subagents: their spend counts toward the total. Once spend reaches
  the cap, spawning another subagent fails with `Budget limit reached`, and Claude Code
  stops any background subagents still running." Requires v2.1.217+; ends the query with
  `error_max_budget_usd`.
  Sources: https://code.claude.com/docs/en/agent-sdk/agent-loop ("Turns and budget"),
  https://code.claude.com/docs/en/agent-sdk/subagents ("Cap subagent depth, concurrency,
  and spend"), https://code.claude.com/docs/en/cli-reference (`--max-budget-usd`).
- Spend check granularity: "Claude Code compares spend with the cap after model responses
  arrive… Spend can therefore pass the cap by up to the cost of that one response, plus
  anything that subagents still running at that moment spend before they stop."
  Source: https://code.claude.com/docs/en/agent-sdk/agent-loop.
- Each subagent's requests "count toward the same usage limits as your main conversation"
  and toward `total_cost_usd` of the query.
  Sources: https://code.claude.com/docs/en/sub-agents; agent-sdk/subagents.
- Old per-session spawn cap: `CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION` (default 200) was
  removed in v2.1.224 and is a no-op; "There's no limit on the total number of subagents
  Claude can spawn over a session."
  Sources: https://code.claude.com/docs/en/env-vars;
  https://code.claude.com/docs/en/sub-agents ("Concurrent subagent limit");
  CHANGELOG 2.1.212 (added), 2.1.224 (removed).

### Timeouts

- **Stall timeout**: `CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS` — "Stall timeout in milliseconds
  for subagents. Also covers workflow agents on v2.1.286+. Default `600000` (10 minutes)…"
  Source: https://code.claude.com/docs/en/env-vars.
- No documented wall-clock lifetime limit for a subagent run as such. (Background *Bash
  commands* a subagent starts had a 1-hour cap until v2.1.260 via
  `CLAUDE_SUBAGENT_BG_SHELL_MAX_MS`, now removed — they "run until they exit or are
  stopped".)
  Sources: https://code.claude.com/docs/en/env-vars (`CLAUDE_SUBAGENT_BG_SHELL_MAX_MS` row);
  CHANGELOG 2.1.260.
- Mid-stream API cutoffs don't kill the subagent immediately: "When something cuts off a
  subagent's response mid-stream and the partial response contains text but no tool calls,
  Claude Code prompts the subagent to continue rather than ending the run… The run ends on
  the error only once those continuations are used up." (Number of allowed continuations
  not documented.)
  Source: https://code.claude.com/docs/en/sub-agents ("API errors in subagents").
- Fallback model chains keep a subagent alive across covered model failures; its report
  then opens with "⚠ This agent switched to <model> because its earlier model could not be
  used."
  Source: https://code.claude.com/docs/en/sub-agents ("API errors in subagents").

### What the parent receives on failure / termination

- Foreground subagent ending on an API error: if it produced text, "the Agent tool returns
  that partial output with a note that the subagent was cut off"; if it produced nothing or
  only tool calls, it fails with `Agent terminated early due to an API error` + detail.
- Background subagent: "marked failed, and the message Claude receives when it ends names
  the API error and includes the subagent's last output, so partial work isn't lost."
  Source: https://code.claude.com/docs/en/sub-agents ("API errors in subagents").
- "An API error that ends the subagent early, such as a rate limit, is never delivered as
  its result."
  Source: https://code.claude.com/docs/en/agent-sdk/subagents.
- Zero resolved tools: the Agent tool "usually returns an error naming the entries instead
  of launching the subagent" (`Agent would be spawned with zero tools`).
  Source: https://code.claude.com/docs/en/tools-reference ("Agent tool behavior").
- Concurrent-limit refusal: error `Concurrent subagent limit reached`, "the error tells
  Claude not to retry."
  Source: https://code.claude.com/docs/en/sub-agents ("Concurrent subagent limit").
- Stopped by user: `x` in `/tasks` or SDK `stop_task`; by Claude: `TaskStop` tool by ID or
  name. Source: https://code.claude.com/docs/en/tools-reference (TaskStop row);
  https://code.claude.com/docs/en/sub-agents.

### How a subagent decides it is done

- **[inference + partial doc]**: the same agentic-loop rule as the main conversation — the
  loop ends when the model produces a response with no tool calls. SDK docs describe the
  loop ending when Claude "produces output with no tool calls" for sessions generally.
  Source: https://code.claude.com/docs/en/agent-sdk/agent-loop. For subagents specifically,
  the final assistant text (or `SubagentHandback` tool call in auto mode) is the report;
  `SubagentStop` "Runs when a Claude Code subagent has finished responding."
  Sources: https://code.claude.com/docs/en/hooks (SubagentStop);
  https://code.claude.com/docs/en/tools-reference (SubagentHandback row).
- CHANGELOG 2.1.285: "Improved subagents in auto mode: a subagent's run now ends as soon as
  it hands its report back to its caller, instead of taking extra turns that reach no one."

### Stop / SubagentStop hooks

- `SubagentStop` "Runs when a Claude Code subagent has finished responding. Matches on agent
  type." It also fires for internal helper agents (prompt suggestions, `/btw`) — `agent_type`
  is then the session's own `--agent` name or empty string.
  Source: https://code.claude.com/docs/en/hooks (SubagentStop).
- A `Stop` hook placed in a **subagent's frontmatter** is "automatically converted to
  `SubagentStop` events" when it runs as a subagent.
  Source: https://code.claude.com/docs/en/sub-agents ("Hooks in subagent frontmatter").
- Blocking: "Returning `decision: "block"` with a `reason` keeps the subagent running and
  delivers `reason` to the subagent as its next instruction. A hook that blocks by exiting 2
  delivers its stderr message the same way." `hookSpecificOutput.additionalContext` gives
  non-error feedback that also keeps it running.
  Source: https://code.claude.com/docs/en/hooks ("SubagentStop", "Stop decision control").
- `stop_hook_active` input field is `true` "when Claude Code is already continuing as a
  result of a stop hook" — intended so hooks can avoid blocking on a condition that will
  never resolve. Received by both `Stop` and `SubagentStop`.
  Source: https://code.claude.com/docs/en/hooks ("Stop input", "SubagentStop input").
- **Continuation cap**: "Claude Code applies an 8-consecutive-continuation cap: after stop
  hooks have continued the turn eight times in a row, Claude Code overrides the next block
  and ends the turn. The count of consecutive continuations resets each time Claude calls a
  tool. To raise the cap, set `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`." (`0` disables the cap;
  applies to Stop and SubagentStop.)
  Sources: https://code.claude.com/docs/en/hooks ("Stop input");
  https://code.claude.com/docs/en/env-vars (`CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` row).
- `SubagentStop` input additionally carries `agent_id`, `agent_type`,
  `agent_transcript_path` (the subagent's own transcript in the `subagents/` folder),
  `last_assistant_message`, plus `background_tasks` and `session_crons` arrays **scoped to
  the parent session**. With `SubagentHandback` (auto mode), `last_assistant_message` is
  the closing text, not the delivered report; the report is `tool_input.message` of the
  handback call.
  Source: https://code.claude.com/docs/en/hooks ("SubagentStop input").
- `SubagentStart` "can't block subagent creation, but they can inject context into the
  subagent" via `hookSpecificOutput.additionalContext`. On re-runs of the same subagent the
  context is re-injected only if not already present; after auto-compaction discards it,
  it's injected again.
  Source: https://code.claude.com/docs/en/hooks ("SubagentStart").
- `SubagentStart` fires on spawn, on resume, and each time an in-process teammate handles a
  new message.
  Source: https://code.claude.com/docs/en/hooks ("SubagentStart").

---

## 3. Context management

### What loads at startup (non-fork subagent)

Source: https://code.claude.com/docs/en/sub-agents ("What loads at startup") —
"A non-fork subagent's initial context contains:"

- **System prompt**: "the agent's own prompt plus environment details that Claude Code
  appends, not the Claude Code system prompt. Custom subagents define theirs in the
  markdown body or `prompt` field."
- **Task message**: "the delegation prompt Claude writes when it hands off the work."
- **CLAUDE.md files**: "every level of the CLAUDE.md hierarchy the main conversation loads,
  including `~/.claude/CLAUDE.md`, project rules, `CLAUDE.local.md`, managed policy files,
  and any `AGENTS.md` files loaded as project instructions." Exceptions: built-in Explore
  and Plan skip CLAUDE.md; `omitClaudeMd: true` loads only managed policy files (or none
  for managed-scope definitions). Path-scoped rules / nested CLAUDE.md still lazy-load on
  file access even for Explore/Plan/omitClaudeMd agents.
- **Git status snapshot** at subagent start (worktree-aware); absent outside git or when
  `includeGitInstructions` is off; Explore and Plan always skip it.
- **Preloaded skills**: full content of skills named in `skills:` (max first 32 distinct
  names; built-ins don't preload).
- **Sibling roster**: system-reminder listing `main` and all named agents, only when the
  subagent has `SendMessage` and another agent has a name; it's a start-time snapshot.

Never reaches a non-fork subagent: the parent's conversation history, output style, the
main conversation's auto memory, and the parent's context-window size (the subagent's model
sets its own window).
Source: https://code.claude.com/docs/en/sub-agents ("What loads at startup").

SDK variant: subagent gets its `prompt` + Agent-tool prompt + project CLAUDE.md (via
`settingSources`) + tool definitions; not the parent's system prompt or history.
Source: https://code.claude.com/docs/en/agent-sdk/subagents ("What subagents inherit").

### Auto-compaction inside a subagent

- "Subagents support automatic compaction. Compaction triggers as the subagent's own
  context window fills, and `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` applies to subagents as well."
  Source: https://code.claude.com/docs/en/sub-agents ("Auto-compaction"); env-vars page
  confirms it "Applies to both main conversations and subagents."
- Compaction events are logged in the subagent transcript as
  `{"type":"system","subtype":"compact_boundary","compactMetadata":{"trigger":"auto",
  "preTokens":…}}`.
  Source: https://code.claude.com/docs/en/sub-agents ("Auto-compaction").
- `autoCompactWindow` frontmatter (v2.1.296+) lowers a single subagent's auto-compact window
  to 100k–1M tokens; overridden by `CLAUDE_CODE_AUTO_COMPACT_WINDOW`.
  Source: https://code.claude.com/docs/en/sub-agents ("Set the auto-compact window for one
  subagent"); CHANGELOG 2.1.296.
- After compaction, SubagentStart-hook context is re-injected ("After auto-compaction
  discards that copy, Claude Code injects the next run's context again").
  Source: https://code.claude.com/docs/en/hooks ("SubagentStart").
- Whether CLAUDE.md / skills are *reloaded* after compaction: path-scoped rules and nested
  CLAUDE.md "load when the subagent works with files there" (lazy loading, independent of
  compaction). No doc statement that the initial CLAUDE.md/skill block is re-injected after
  a subagent's own compaction — see Open questions.
  Sources: https://code.claude.com/docs/en/sub-agents ("What loads at startup");
  lazy loading per https://code.claude.com/docs/en/hooks (`InstructionsLoaded` fires "when
  files are lazily loaded during a session", and now reports `agent_id`/`agent_type` for
  subagent file access — CHANGELOG 2.1.288).
- Extended thinking is inherited from the main conversation since v2.1.198 (no
  per-subagent thinking setting). Source: https://code.claude.com/docs/en/sub-agents
  ("Choose a model").

### Memory (`memory:` frontmatter)

- `memory: user | project | local` gives the subagent a persistent directory that survives
  across conversations:
  `user` → `~/.claude/agent-memory/<name>/`; `project` → `.claude/agent-memory/<name>/`;
  `local` → `.claude/agent-memory-local/<name>/`.
- When enabled: the system prompt includes memory read/write instructions plus the first
  200 lines or 25KB of `MEMORY.md` (whichever first), and Read/Write/Edit are auto-enabled.
- It's part of auto memory: `autoMemoryEnabled: false` or `CLAUDE_CODE_DISABLE_AUTO_MEMORY`
  disables it entirely.
  Source: https://code.claude.com/docs/en/sub-agents ("Enable persistent memory").

---

## 4. Background subagents

- Default: "With nothing configured, a subagent that Claude spawns from your main
  conversation starts in the background." (Background-by-default shipped in v2.1.198 per
  CHANGELOG; fork-mode default extended to `-p`/SDK/desktop/VS Code in v2.1.302.)
  Sources: https://code.claude.com/docs/en/sub-agents ("Run subagents in foreground or
  background"); CHANGELOG 2.1.198, 2.1.302.
- In `claude -p` / SDK, Claude can choose to wait (blocks, `status: "completed"`); in
  interactive terminal/VS Code/desktop, waiting requires fork mode **off**
  (`CLAUDE_CODE_FORK_SUBAGENT=0`). `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1` makes Claude
  wait for every subagent and disables Ctrl+B.
  Source: https://code.claude.com/docs/en/sub-agents ("Let Claude wait for a subagent").
- `background: true` frontmatter forces the subagent to stay in the background even when
  Claude asks to wait. SDK AgentDefinition has the same `background` field.
  Sources: https://code.claude.com/docs/en/sub-agents (frontmatter);
  https://code.claude.com/docs/en/agent-sdk/subagents.
- Notification: "A background subagent's results reach Claude as a completion notification
  in a later turn. Claude waits for that notification before reporting the subagent's
  results, and if you ask about progress first, it reports that the subagent is still
  running." The notification is "marked as an automated event rather than a message from
  you" and (since 2.1.144) includes elapsed duration.
  Sources: https://code.claude.com/docs/en/sub-agents; CHANGELOG 2.1.144.
- Poll/wait: Claude can't busy-poll a handle; it either waits on the blocking call or gets
  the completion notification. It can check progress by opening the transcript (user can
  too), and can message the running subagent via `SendMessage`. There's no documented
  "wait with timeout" primitive for the parent model — [inference] from the documented
  mechanics.
- Permission prompts from a background subagent "surface in your main session… names the
  subagent that is asking. Approve… or press Esc to deny that one tool call without
  stopping the subagent." Session-lasting answers apply session-wide.
  Source: https://code.claude.com/docs/en/sub-agents ("Tools, permissions, and results of
  background subagents").
- A background subagent can leave background Bash/PowerShell commands running past its own
  turn; when they finish "Claude Code sends the subagent a notification" (it can be woken
  for it). Source: https://code.claude.com/docs/en/sub-agents.
- On parent exit (`claude -p`): the process waits for background subagents because "its
  result is part of the final output", subject to the 10-minute idle ceiling
  (`CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS`), then stops them and drops partial results.
  Source: https://code.claude.com/docs/en/headless ("Background tasks at exit").
- UI: finished background subagents clear their panel row immediately (footer shows
  `/tasks to see subagents` for 30 s); failed/stopped keep their row 30 s; `/tasks` lists
  completed subagents for 30 s and its detail view stays open.
  Source: https://code.claude.com/docs/en/sub-agents ("Finished subagents in the panel and
  task list").
- `CLAUDE_AUTO_BACKGROUND_TASKS=1` moves a subagent to the background after ~2 minutes
  automatically. `CLAUDE_CODE_WORKER_CHECKIN_SCHEDULE` configures check-in reminders to
  Claude about still-running background subagents (v2.1.283+).
  Source: https://code.claude.com/docs/en/env-vars.
- Distinct concept: **background sessions** (`claude agents` / `--bg` / `/bg`) are whole
  sessions supervised by a daemon (`claude daemon`), not subagents; subagents/teammates a
  session spawns don't appear as agent-view rows.
  Source: https://code.claude.com/docs/en/agent-view.

---

## 5. Concurrency and isolation

### Parallel subagents and limits

- Multiple subagents can run concurrently; each has its own context window and sends its
  own API requests. Docs recommend parallel research subagents.
  Sources: https://code.claude.com/docs/en/sub-agents ("Run parallel research");
  https://code.claude.com/docs/en/agent-sdk/subagents.
- **Concurrency cap**: default **20** concurrently running subagents per session;
  `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` adjusts it (can't disable; positive whole number;
  v2.1.217+). Overage fails with `Concurrent subagent limit reached` and tells Claude not to
  retry. Forks and resumes can push the count past the cap (they don't check it); ultracode
  sessions are exempt. No limit on total spawns per session (the 200/session cap was
  removed in v2.1.224).
  Sources: https://code.claude.com/docs/en/sub-agents ("Concurrent subagent limit");
  https://code.claude.com/docs/en/env-vars; CHANGELOG 2.1.217, 2.1.224.
- **Depth cap (nesting)**: "By default, a subagent can spawn subagents of its own, up to
  three layers below the main conversation." At the limit the `Agent` tool is withheld from
  the subagent's toolset (for forks it stays listed but errors). `CLAUDE_CODE_
  MAX_SUBAGENT_SPAWN_DEPTH` changes it; `1` disables nesting. History: nesting added
  v2.1.172 (5 layers), default 1 in 2.1.217–2.1.218, default 3 since 2.1.219. A nested
  background subagent that outlives its spawner "reports to your main conversation".
  Sources: https://code.claude.com/docs/en/sub-agents ("Let subagents spawn their own
  subagents"); https://code.claude.com/docs/en/env-vars; CHANGELOG 2.1.172, 2.1.217,
  2.1.219.
- Restrict spawnable types on a main-thread agent (`claude --agent`) with
  `Agent(type1, type2)` allowlist in `tools:`; in a subagent definition the parenthesized
  type list is ignored. Omit `Agent` from tools (or `disallowedTools`) to forbid spawning.
  Source: https://code.claude.com/docs/en/sub-agents ("Restrict which subagents can be
  spawned").
- Deny specific types session-wide: `permissions.deny` with `Agent(name)`, or
  `--disallowedTools "Agent(Explore)"`. Deny `Agent` itself to forbid all delegation.
  Source: https://code.claude.com/docs/en/sub-agents ("Disable specific subagents").

### Worktree isolation

- `isolation: worktree` runs the subagent in a temporary git worktree — "an isolated copy of
  the repository branched by default from your default branch rather than the parent
  session's `HEAD`. The worktree is automatically cleaned up if the subagent makes no
  changes"; a worktree with changes persists until the periodic sweep (cleanupPeriodDays).
  Sources: https://code.claude.com/docs/en/sub-agents (frontmatter);
  https://code.claude.com/docs/en/worktrees ("Isolate subagents with worktrees",
  "Clean up subagent and background-session worktrees").
- Enforcement: a worktree-isolated subagent's Bash/PowerShell commands get a working-
  directory check (command fails if cwd resolves to the main checkout), plus Bash command-
  content checks (blocks git redirection into the main checkout and commands it can't
  verify stay inside the worktree). Monitor commands get the same checks. If the *main
  session* itself is worktree-isolated, the same checks apply to all its subagents.
  Source: https://code.claude.com/docs/en/sub-agents ("Write subagent files");
  https://code.claude.com/docs/en/worktrees ("How Claude Code enforces isolation").
- A worktree-isolated subagent takes its startup instruction files (CLAUDE.md etc.) from
  the main conversation, not from the worktree copy (fix: double-loading of project
  CLAUDE.md from the worktree on first file read, CHANGELOG 2.1.286); its git snapshot
  shows the worktree's branch/status.
  Sources: https://code.claude.com/docs/en/worktrees; https://code.claude.com/docs/en/
  sub-agents ("What loads at startup").
- Base branch: same as `--worktree` — default branch, unless `worktree.baseRef: "head"`.
  Source: https://code.claude.com/docs/en/worktrees.

### Permission modes

- `permissionMode` frontmatter: `default`, `acceptEdits`, `auto`, `dontAsk`,
  `bypassPermissions`, `plan` (`manual` = alias for `default`). Unset → inherits main
  conversation's mode.
- Inheritance rules (main session wins in several cases): if main is `bypassPermissions`/
  `acceptEdits`/`auto`, the subagent runs in that mode and the field is ignored; if main is
  `plan`, the subagent stays in plan regardless; if main is `default`/`dontAsk`, the set
  value applies except `bypassPermissions` (kept only when main also bypasses) and `auto`
  when unavailable. Under auto mode the classifier evaluates the subagent's tool calls and
  reviews its final report before delivery.
  Source: https://code.claude.com/docs/en/sub-agents ("Permission modes").

### Tool restrictions

- `tools` = allowlist; `disallowedTools` = denylist applied first, then `tools` resolved on
  the remainder; a tool in both is removed. Neither set → inherits all subagent-available
  tools.
  Source: https://code.claude.com/docs/en/sub-agents ("Restrict which tools a subagent can
  use"); https://code.claude.com/docs/en/tools-reference.
- A `disallowedTools` entry with a specifier like `Bash(git push *)` removes the **whole**
  tool. MCP-wide entries: `mcp__server`, `mcp__server__*`, `mcp__*`.
  Source: https://code.claude.com/docs/en/sub-agents.
- Tools **removed from every subagent** (first filter): `Agent` (at depth limit),
  `AskUserQuestion`, `EndConversation`, `EnterPlanMode`, `ExitPlanMode` (unless
  `permissionMode: plan`), `ScheduleWakeup`, `WaitForMcpServers`, `Workflow`. Forks skip
  both filters and get the parent's exact pool.
- Tools a **background** subagent keeps (second filter): all MCP tools plus `Read, Grep,
  Glob, LSP, Bash, PowerShell, Edit, Write, NotebookEdit, WebFetch, WebSearch, TodoWrite,
  Skill, ToolSearch, EnterWorktree, ExitWorktree, Monitor, TaskStop, SendMessage,
  Artifact`, and `SubagentHandback` where applicable. Everything else built-in is removed —
  "the same definition can resolve to different tools in the foreground and the
  background."
  Source: https://code.claude.com/docs/en/sub-agents ("Available tools").
- `mcpServers` frontmatter: inline definitions (connect at subagent start, disconnect at
  finish, subject to folder-trust rules) or name references sharing the parent's
  connection. `--strict-mcp-config` doesn't filter servers passed via `--agents`/SDK.
  Source: https://code.claude.com/docs/en/sub-agents ("Scope MCP servers to a subagent",
  "Trust required for inline MCP servers").
- Hooks in frontmatter: `PreToolUse`/`PostToolUse`/`Stop`(→`SubagentStop`) etc., run only
  while that subagent is active; project-level agent files need folder trust for their
  hooks to run. Session-wide settings.json hooks also fire inside subagents.
  Source: https://code.claude.com/docs/en/sub-agents ("Define hooks for subagents",
  "Conditional rules with hooks").

### Nested subagents

- Covered above (depth cap). Extra details: a spawning subagent waits for each nested
  subagent's report while fork mode is on; with fork mode off in `-p`/SDK, nested spawns go
  to the background and the spawner doesn't wait. Terminal UI shows nesting as a tree with
  `(+N)` counts. Resumed subagents restore their original spawn depth; forks count toward
  the depth cap (CHANGELOG 2.1.190).
  Sources: https://code.claude.com/docs/en/sub-agents ("Let subagents spawn their own
  subagents"); CHANGELOG 2.1.190.

---

## 6. Observability

### What the user sees while a subagent runs

- Terminal: a **subagent panel** below the prompt input — one row per running agent
  (nested shown as a tree with `(+N)`). `Enter` opens the subagent's live transcript (tool
  calls, results, progress) and lets you type follow-up messages; `x` stops it; `Esc`
  returns focus. Delegation appears in the transcript as a tool-call row like
  `code-improver(Suggest code improvements)`.
  Sources: https://code.claude.com/docs/en/sub-agents ("Observe and steer running forks",
  "Let subagents spawn their own subagents", quickstart).
- `/tasks` lists running/finished subagents and (v2.1.242+) the model and effort each
  subagent runs on; `Enter` opens its transcript. `/list-agents` (ListAgents tool) lists
  messageable agents.
  Sources: https://code.claude.com/docs/en/sub-agents ("Choose a model");
  https://code.claude.com/docs/en/tools-reference (ListAgents).
- VS Code: background subagents show as one status line under their Agent card; an "agent
  map" (`N agents` footer pill) shows per-agent cards, Stop, and read-only transcripts
  (CHANGELOG 2.1.269, 2.1.302).
- Non-interactive/stream-json: subagent messages appear with `parent_tool_use_id` set;
  without `--forward-subagent-text` (v2.1.211+) or `CLAUDE_CODE_FORWARD_SUBAGENT_TEXT=1`,
  text/thinking of **foreground** subagents is omitted from the output stream (nested
  forwarding added later; per-run version requirements in headless docs).
  Sources: https://code.claude.com/docs/en/cli-reference (`--forward-subagent-text`);
  https://code.claude.com/docs/en/headless ("Follow subagent messages").
- SDK: messages from inside a subagent carry `parent_tool_use_id`; TS messages also carry
  `agent_id` matching the task events' `task_id` (SDK v0.3.292+). Task lifecycle events:
  `task_started`/`task_progress`/etc. exist in the SDK message stream.
  Source: https://code.claude.com/docs/en/agent-sdk/subagents ("Detect subagent
  invocation").
- OTEL/telemetry: subagent API requests carry `x-claude-code-agent-id` /
  `x-claude-code-parent-agent-id` headers and `agent_id`/`parent_agent_id` span attributes
  (CHANGELOG 2.1.139, 2.1.145). `subagentStatusLine` payload exists for custom status lines
  (CHANGELOG 2.1.215, 2.1.293).

### Transcripts on disk

- `~/.claude/projects/{project}/{sessionId}/subagents/agent-{agentId}.jsonl` per subagent;
  `agent-{agentId}.meta.json` holds metadata such as its name.
  Source: https://code.claude.com/docs/en/sub-agents ("Resume subagents",
  "Message a subagent by name after a restart"). Confirmed by SubagentStop's
  `agent_transcript_path` field.
  Source: https://code.claude.com/docs/en/hooks ("SubagentStop input").
- Subagent transcripts persist independently: unaffected by main-conversation compaction;
  resumable across restarts by resuming the session; auto-deleted after
  `cleanupPeriodDays` (30-day default).
  Source: https://code.claude.com/docs/en/sub-agents ("Resume subagents").
- `/resume` / `--resume` restores the session incl. agent IDs; "Fixed `--resume` not
  reporting background subagents that were running when the previous Claude Code process
  exited" (CHANGELOG 2.1.158). In-process **teammates** are NOT restored on resume
  (agent-teams limitation).
  Sources: CHANGELOG; https://code.claude.com/docs/en/agent-teams ("Limitations").

---

## 7. Subagent definition frontmatter — every documented field

Source for the whole table: https://code.claude.com/docs/en/sub-agents ("Frontmatter
reference"). Field names are camelCase; unknown fields are silently ignored (diagnosable
via `claude --debug` log `does not recognize or apply`, v2.1.296+). Only `name` and
`description` are required. The same fields are accepted in `--agents` JSON (except `color`
and `experimental`, which are ignored there) and most appear in the SDK `AgentDefinition`
(https://code.claude.com/docs/en/agent-sdk/subagents).

| Field | Required | Documented meaning | Default |
| :- | :- | :- | :- |
| `name` | Yes | Unique identifier, ≤256 chars, no `:` (reserved for plugin scoping); hooks receive it as `agent_type`; filename needn't match | — (required; file skipped without it) |
| `description` | Yes | When Claude should delegate to this subagent | — (required; file skipped without it) |
| `tools` | No | Allowlist of tools (comma-separated or YAML list); `Agent(type)` allowlist only meaningful for main-thread agents | Inherits every tool available to subagents |
| `disallowedTools` | No | Tools to remove from the inherited/specified set; applied before `tools`; specifier entries still remove whole tool | none |
| `model` | No | `sonnet`/`opus`/`haiku`/`fable`, full model ID, or `inherit` | resolved via model order: per-invocation param → frontmatter → `CLAUDE_CODE_SUBAGENT_MODEL` → main conversation's model |
| `permissionMode` | No | `default`, `acceptEdits`, `auto`, `dontAsk`, `bypassPermissions`, `plan`, `manual` (alias for default); ignored for plugin subagents | inherits main conversation's permission mode |
| `maxTurns` | No | Max agentic turns; on limit the output returns marked partial and the subagent can be resumed (partial marking v2.1.246+) | none (no turn limit) |
| `autoCompactWindow` | No | Lower auto-compact window for this subagent, 100000–1000000 tokens; v2.1.296+ | the window the subagent would otherwise inherit |
| `skills` | No | Skill names to preload — full content injected at startup; first 32 distinct names; doesn't restrict Skill-tool use | none |
| `mcpServers` | No | MCP servers for this subagent: name reference or inline definition; ignored for plugin subagents | none |
| `hooks` | No | Lifecycle hooks scoped to this subagent (Stop→SubagentStop at runtime); ignored for plugin subagents; project files need folder trust | none |
| `memory` | No | Persistent memory scope: `user`, `project`, `local`; requires auto memory enabled | none |
| `background` | No | `true` keeps the subagent in the background even when Claude asks to wait | subagents spawn in background by default anyway |
| `omitClaudeMd` | No | `true` omits user/project/local CLAUDE.md (managed policy still loads); ignored when run as main session agent; v2.1.271+ | `false` |
| `effort` | No | `low`/`medium`/`high`/`xhigh`/`max`; overrides session effort but not `CLAUDE_CODE_EFFORT_LEVEL` | session's effort |
| `isolation` | No | `worktree` → run in a temporary git worktree off the default branch; auto-removed if no changes | none (runs in session cwd) |
| `color` | No | Display color in task list/transcript: `red`,`blue`,`green`,`yellow`,`purple`,`orange`,`pink`,`cyan` | none |
| `initialPrompt` | No | Auto-submitted as first user turn when agent runs as **main session** via `--agent`/`agent` setting; commands/skills processed; prepended to user prompt; ignored for plugin subagents | none |
| `experimental` | No | Map of experimental options; only documented key is `cacheTtl` (`5m`/`1h`) for the subagent's prompt-cache TTL; file-only, ignored in `--agents`; v2.1.248+ | none |

Notes:
- Body of the file (after `---`) = the subagent's system prompt.
- Subagent scopes/precedence (highest→lowest): managed settings → `--agents` CLI JSON →
  `.claude/agents/` (project) → `~/.claude/agents/` (user) → plugin `agents/`. Programmatic
  SDK `agents` beat filesystem agents of the same name.
  Source: https://code.claude.com/docs/en/sub-agents ("Choose the subagent scope");
  https://code.claude.com/docs/en/agent-sdk/subagents.
- SDK `AgentDefinition` additionally documents `permissionMode`, `background`,
  `initialPrompt`, `autoCompactWindow` (TS only), `omitClaudeMd` (TS only) — Python keeps
  camelCase field names to match the wire format.
  Source: https://code.claude.com/docs/en/agent-sdk/subagents ("AgentDefinition
  configuration").

---

## 8. Open questions (not specified in the docs I read)

1. **Exact Agent tool input schema** — the docs describe `subagent_type`, `prompt`,
   `description`, `model`, `effort`, `name`, `isolation`, `run_in_background` usages but no
   formal schema with required/optional marks. (The lead's list included `description` and
   `run_in_background`; the transcript row shows a "short task description" in the call —
   e.g. `code-improver(Suggest code improvements)` — implying a description param, but I
   found no explicit schema doc.)
2. **Foreground subagent turn limit**: does `--max-turns` interact with foreground
   subagent turns (count them, or ignore them)? Docs only define `maxTurns` per-subagent
   frontmatter and `--max-turns` for the print-mode session.
3. **Post-compaction re-load inside a subagent**: docs confirm SubagentStart-hook context
   is re-injected after auto-compaction and that rules lazy-load on file access, but do not
   explicitly state whether the initial CLAUDE.md block or `skills:`-preloaded content is
   re-injected after the subagent's own compaction.
4. **Exact continuation count** for mid-stream cutoffs ("only once those continuations are
   used up" — number undocumented).
5. **Wall-clock timeout for a subagent run**: only a stall timeout is documented
   (`CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS`, default 10 min); no overall run-time cap is
   documented for interactive sessions.
6. **`/resume` of an individual subagent transcript from CLI**: docs describe resuming
   subagents *within* a session (SendMessage, typing into transcript) and resuming the
   session itself; no documented CLI to attach directly to `agent-{id}.jsonl`.
7. **What the parent sees per-turn for a running background subagent programmatically**:
   the notification mechanism and panel are documented; a documented "poll status" tool for
   the parent model is not (ListAgents lists agents; there's no documented WaitForAgent —
   `notify_when_idle` exists only for cross-session targets, and is refused for subagents/
   teammates).
8. **Whether `permissionMode: bypassPermissions` in frontmatter can ever take effect when
   the main session is not in bypass mode** — docs say "A subagent runs in this mode only
   when the main conversation does"; the v2.1.267 exception wording is ambiguous (it keeps
   the main mode in that case).
9. **Fork specifics**: whether a fork can itself be `maxTurns`-limited or configured beyond
   `isolation` at spawn is not documented; forks can't spawn further forks and keep `Agent`
   listed-but-erroring at the depth limit.

---

## Appendix A — env vars and flags relevant to subagents

From https://code.claude.com/docs/en/env-vars and /cli-reference (all fetched this session):

| Knob | Meaning | Default |
| :- | :- | :- |
| `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS` | running-subagent cap per session; exceeding → `Concurrent subagent limit reached` | 20 (v2.1.217+) |
| `CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH` | nesting layers below main conversation | 3 (v2.1.219+; `1` disables) |
| `CLAUDE_CODE_MAX_SUBAGENTS_PER_SESSION` | **removed v2.1.224, no-op** (was 200 total spawns) | — |
| `CLAUDE_CODE_FORK_SUBAGENT` | `0` turns fork mode off and lets Claude wait for subagents interactively | on (v2.1.302 everywhere; 2.1.232 terminal) |
| `CLAUDE_CODE_DISABLE_BACKGROUND_TASKS` | removes `run_in_background`, disables Ctrl+B, Claude waits for all subagents | off |
| `CLAUDE_CODE_DISABLE_EXPLORE_PLAN_AGENTS` | removes built-in Explore/Plan (v2.1.198+) | off |
| `CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS` | removes all built-in agents in `-p`/SDK; omitting `subagent_type` then errors | off |
| `CLAUDE_CODE_SUBAGENT_MODEL` | default subagent model (per-spawn param & frontmatter beat it since v2.1.251) | unset → inherit main |
| `CLAUDE_CODE_SUBAGENT_MODEL_FORCE` | forces one model onto all subagents/teammates/workflow agents (v2.1.257+) | off |
| `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` | consecutive Stop/SubagentStop continuations before forced end (`0` = uncapped) | 8 |
| `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE` | compaction trigger %; applies to subagents too | (default %) |
| `CLAUDE_ASYNC_AGENT_STALL_TIMEOUT_MS` | subagent stall timeout | 600000 (10 min) |
| `CLAUDE_CODE_PRINT_BG_WAIT_CEILING_MS` | `-p` post-turn idle wait for bg work incl. subagents | 600000 (10 min) |
| `CLAUDE_AUTO_BACKGROUND_TASKS` | auto-backgrounds a subagent after ~2 min | off |
| `CLAUDE_CODE_WORKER_CHECKIN_SCHEDULE` | check-in reminders for still-running bg subagents (v2.1.283+) | unset (none) |
| `CLAUDE_CODE_FORWARD_SUBAGENT_TEXT` | emit subagent text/thinking in stream-json (v2.1.211+) | off |
| `CLAUDE_CODE_SUBAGENT_PROMPT_CACHE_TTL` / `subagentPromptCacheTtl` | prompt-cache TTL for non-main requests incl. subagents | 5m |
| `CLAUDE_CODE_DISABLE_BG_EXIT_HANDOFF` | don't hand bg subagents to a restarted session process (v2.1.196+) | off |
| `CLAUDE_SUBAGENT_BG_SHELL_MAX_MS` | **removed v2.1.260, no-op** (was 60-min cap on subagent bg shells) | — |
| `--max-turns` | session agentic-turn cap, print mode only, exits with error | none |
| `--max-budget-usd` | session spend cap incl. subagent spend; stops bg subagents | none |
| `--agent <name>` / `agent` setting | run the session itself as an agent | — |
| `--agents <json>` | session-scoped subagent defs; file path allowed with `-p` (v2.1.281+) | — |
| `--append-subagent-system-prompt[-file]` | append text to every subagent's system prompt (`-p`; v2.1.205 / v2.1.261) | — |
| `--forward-subagent-text` | stream-json forwarding of subagent messages (v2.1.211+) | off |

## Appendix B — notable CHANGELOG entries (feature-defining, not exhaustive)

From https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md:

- **2.1.172**: "Sub-agents can now spawn their own sub-agents (up to 5 levels deep)."
- **2.1.198**: "Subagents now run in the background by default"; subagents inherit the
  session's extended-thinking config; `/agents` wizard removed; "Subagents … treat messages
  from the agent that launched them as normal task direction."
- **2.1.208**: zero-tools spawn now errors naming unrecognized entries.
- **2.1.210**: subagent output scanning / Agent tool hardened vs indirect prompt injection.
- **2.1.211**: `--forward-subagent-text` added.
- **2.1.212**: `/fork` → background session; in-session fork is `/subtask`; per-session
  200-subagent cap added.
- **2.1.217**: concurrent cap (20, `CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS`); nesting default
  dropped to 1; `--max-budget-usd` stops background subagents.
- **2.1.219**: nesting default raised to 3.
- **2.1.224**: 200-per-session spawn cap removed.
- **2.1.232**: fork mode on by default in interactive terminal; finished subagents hide
  immediately w/ `/tasks` footer hint.
- **2.1.246**: `maxTurns` partial-output marking + resume hint.
- **2.1.248**: `experimental.cacheTtl` in agent frontmatter.
- **2.1.251**: `CLAUDE_CODE_SUBAGENT_MODEL` becomes a default (definition/per-spawn model
  take precedence).
- **2.1.257**: `CLAUDE_CODE_SUBAGENT_MODEL_FORCE`; subagents auto-continue after mid-stream
  cutoff.
- **2.1.260**: 1-hour cap on subagent background shells removed; subagent-resumed agent now
  wakes its resumer.
- **2.1.271**: `omitClaudeMd`; auto-mode `SubagentHandback` reporting.
- **2.1.277**: subagent results wrapped in a "subagent output" header.
- **2.1.280**: background subagents gain `LSP`; nested bg results saved in parent
  transcript.
- **2.1.285**: auto-mode subagent run ends at hand-back; foreground default for nested
  spawns in `-p`/SDK.
- **2.1.292**: `effort` param on the Agent tool.
- **2.1.296**: `autoCompactWindow` frontmatter; `CLAUDE_CODE_WORKFLOW_SUBAGENT_MODEL`.
- **2.1.299**: names since last compaction survive restart for SendMessage.
- **2.1.302**: fork subagents on by default everywhere; desktop/VS Code always background;
  nested Agent calls in `-p`/SDK run foreground by default.
- **2.1.304**: resumed/messaged subagents no longer forget recent work; plugins can't keep
  a headless run alive past `--max-turns` by spawning subagents.

## Appendix C — adjacent mechanisms distinguished from subagents

- **Background sessions / agent view** (`claude agents`, `--bg`, `/bg`): whole sessions
  under a supervisor daemon; persist after the terminal closes; the built-in `claude` agent
  is the default agent type for a dispatched session. Source:
  https://code.claude.com/docs/en/agent-view.
- **Agent teams** (experimental, `CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS=1`): named Agent
  spawns become teammates — persistent peers with a shared task list and JSON-file
  mailboxes at `~/.claude/teams/{team}/inboxes/{agent}.json`; teammates message each other
  and the lead directly; idle teammates notify the lead with their final answer; lead can't
  be transferred; teammates can't spawn teammates; in-process teammates' subagents run
  foreground only. Source: https://code.claude.com/docs/en/agent-teams.
- **Cross-session messaging** (`SendMessage`/`ListAgents` between *sessions*, v2.1.224+):
  plain-text messages over per-session inbox sockets; receiver treats them as from another
  Claude session, never as user approval. Source:
  https://code.claude.com/docs/en/cross-session-messaging.
- **Workflow tool**: script-orchestrated `agent()` subagents for dozens-to-hundreds of
  agents, outside conversation context. Source:
  https://code.claude.com/docs/en/agent-sdk/subagents ("Scale up with dynamic workflows").
