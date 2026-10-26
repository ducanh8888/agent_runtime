# AgentRT — zero-tend sessions with native sub-agent semantics

Date: 2026-10-26 (Asia/Bangkok). Basis: `origin/main` at `6b88f3a`, a
read-only census of the operator's state directory on the Windows host
(10,320 stored conversations), and the documented behaviour of Claude Code's
native sub-agents (`../research/claude-code-subagents.md`). Supersedes the
OmniRoute migration plan as the active plan; that plan's open items are
carried over in section 9.

**Status: planned; nothing in sections 4-6 is implemented.** Section 2 is
measured; section 3 lists defects found by reading `origin/main`, each marked
with how it was established. Owner decisions are in section 8. A
pre-implementation audit added D14-D19 (section 10); a final revision
deferred the goal tier (section 7).

## 1. Objective

Two requirements, from the owner:

1. **Minimum operator effort.** The person or agent running AgentRT should
   not have to choose a step number, notice a cut, hand a session a
   "continue", or re-ask for the answer. Work that can carry on should
   carry on; work that ends should land with something usable.
2. **Native sub-agent semantics.** Each session behaves the way Claude
   Code's `Agent` tool behaves for its sub-agents: run until done, no turn
   limit by default, a cap returns a partial and resumable result, stop
   hooks enforce requirements, skills are loaded once, a restart hands the
   work back to the process.

The model for "done" is the native one — the model stops calling tools and
its last text is the answer. There is no second model judging completion.
An earlier draft built the run decision on the vendored goal/judge loop;
that tier is deferred (section 7) because it is more than native behaviour
and the owner chose native.

## 2. What was measured

All figures are from the state directory on the Windows host, read through
`base_state.json` (`max_iterations`, `iterations_used`, `execution_status`,
`tags` only) and `events/*.json`. A "step" is reconstructed as one LLM
response with actions (grouped by `llm_response_id`), one `Condensation`, or
one agent message; the reconstruction reproduces the caps exactly (runs cut at
60, 150 and 400 reconstruct to 60, 150 and 400 steps).

### 2.1 Who runs out, and while doing what

- 29 conversations (31 cap hits; forks share inherited runs) ended a run with
  `MaxIterationsReached`: 26 on 2026-10-20 in audit batches
  (`preflight037-audit*`, `tool1233-*`, `lite-*`), 4 on 2026-09-25.
- Every cap was set by the operator: 60, 120, 150, 250 or 400. None hit the
  runtime default.
- The runs were mostly **still working** when cut: the median share of novel
  actions (a `(tool, arguments)` pair not seen earlier in the run) in the
  last 50 steps was 0.61.
- At least 11 of the 31 cut runs show the existing "[Step budget]" notice in
  their last events (three steps before the cap); the model did not finish in
  any of them.
- 23 of 31 cuts were followed by a manual `send`; 5 of those later finished.
  Operators are already tending cut sessions by hand.

### 2.2 Where the budget went

| Step kind | Share of all steps in the 31 cut runs | Skill-heavy audit runs |
|---|---|---|
| `Condensation` (one iteration and one LLM call each) | 21% (898 / 4,264) | up to 36% |
| Steps whose only actions were `invoke_skill` | 13% (571 / 4,264) | up to 45% |

The four 2026-09-25 runs, before the 40k condenser cap (`acef636`,
2026-10-09), spent 2% on condensation.

### 2.3 Why skills are invoked again and again

- 1,592 `invoke_skill` calls across the 26 sessions: 86 first invocations,
  1,506 repeats. **Every repeat came after a `Condensation` since the
  previous invocation of the same skill; none without one.**
- The invoked skills are 2-4k tokens each (median observation 6.8k-14.1k
  characters); an audit lane loads about five, ~13k tokens.
- The condenser triggers at `CONDENSER_MAX_TOKENS = 40_000` and reduces to
  half of it (`LLMSummarizingCondenser._get_forgotten_events`). With ~13k of
  the 20k headroom taken by skills, the next few reads trigger another
  condensation, which forgets the skills, which the model re-invokes.
  Average: one condensation per 4.7 steps.
- `ConversationState.invoked_skills` records the names, but nothing uses it
  to keep the content.

### 2.4 Live observation during the research

Session `8facc3c2` (`inspect`, default cap): 152 iterations, 133
`file_editor` views and 120 `inspect` calls, no report written; the daemon
restarted at 09:05 (process `13196` started 09:05:03) and the run ended
`error`/`DaemonRestarted` with an empty `result` (`bucket: failed`). One
`send` ("write the report from what you have") finished it in 3 iterations.
A restart parks the run and waits for the operator — native sub-agents hand
background work to the restarted process. Separately observed:
`artifacts(path="cc_parity_inventory.md")` returned "File not found" for a
report that exists at the session's reports path after the restart — to
reproduce and file separately (section 7).

### 2.5 STUCK is not a false-positive problem

193 conversations are `stuck`. By the last repeated action: 152 are
`file_editor view` with "Invalid view cursor" or "Cannot combine cursor with
view_range" (#14, fixed 2026-10-11), 13 `str_replace` without `new_str` (#33),
14 MCP argument validation errors, 3 a repeated `C-c`. None looked like a
false positive; none is later than 2026-10-15. Pairing actions with
observations by position — a real property of `StuckDetector` — produced no
mismatch in the last six pairs of any of them.

## 3. Defects found during the research

Established by reading code ("code") or by the census ("measured"). "Deferred"
rows belong to the goal tier of the earlier draft (section 7) and are not
fixed here.

| # | Defect | Evidence | Addressed by |
|---|---|---|---|
| D1 | A condensation step increments the iteration counter: `Agent.step()` returns after emitting the `Condensation`, and the run loop counts it as a step. | code; 21% of cut-run steps (2.2) | 4.A.1 |
| D2 | Condensation forgets invoked skills; the model re-invokes them, re-filling the headroom that triggered the condensation. | measured (2.3) | 4.A.3, 4.E |
| D3 | Reaching the cap ends the run in `error` with no answer; the three-step notice did not make the model finish in any of the 11 runs that show it; operators resume by hand. | measured (2.1) | 4.B |
| D4 | A `finish` on the final iteration skips the stop hook: the loop breaks on `iteration >= max_iteration_per_run` with `FINISHED` and never runs `run_stop` (both `run()` and `arun()`). `require="commit"` can be bypassed this way. | code; frequency not measured | 4.C.1 |
| D5 | A stop hook that times out or crashes allows the stop: the executor returns `success=False, blocked=False`, and `HookResult.should_continue` only refuses on `blocked` or `DENY`. | code | 4.C.2 |
| D6 | The #40 cap (`MAX_CONSECUTIVE_STOP_HOOK_DENIALS = 3`) counts denials per run and never resets, so it is not "consecutive". | code | 4.C.3 |
| D7 | In `arun()` the stop hook is a synchronous `subprocess.run` on the worker's event loop, inside `with self._state`. | code; `commit_hook` measured at 0.16 s per decision on an idle host | not changed; precondition for any longer check (7) |
| D8 | The action-error nudge only fires on `AgentErrorEvent`; a tool error arrives as an `ObservationEvent` with `is_error: true`, so the dominant loop goes to `STUCK` without the nudge. | code; 172 of 193 stuck sessions (2.5) | 4.A.4 |
| D9 | `docs/reference/daemon-behavior.md` "Iteration limits" says the agent is not warned before the cut (the notice exists since `59b41ae`) and gives 500 as the default without saying a runtime dispatch sends 100,000. | code vs. doc | 4.F, 6 |
| D10 | Operator guidance (reply on #37) recommends caps of 60-120; with D1-D2 a skill-heavy audit spends up to ~70% of such a cap on overhead. | measured (2.2) | 4.A, 4.F |
| D11 | The vendored goal judge renders every event of the conversation; a long session's judge prompt is unbounded. | code | deferred (7) |
| D12 | Goal follow-ups are sent as `source="user"` messages, which move `last_user_message_id`; `result` is then scoped to the last round only. | code | deferred (7) |
| D13 | `wait` can settle between goal rounds: the status reads `finished`/`stuck` while the judge runs. | code | deferred (7) |
| D14 | The iteration cap and `iterations_used` are per run, reset at every run start; the goal loop treats a per-run cap hit as a stop signal (`interrupted`). | code | deferred (7); the per-run reset is harmless for the native tier because a cap ends the dispatch anyway (4.B) |
| D15 | The existing wrap-up (`_run_final_summary`) is skipped when `iterations_remaining(state) <= 0` and charges one iteration — it can never run in the case this plan needs it for. | code | 4.B.2 |
| D16 | Goal loops do not survive a daemon restart: recovery marks the run `DaemonRestarted` and nothing resumes the goal. | code | deferred (7); the run-level resume is 4.D |
| D17 | The stuck window and the FINISHED/STUCK → IDLE reset key on `source="user"`; a non-user follow-up would re-detect the same trailing loop before a step. | code | deferred (7) — needed by the goal tier's follow-ups; not by `send` |
| D18 | Any exception from the judge LLM ends the goal as `interrupted`. | code | deferred (7) |
| D19 | A `dispatch_from` fork inherits its parent's goal status; `resume_goal_loop` on the fork would resume the parent's objective. | code | deferred (7) |

Not defects, recorded so they are not re-opened: #41 (`wait` false
`missing`) and #42 (streaming call without a total deadline) are fixed on
`main` (`b6db008`, `29827ca`) and await confirmation against a real
occurrence; no code path retries a tool automatically.

## 4. Design

Every change follows one rule from the native model: the agent is never told
about a step budget, and an end the operator did not ask for still lands with
something usable. Ownership: policy values (caps, thresholds, on/off) live in
`agentrt-runtime` config; the mechanism lives in the vendored SDK loop,
condenser and hook path, and the vendored server's recovery. Vendored edits
carry `AgentRT fork (...)` comments.

### A. The budget counts work (SDK)

1. A step that only produced a `Condensation` or a `CondensationRequest` does
   not increment `iterations_used` and does not count against the cap.
   `iterations_used` keeps its name and means work steps; condensations are
   counted in a new state field `condensation_steps`.
2. Guard for what A.1 removes: `K` consecutive condensation steps with no
   action in between (provisional `K = 3`) set `STUCK` with reason
   `context_loop`. This fills the stub
   `StuckDetector._is_stuck_context_window_error`.
3. The condenser keeps the latest `invoke_skill` action/observation pair of
   every skill in `ConversationState.invoked_skills` out of
   `Condensation.forgotten_event_ids` (a set, so individual events can be
   excluded; the pair is kept whole because an observation without its
   action is malformed tool history). Pinned content is bounded by a cap
   (provisional 12k tokens, at most 60% of the post-condensation target;
   past it the least recently invoked pinned skill becomes forgettable), and
   the forgotten range is extended so the token target is still met.
   Rejected: raising `CONDENSER_MAX_TOKENS` (the 40k figure comes from
   router latency and error data, OmniRoute plan section 10).
4. The once-per-streak nudge also fires for a repeated action whose
   observations are identical and `is_error`, before the action-observation
   pattern sets `STUCK` (D8).

### B. A cap hit lands partial and resumable (native `maxTurns` behaviour)

Native: "Claude Code returns its output marked as partial, and Claude can
resume it to continue."

1. On `MaxIterationsReached` the run still ends `error` with that code
   (existing clients read the same status), but `result` returns the agent's
   last usable text with `state: partial`, a `resume_hint` naming the way
   to continue (`send`/`resume`), and `wait` buckets it `partial`, not
   `failed`.
2. When the last text is empty or is an unexecuted tool call (the case
   `completed_cleanly_reason` already names `final_message_is_unexecuted_
   tool_call`), the server makes one tools-disabled wrap-up completion over
   the condensed view — the shape of `_finalize_completion` — records the
   answer as an agent `MessageEvent`, then writes the terminal
   `ConversationErrorEvent`. This is the approved budget-stop model call,
   narrowed to the case where native would return nothing usable; it is not
   skipped at zero allowance and is not charged to `iterations_used` (D15).
3. The "[Step budget]" notice three steps before the cap is removed. Native
   tells the agent nothing about turns, and in all 11 observed runs the
   notice did not stop the cap anyway (2.1). The notice going away is the
   one place the agent's prompt changes; it is a removal, not an adaptation.
4. `max_iterations` keeps meaning per-run. Because a cap ends the dispatch
   (B.1), the per-run counter reset (D14) needs no fix here.

### C. Stop hooks behave like `SubagentStop`/`Stop`

1. The stop hook runs before a `FINISHED` on the final allowed step is
   accepted (D4).
2. For the runtime's own `require="commit"` hook, a hook timeout or crash
   counts as a denial (D5): `HookResult.error` on a `STOP` hook is treated
   as blocked, with the error as feedback; other hook events keep today's
   semantics.
3. The denial cap becomes 8 consecutive denials, `AGENTRT_STOP_HOOK_BLOCK_`
   `CAP` to tune, and the count resets when the agent executes a tool
   between denials (D6) — native resets "each time Claude calls a tool".
   Stop-hook input gains `stop_hook_active` so a hook can stop blocking a
   condition that will never resolve.

### D. A daemon restart hands the run back (native exit hand-off)

Native: background work is handed to the restarted session process; today
recovery marks the run `DaemonRestarted` and parks it for the operator
(observed live, 2.4).

1. Restart recovery still writes the `DaemonRestarted` event (events are
   history) and still names the cut tool call, then schedules the
   conversation to run again — a `run()` on the ERROR status — so the agent
   continues from the logged history and answers the same request.
2. Bounded: only conversations whose persisted status was `running` when
   the daemon died resume; `paused`, `stopped` and never-started sessions
   stay put, and an operator `stop`/`send` ordering still wins. Recovery is
   already rate-limited by the admission scheduler; resuming runs go
   through the same queue.
3. Goal loops keep parking as `interrupted` (D16) — deferred with the tier.

### E. Skills are loaded once (native `skills:` preload)

Native preloads the named skills' full content at startup; AgentRT relies
on `invoke_skill`, which costs a step per load and loses the content to
condensation (D2).

1. `dispatch(..., skills=[...])` names skills whose content is placed in the
   session's system-message skill section at start — part of what the
   condenser keeps (`keep_first`), so it survives condensation and stays
   prompt-cache-stable. Unknown names refuse the dispatch at the client.
2. Skills invoked later through `invoke_skill` are still pinned across
   condensation by A.3; preload just removes the common case from the
   invoke-and-forget cycle.
3. `skills` travels on the create request to the agent context; unset
   keeps today's progressive disclosure exactly.

### F. The operator can see why (runtime, additive fields)

1. `status`, `result` and `wait` items gain `termination_reason`
   (`finished`, `cap`, `stop_hook_unmet`, `stuck`, `paused`,
   `error:<code>`, `daemon_restarted` — projected from the last
   `ConversationErrorEvent` code), `resume_hint` on a `cap` end,
   `condensation_steps`, and `iterations_used` stays the work-step count.
   Absent on an older daemon; the client checks a field arrived before
   reading it.
2. Documentation: "Iteration limits" in `daemon-behavior.md` corrected
   (D9); orchestration guide drops the 60-120 cap advice (D10) and documents
   partial-and-resumable; MCP docstrings for `dispatch`, `status`, `wait`,
   `result` name the new fields and that `max_iterations` is not needed.

## 5. Interfaces, fixed before implementation

- Server create request: optional `skills: list[str]`; field names and
  defaults recorded in the OpenAPI schema; additive.
- SDK `LocalConversation`: on cap hit, emit the last usable text (or the
  wrap-up call's answer) before the `ConversationErrorEvent`; no new public
  method needed in this tier.
- SDK `HookResult`/`run_stop`: `STOP` hook `error` → denial (C.2); hook
  input gains `stop_hook_active`; denial counter resets on a tool call.
- SDK `ConversationState`: `condensation_steps: int = 0` (defaults load old
  state).
- Runtime: `dispatch(..., skills: list[str] | None = None)`; env
  `AGENTRT_STOP_HOOK_BLOCK_CAP`; `termination_reason`/`resume_hint`/
  `condensation_steps` on the payloads named in F.
- No new error codes; `MaxIterationsReached` keeps its code and `error`
  status.

## 6. Work items, in order

Each item lands with a test that fails without it.

1. Record the census of section 2 as a result record, with the script that
   produced it, so the measurements in step 8 have a baseline in the
   repository.
2. A (SDK loop, stuck detector, condenser). Tests: a condensation does not
   change `iterations_used`; `K` condensations without an action set `STUCK`
   (`context_loop`); an `is_error` repeat is nudged once before `STUCK`;
   a replay of conversation `406777e2` (audit, cap 60, 14 condensations, 25
   skill-only steps) keeps the pinned skill pairs in the view after
   condensation within the pinned cap.
3. B (cap lands partial). Tests: a capped run ends `error`/
   `MaxIterationsReached`, `result` returns the last text with
   `state: partial` and a `resume_hint`, `wait` buckets it `partial`; an
   empty-text cap end triggers exactly one wrap-up call; the three-step
   notice is not emitted; a `send` continues the same request.
4. C (stop hooks). Tests: a `finish` on the last allowed step runs the hook;
   a `commit_hook` timeout does not allow the stop; a non-stop hook error
   keeps today's behaviour; the counter resets on a tool call and forces the
   end at 8 consecutive denials; hooks receive `stop_hook_active`.
5. D (restart resumes). Test: kill a daemon mid-run, restart, the session
   runs again without a `send` and `DaemonRestarted` is in the log.
6. E (skills preload). Tests: `dispatch(skills=[name])` puts the content in
   the system message, it survives a condensation, an unknown name is
   refused client-side; no `skills` gives today's prompt byte-for-byte.
7. F + documentation (D9, D10), MCP docstrings.
8. **End to end, on Windows against a daemon started from source with its
   own `AGENTRT_STATE_DIR`:** dispatch, without `max_iterations`, a
   skill-loading read-only audit and a small code fix with
   `require="commit"`; compare condensation share and skill re-invocations
   with 2.2-2.3; restart the daemon mid-run and confirm the run resumes and
   `wait` collects it; dispatch one capped run and confirm the partial
   result and `resume_hint`. Bounded: the runs listed here only.

## 7. Deferred — not in this plan

Deferred at the owner's instruction ("có thể defer các plan tôi gửi"); each
item keeps its analysis so a later plan does not re-derive it.

- **The goal/judge tier.** Full draft: commit `88cab4f` of this file.
  Summary: `dispatch(goal=True)` routes through the vendored `_run_goal_
  loop`; a judge decides completion on every finish attempt and at silent
  checkpoints; stalled checkpoints (flat score and a low-novelty window)
  end the run `GoalNotMet`; follow-ups travel as environment messages;
  ceilings on rounds, work steps and wall-clock. Its known defects to fix
  if revived: D11-D19. Worth it only if "runs that keep reading forever
  without producing" prove common enough to pay a model call per
  checkpoint — the census (2.4) suggests they are not.
- **Acceptance runner** (command/test/artifact checks, fingerprints,
  shared-workspace ownership, process-tree cleanup): no issue asks for it;
  `require="commit"` covers completion. D7 is its precondition.
- **A five-value `outcome` persisted per run:** overlaps `status`,
  `result.state`, `bucket` and `completed_cleanly`; F projects a reason.
- **ACP completion paths:** the runtime does not use ACP.
- **Rewriting stuck-detector pairing and equality:** no false STUCK
  measured (2.5); A.2 and A.4 are kept.
- **Model-call timeouts and `wait` missing:** done on `main` (#41, #42).
- **Native-parity items scoped out:** mid-stream "continue" prompts on a
  text-only partial response; a stall timeout (native: 10 min); resume by
  agent name / `SendMessage`; concurrency cap (native: 20) and spawn-depth
  cap (3); `memory:` directories; `effort`; `permissionMode` inheritance;
  output scanning and the "sub-agent output" header on results; completion
  notifications (MCP has no push channel to the parent; `wait`/`watch`
  polling is the substitute); `autoCompactWindow`; worktree auto-cleanup
  and bash-level isolation checks.
- **The `artifacts` "File not found" after restart** (2.4): reproduce and
  file separately; not caused by anything in this plan.
- **Progress windows that tell the agent to wrap up:** rejected — the
  agent must not adapt to a limit.

## 8. Owner decisions (2026-10-26)

- Vendored code may be changed for this plan.
- The agent's behaviour must not be adapted to a step limit; goal-driven
  continuation was preferred, then narrowed: native sub-agent semantics is
  the target, so the goal tier is deferred (section 7) and this plan is
  the single committed plan.
- Every budget stop spends one model call on the wrap-up answer —
  **narrowed in section 4.B.2 to the case where the agent left no usable
  text** (native parity): the common capped run returns its last text and
  spends nothing. If the narrowing is not wanted, say so before step 3.
- Deferral of the earlier plan items is approved.

## 9. Carried over from the OmniRoute migration plan

Still open there and not addressed here: #41 and #42 await confirmation
against a real occurrence; #35's client-side rejection is not transactional
with session creation; #13's tmux exit under ~230 sessions is not reproduced;
`watch` skips bursts of more than 50 events between polls; the admission lock
may still serialize creates; an `isolated_worktree` subdirectory session
leaves its worktree registered until `git worktree prune`; #23 (CLI `status`
13-78 s at ~15 running sessions) and #24 (one host for 200-300 sessions) are
open issues.

## 10. Pre-implementation audit (2026-10-26)

Checked against `origin/main` before any code change. Claims confirmed:
D1 (`astep` returns after emitting the `Condensation`, the loop counts it),
D4 (the `iteration >= max` + `FINISHED` break precedes `run_stop`), D11
(`_snapshot_and_judge` passes every event), D12 (`last_user_message_id` is
set only for `source="user"`), D13 (no goal reference anywhere in
`agentrt-runtime`). Assumptions the earlier draft got wrong, now fixed or
deferred:

- "Resume after restart through the existing `resume_goal_loop`": nothing
  calls it on restart; recovery marks the run `DaemonRestarted` (D16). The
  native-tier fix (4.D) resumes the run, not the goal.
- "`max_iterations` becomes the ceiling across rounds": the cap and the
  counter are per run (D14); irrelevant in the native tier.
- Follow-ups as environment messages would leave a resumed run re-detected
  as `STUCK` (D17); deferred with the goal tier's follow-ups.
- The existing wrap-up is skipped at zero allowance (D15); 4.B.2 bypasses
  that path.
- Skill pinning must keep the action/observation pair and the token target
  (A.3): `forgotten_event_ids` is a set, but tool history must stay paired.
- Judge exceptions end the goal today (D18); deferred.

## 11. The parity target (Claude Code native sub-agents)

Researched from the official docs and the `anthropics/claude-code` CHANGELOG,
fetched 2026-10-26; every fact below cites the record in
[`../research/claude-code-subagents.md`](../research/claude-code-subagents.md),
which carries the URLs and verbatim quotes.

| Behaviour | Claude Code (documented) | This plan |
|---|---|---|
| Turn limit | None by default; `maxTurns` optional | same: default 100,000, `max_iterations` optional |
| On `maxTurns` | output marked partial, resumable | 4.B |
| "Done" | model stops calling tools; last text is the report | same; no judge |
| Stop hooks | block with `reason` as next instruction; 8 consecutive, reset per tool call; `stop_hook_active` input | 4.C |
| Time bounds | 10-min stall timeout; session dollar cap; no run cap | keep LLM attempt/streaming deadlines; stall timeout deferred |
| Mid-stream cut-off | text-but-no-tool-call partial → "continue" prompt | deferred |
| Failure report | partial output + "cut off" note; error names the API error | 4.B.2 wrap-up only when nothing usable |
| Return path | final report only, under a marked header, scanned | header/scan deferred; `result` already scopes to the request |
| Follow-up | `SendMessage` resumes same id, full history | `send`/`resume` today; name-resume deferred |
| Context | own window; system prompt + task + CLAUDE.md + git snapshot + preloaded `skills:`; auto-compaction | 4.E preload; 4.A keeps skills across condensation |
| Isolation/limits | `isolation: worktree`; tools allow/deny; permissionMode; 20 concurrent; depth 3; `memory:` | `isolated_worktree`/presets today; caps deferred |
| Lifetime | within one session; work handed to the restarted process; transcripts persist | sessions outlive the daemon today; 4.D resumes the run |

Section 2.4 is the live observation of the lifetime gap this plan closes.
