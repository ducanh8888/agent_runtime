# AgentRT — goal-driven runs instead of an operator step budget

Date: 2026-10-26 (Asia/Bangkok). Basis: `origin/main` at `6b88f3a`, and a
read-only census of the operator's state directory on the Windows host
(10,320 stored conversations). Supersedes the OmniRoute migration plan as the
active plan; that plan's open items are carried over in section 9.

**Status: planned; nothing in sections 4-6 is implemented.** Section 2 is
measured; section 3 lists defects found by reading `origin/main` and by the
census, each marked with how it was established. Owner decisions already
taken are in section 8.

## 1. Problem and objective

Operating AgentRT, sessions often end with `MaxIterationsReached`. The
operator (a person or an orchestrating agent) has to choose a
`max_iterations` for every dispatch, notice the cut, and `send` "continue" by
hand. Without a cap the runtime default (`DEFAULT_MAX_ITERATIONS = 100_000` in
`agentrt/runtime/config.py`) bounds nothing, and runaway sessions cost real
money (#37: 370+ iterations, #40: 445 finish attempts).

Objective: **a dispatch runs until its task is done, the operator never
chooses or watches a step number, and a bound still holds.**

1. **Goal-driven.** Whether a session continues or ends is decided against
   its task — "is the objective met, and is the work still moving toward
   it" — not against a step count.
2. **The agent does not adapt to a limit.** Nothing tells the agent how many
   steps remain or asks it to wrap up because of a budget. What it is told is
   about the goal: what is still outstanding.
3. **The budget counts work.** Steps the runtime spends on itself
   (condensation, re-loading forgotten skills) do not use it up.
4. **Every non-complete end lands.** A stop for lack of progress or at a
   safety ceiling ends with a tools-disabled wrap-up answer and a stated
   reason, never with an empty `error`.

Non-goals: a second runtime layer, a supervisor process outside the daemon,
acceptance checks beyond today's `require="commit"` (section 7).

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
  actions (a `(tool, arguments)` pair not seen earlier in the run) in the last
  50 steps was 0.61.
- At least 11 of the 31 cut runs show the existing "[Step budget]" notice in
  their last events (three steps before the cap); the model did not finish in
  any of them.
- 23 of 31 cuts were followed by a manual `send`; 5 of those later finished.
  Operators are already running a goal loop by hand.

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
  condensation, which forgets the skills, which the model re-invokes. Average:
  one condensation per 4.7 steps.
- `ConversationState.invoked_skills` records the names, but nothing uses it to
  keep the content.

### 2.4 A deterministic movement signal

Sample: 400 random conversations with at least 400 events, split into runs and
into windows of 50 steps; novelty per window as in 2.1, excluding
`invoke_skill`, `task_tracker`, `think` and `finish`.

| Runs ending in | Runs | Some window < 0.2 | Some window < 0.1 | Last-window novelty p50 |
|---|---|---|---|---|
| `finish` | 262 | 4% | 1% | 0.70 |
| `MaxIterationsReached` | 21 | 19% | 10% | 0.61 |
| `LLMServiceUnavailableError` | 26 | 46% | 35% | 0.33 |

A low-novelty window is rare in runs that finish. Novelty cannot tell a long
productive audit from endless exploration (both read new files), so it is not
used to decide that a goal is met; section 4.C uses it only as a veto that
keeps a session running while it is visibly moving.

### 2.5 STUCK is not a false-positive problem

193 conversations are `stuck`. By the last repeated action: 152 are
`file_editor view` with "Invalid view cursor" or "Cannot combine cursor with
view_range" (#14, fixed 2026-10-11), 13 `str_replace` without `new_str` (#33),
14 MCP argument validation errors, 3 a repeated `C-c`. None looked like a
false positive; none is later than 2026-10-15. Pairing actions with
observations by position — a real property of `StuckDetector` — produced no
mismatch in the last six pairs of any of them.

## 3. Defects found during the research

Established by reading code ("code") or by the census ("measured").

| # | Defect | Evidence | Addressed by |
|---|---|---|---|
| D1 | A condensation step increments the iteration counter: `Agent.step()` returns after emitting the `Condensation`, and the run loop counts it as a step. | code; 21% of cut-run steps (2.2) | 4.A.1 |
| D2 | Condensation forgets invoked skills; the model re-invokes them, re-filling the headroom that triggered the condensation. | measured (2.3) | 4.A.3 |
| D3 | Reaching the cap ends the run in `error` with no answer; the three-step notice did not make the model finish in any of the 11 runs that show it; operators resume by hand. | measured (2.1) | 4.B, 4.D |
| D4 | A `finish` on the final iteration skips the stop hook: the loop breaks on `iteration >= max_iteration_per_run` with `FINISHED` and never runs `run_stop` (both `run()` and `arun()`). `require="commit"` can be bypassed this way. | code; frequency not measured | 4.E.1 |
| D5 | A stop hook that times out or crashes allows the stop: the executor returns `success=False, blocked=False`, and `HookResult.should_continue` only refuses on `blocked` or `DENY`. | code | 4.E.2 |
| D6 | The #40 cap (`MAX_CONSECUTIVE_STOP_HOOK_DENIALS = 3`) counts denials per run and never resets, so it is not "consecutive"; reaching it ends in `error` / `StopHookUnmet`. | code | 4.F (reason exposed); counting left as is |
| D7 | In `arun()` the stop hook is a synchronous `subprocess.run` on the worker's event loop, inside `with self._state`. | code; `commit_hook` measured at 0.16 s per decision on an idle host | not changed; precondition for any longer check (section 7) |
| D8 | The action-error nudge only fires on `AgentErrorEvent`; a tool error arrives as an `ObservationEvent` with `is_error: true`, so the dominant loop goes to `STUCK` without the nudge. | code; 172 of 193 stuck sessions (2.5) | 4.A.4 |
| D9 | `docs/reference/daemon-behavior.md` "Iteration limits" says the agent is not warned before the cut (the notice exists since `59b41ae`) and gives 500 as the default without saying a runtime dispatch sends 100,000. | code vs. doc | 6, step 7 |
| D10 | Operator guidance (reply on #37) recommends caps of 60-120; with D1-D2 a skill-heavy audit spends up to ~70% of such a cap on overhead. | measured (2.2) | 4.B, 6 step 7 |
| D11 | The vendored goal judge renders **every** event of the conversation (`list(conversation._state.events)` → `_render_transcript`), not the condensed view. A long session's judge prompt is unbounded and will exceed the model's context; the loop then records `interrupted`. | code | 4.C.2 |
| D12 | Goal-loop follow-ups are sent as `source="user"` messages, which move `last_user_message_id`; `result` answers the newest user input, so after a follow-up the dispatch's own answer is scoped to the last round only. | code | 4.B.4 |
| D13 | The runtime does not know the goal subsystem exists: `wait` settles on a terminal status sampled twice, and between goal rounds the status is `finished` (or `stuck`) while the judge runs, so `wait` can settle a session that is about to continue. | code | 4.F.2 |

Not defects, recorded so they are not re-opened: #41 (`wait` false
`missing`) and #42 (streaming call without a total deadline) are fixed on
`main` (`b6db008`, `29827ca`) and await confirmation against a real
occurrence; no code path retries a tool automatically.

## 4. Design

The vendored server already has a goal subsystem: `POST
/api/conversations/{id}/goal` starts `EventService._run_goal_loop`, which runs
the agent, asks `GoalController` (SDK `conversation/goal/`) to judge the
transcript with `judge_goal`, and either ends (`complete`, or `capped` after
`max_iterations` rounds, default 10) or sends `FOLLOWUP_PROMPT` with what is
outstanding and runs again. It already continues past a `STUCK` run instead of
halting. The runtime does not use it today. This plan makes it the default
way a dispatch runs, fixes what it lacks for long sessions, and moves the
safety bound out of the agent's view.

Ownership: policy values (ceilings, thresholds, on/off) live in
`agentrt-runtime` config and are sent on the request; the mechanism lives in
the vendored SDK loop, condenser and goal package, and the vendored server's
goal loop. Vendored edits carry `AgentRT fork (...)` comments.

### A. The budget counts work (SDK)

1. A step that only produced a `Condensation` or a `CondensationRequest` does
   not increment `iterations_used` and does not count against any ceiling.
   `iterations_used` keeps its name and means work steps; condensations are
   counted in a new state field `condensation_steps`.
2. Guard for what A.1 removes: `K` consecutive condensation steps with no
   action in between (provisional `K = 3`) set `STUCK` with reason
   `context_loop`. This fills the stub
   `StuckDetector._is_stuck_context_window_error`.
3. The condenser keeps the latest observation of every skill in
   `ConversationState.invoked_skills` out of the forgotten range, bounded by a
   pinned-token cap (provisional 12k; past it the least recently invoked
   pinned skill becomes forgettable). Rejected: re-injecting skills into the
   system prompt (breaks the prompt cache on every invocation); raising
   `CONDENSER_MAX_TOKENS` (the 40k figure comes from router latency and error
   data, OmniRoute plan section 10).
4. The once-per-streak nudge also fires for a repeated action whose
   observations are identical and `is_error`, before the action-observation
   pattern sets `STUCK`.

### B. A dispatch is a goal (runtime + server)

1. `dispatch`, `dispatch_from` and `dispatch_many` start the conversation with
   the task as the goal objective: the server's create request gains an
   optional `goal: {objective_from_initial_message: true, max_rounds,
   max_work_steps, max_run_seconds, checkpoint_steps}`; when present the
   server starts `_run_goal_loop` instead of a plain run, using the initial
   message (with its context files) as the objective. Absent: today's
   behaviour exactly.
2. Runtime default: goal mode on for every dispatch (`AGENTRT_GOAL_MODE=1`);
   `dispatch(goal=False)` and `AGENTRT_GOAL_MODE=0` give a plain run.
   Ceilings come from `AGENTRT_GOAL_*` settings (env or state-dir `.env`).
3. A dispatch's explicit `max_iterations` still works and becomes the hard
   ceiling on work steps across all rounds; the MCP and CLI documentation stop
   recommending one.
4. Goal follow-ups and the resume prompt are emitted with
   `source="environment"` (as stop-hook feedback already is), so
   `last_user_message_id` stays on the dispatch message and `result` answers
   the task, not the last round (D12). A real `send` from the operator still
   supersedes the goal loop, as today.
5. Inside a run the agent sees no step budget: in goal mode
   `_maybe_warn_iteration_budget` is not called and the per-run iteration cap
   is the hard ceiling only. The three-step notice stays for plain runs.
6. Sessions outlive the client and the daemon: a goal whose last persisted
   status is `running` when the daemon restarts is continued by the server's
   restart recovery through the existing `resume_goal_loop`, from the round
   it had reached.

### C. Continue or stop is judged against the goal (SDK goal package + server)

1. **On every finish attempt** (the agent calls `finish`, and the stop hook,
   if any, allowed it): the judge decides. Complete → the session finishes.
   Not complete → `FOLLOWUP_PROMPT` with `missing`, next round. This also
   catches a premature "done".
2. **Judge input is bounded (D11):** the judge reads the conversation's
   condensed view (`state.view`: summary plus recent events), not every
   event, capped at a fixed token budget, and the objective.
3. **Silent checkpoints:** every `checkpoint_steps` work steps (provisional
   100) the loop takes a checkpoint without stopping the run and without
   sending anything to the agent. The checkpoint asks the judge for a verdict
   and computes the window's novelty (2.4) and file changes. The run is
   marked **stalled** at a checkpoint when the judge's `score` is not higher
   than the best score so far **and** the window is low (novelty < provisional
   0.15 and no file changed). Either signal alone keeps it running.
4. `stall_checkpoints` consecutive stalled checkpoints (provisional 2) end the
   goal with reason `no_progress` through D. A judge verdict of complete at a
   checkpoint does not end the run on its own; the agent's own finish is
   still what triggers C.1.
5. A judge call that fails or cannot be parsed counts as "not complete, no
   improvement" (the vendored parser already returns score 0), never as
   complete. The novelty veto in C.3 keeps a moving session running through
   judge outages.
6. Safety ceilings, outside the agent's view: `max_rounds` (finish attempts
   judged incomplete, provisional 10, the existing default), `max_work_steps`
   (provisional 1,000 across rounds) and `max_run_seconds` (provisional
   14,400). Wall-clock rather than cost: usage through the router is not
   reliable (Devin usage is misreported by the router, OmniRoute plan
   section 10), so a cost bound would not hold.
7. `STUCK` in goal mode keeps the vendored behaviour: the judge runs and a
   follow-up carries the agent out of the loop; a second `STUCK` in a row ends
   the goal with reason `stuck`.

### D. Every non-complete end lands

1. On `no_progress`, `stuck`, `max_rounds`, `max_work_steps` or
   `max_run_seconds` the server makes one tools-disabled completion over the
   condensed view (the shape of the existing `_finalize_completion`), asking
   for findings, changes made and what remains, and records the answer as an
   agent `MessageEvent` before the terminal `ConversationErrorEvent` with
   code `GoalNotMet` and the reason in its detail. One model call per such
   end, always, independent of `AGENTRT_FINALIZE_SUMMARY` (owner decision,
   section 8).
2. Status for these ends stays `error`, so existing clients read the same
   status; `result` now has text, so `wait` buckets it `partial`. A failed
   wrap-up call is recorded and does not change the code.
3. A goal judged complete ends `finished` as today.

### E. Completion gate holes

1. The stop hook runs before a `FINISHED` on the final allowed step is
   accepted (D4); in goal mode the judge then runs as in C.1.
2. For the runtime's own `require="commit"` hook, a hook timeout or crash
   counts as a denial (D5). Implemented in the vendored stop-hook path:
   `HookResult.error` on a `STOP` hook is treated as blocked, with the error
   as feedback; other hook events keep today's semantics.

### F. The operator can see why (runtime; server where noted)

1. `status`, `result`, `wait` items and `capacity.running_sessions` gain
   `termination_reason` (`complete`, `no_progress`, `stuck`, `max_rounds`,
   `max_work_steps`, `max_run_seconds`, `stop_hook_unmet`, `paused`,
   `error:<code>`), `goal` (`active`, `round`, last `score` and `missing`,
   projected from the persisted `key="goal"` state events), `iterations_used`
   (work steps) and `condensation_steps`. Additive; absent on an older
   daemon, and the client checks that a field arrived before reading it.
2. `wait` treats a session whose goal is `active` as not settled (D13), in
   addition to the two-sample rule.

## 5. Interfaces, fixed before implementation

- Server create request: optional `goal` object as in B.1; field names and
  defaults recorded in the OpenAPI schema; additive.
- SDK `LocalConversation`: optional `checkpoint_steps: int | None` and a
  checkpoint callback set by the server; `None` keeps today's loop.
- SDK `GoalController`: gains the stall rule (C.3-C.4) and the bounded judge
  input (C.2); `on_run_finished` keeps its signature; a new
  `on_checkpoint(view, window_stats) -> GoalCheckpoint`.
- SDK `ConversationState`: `condensation_steps: int = 0` (defaults load old
  state).
- New error code `GoalNotMet`; no existing code changes meaning.
- Runtime client/MCP: `dispatch(..., goal: bool | None = None)`; MCP
  docstrings for `dispatch`, `status`, `wait`, `result` describe goal mode,
  the reasons, and that `max_iterations` is not needed.

## 6. Work items, in order

Each item lands with a test that fails without it.

1. Record the census of section 2 as a result record, with the script that
   produced it, so the measurements in step 8 have a baseline in the
   repository.
2. A.1, A.2, A.4 (SDK loop, stuck detector). Tests: a condensation does not
   change `iterations_used`; `K` condensations without an action set `STUCK`
   (`context_loop`); an `is_error` repeat is nudged once before `STUCK`.
3. A.3 (condenser). Test from a replay of conversation `406777e2` (audit, cap
   60, 14 condensations, 25 skill-only steps): after condensation the pinned
   skill observations are still in the view, within the pinned cap.
4. E.1, E.2. Tests: a `finish` on the last allowed step runs the stop hook; a
   `commit_hook` that times out does not allow the stop; a non-stop hook error
   keeps today's behaviour.
5. C.2 and C.5 in the goal package. Tests: a 4,000-event conversation's judge
   prompt stays within the cap; an unparsable verdict never completes.
6. B, C.1, C.3, C.4, C.6, C.7 and D (server goal loop, SDK checkpoint,
   runtime dispatch and config). Tests: a finish judged incomplete gets a
   follow-up with `source="environment"` and `result` still answers the
   dispatch; checkpoints send nothing to the agent; two stalled checkpoints
   end with `GoalNotMet`/`no_progress` and a wrap-up message; a moving window
   with a flat score keeps running; each ceiling ends with a wrap-up; no
   `goal` field gives today's behaviour; restart mid-goal resumes from the
   persisted goal status.
7. F and documentation (D9, D10): "Iteration limits" in `daemon-behavior.md`,
   the orchestration guide, the MCP docstrings, `architecture.md`'s lifecycle
   section.
8. **Calibration and end to end, on Windows against a daemon started from
   source with its own `AGENTRT_STATE_DIR`.** Spends model calls (judge,
   wrap-up), bounded to the runs listed here:
   - Replay judge checkpoints on 30 stored runs (10 that finished, 10 cut at
     the cap, 10 stuck) and choose `checkpoint_steps`, the novelty threshold
     and `stall_checkpoints` so that no finished run would have been stopped;
     record chosen and rejected values.
   - Dispatch, without `max_iterations`: a skill-loading read-only audit, a
     small code fix with `require="commit"`, and a task that cannot be
     completed. Compare with 2.2-2.3 (condensation share, skill
     re-invocations) and record termination reason, rounds, judge calls and
     wall-clock for each.

## 7. Rejected or deferred from the draft fix plan (v4)

- **Acceptance runner** (command/test/artifact checks, fingerprints,
  shared-workspace ownership, process-tree cleanup): no issue asks for it;
  the goal judge plus `require="commit"` covers completion. Deferred; D7 is
  its precondition.
- **Progress windows that tell the agent to wrap up** (an earlier draft of
  this plan): rejected by the owner — the agent must not adapt to a limit.
  Novelty survives only as a veto inside the goal checkpoint.
- **A five-value `outcome` persisted per run:** overlaps `status`,
  `result.state`, `bucket` and `completed_cleanly`; F projects a reason from
  existing and new events instead.
- **ACP completion paths:** the runtime does not use ACP.
- **Rewriting stuck-detector pairing and equality:** no false STUCK measured
  (2.5); only A.2 and A.4 are kept.
- **Model-call timeouts and `wait` missing:** done on `main` (#41, #42).
- **Changing the #40 denial counting (D6):** left as is; in goal mode a
  stop-hook denial is still the commit gate, and the goal ceilings bound the
  rest.

## 8. Owner decisions (2026-10-26)

- Vendored code may be changed for this plan.
- The agent's behaviour must not be adapted to a step limit; goal-driven
  continuation is preferred.
- Every budget stop spends one model call on the wrap-up answer.

Still open for the owner: goal mode on by default for every dispatch (B.2),
and the judge's model calls (one per finish attempt and per checkpoint) — the
default plan assumes both.

## 9. Carried over from the OmniRoute migration plan

Still open there and not addressed here: #41 and #42 await confirmation
against a real occurrence; #35's client-side rejection is not transactional
with session creation; #13's tmux exit under ~230 sessions is not reproduced;
`watch` skips bursts of more than 50 events between polls; the admission lock
may still serialize creates; an `isolated_worktree` subdirectory session
leaves its worktree registered until `git worktree prune`; #23 (CLI `status`
13-78 s at ~15 running sessions) and #24 (one host for 200-300 sessions) are
open issues.
