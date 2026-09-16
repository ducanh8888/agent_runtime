# Agent Runtime — DeepSeek hardening plan

Date: 2026-09-11 (Asia/Bangkok).
Basis: [self-audit and measured evidence](../research/deepseek-hardening-audit.md), the user's
nine runtime feedback items, and the DeepSeek optimization proposal as narrowed
by the user to direct DeepSeek with thinking enabled and effort `high`.
Planning baseline: `cce40bea488ee7299d312faa07172d0216dfb875`.
Implementation baseline: `203f493a4fb0736b384a1c370556c30d16c9c421`.

## Status

Active follow-on implementation plan; **H0–H6 are complete** and **H7 ran its production cutover on 2026-09-12**, with the sub-agent items and the H5 shared-writer limit finished afterwards. H7's staged scale verification (50–100 workers) remains and needs an agreed load and window.

**H8 and H9 are planned, not started**, added 2026-09-16 from consumer feedback
received after the H7 cutover (`../research/friction-log.md`'s stall/finalize
entry, and a second, more detailed consumer report). Both are diagnosis and
design only — no code has changed for either. H8 closes the fifteen items in
that report; H9 is a deliberate architecture comparison against Claude Code's
and Codex's native sub-agent primitives, aimed at closing the gaps H8 cannot by
itself (the two-process, MCP-mediated design is not going away, but several of
its rough edges are the *reason* it does not yet feel native).
The scope and thinking/high policy are user requirements. H2–H7 API
examples and internal field names below are proposed contracts, not shipped
capabilities; verified behavior is recorded in
[H0 result](../results/h0.md) and [H1 result](../results/h1.md).

The original [implementation plan](initial-runtime.md) remains the historical
P1–P4 record. The [documentation index](../README.md) owns document discovery.
H-prefix phase names avoid reusing those closed phases.
[Daemon behavior](../reference/daemon-behavior.md) and MCP tool descriptions
describe measured/shipped behavior, not this plan's intended behavior. Update
them only when a corresponding change is implemented and verified.

## 1. Fixed scope and non-goals

- DeepSeek direct: `https://api.deepseek.com`, API model `deepseek-flash`.
  The SDK's provider-routing prefix is internal, not part of the API model name.
- Always thinking enabled and effort `high` for every LLM service in the new
  deployment: worker, condenser, title, follow-up, finalize and vision.
- No Sol, multi-model escalation, task classifier, low/medium/max tier selection,
  cost-driven model changes or automatic effort reduction.
- Preserve the SDK's generic provider capabilities; enforce this policy in the
  AgentRT deployment/profile, not as a breaking global SDK restriction.
- No silent fallback to 9Router. Provider failure is a visible failure.
- Permission selection remains separate from LLM selection. Initially expose
  only the allowed `deepseek-high` worker profile; do not build an unused model
  registry or permission × model × effort product.
- Correctness, useful output, responsiveness and safe concurrency are the
  priorities. Cache and cost telemetry measure behavior; they do not lower
  reasoning or silently stop work. Operational timeouts and explicit iteration
  limits remain safety controls, not cost optimization.
- Preserve running sessions, paused sessions and user changes. No global profile
  rebuild, session cleanup, dependency replacement or daemon restart is implied
  by this plan. Historical permissions/spend figures are not fresh authority
  for disruptive operations or unlimited paid load tests.
- No new general-purpose agent loop, provider abstraction, event store or cron
  service. Worktrees and guarded tools are not OS security sandboxes.

## 2. Ownership and sequence

Use the original convention: `REUSE` existing behavior, `PORT` adapt existing
SDK/server/tool behavior, `NEW` only where the fork has no implementation.

| Phase | Deliverable | Basis | Depends on | Status |
|---|---|---|---|---|
| H0 | Direct/high policy, profile migration, usage foundation | REUSE + PORT | Baseline capture | Complete — [result](../results/h0.md) |
| H1 | Correct paging, guarded readonly tools, read evidence | PORT + NEW | Baseline; H0 for live LLM tests | Complete — [result](../results/h1.md) |
| H2 | Request/run-scoped results, errors, titles and progress | PORT + NEW | Baseline; H0 for live LLM tests | Complete — [result](../results/h2.md) |
| H3 | Reliable waits, finalization and partial summaries | REUSE + PORT + NEW | H0, H2 | Complete — [result](../results/h3.md) |
| H4 | Revision-pinned snapshots and writer isolation | REUSE + PORT + NEW | H1, H2 | Complete — [result](../results/h4.md) |
| H5 | Durable batch admission and bounded execution | REUSE + NEW | H2–H4, H0 usage hooks | Complete — [result](../results/h5.md) |
| H6 | Guarded images and complete accounting | REUSE + PORT + NEW | H0–H2; H4 for snapshot attachments | Complete — [result](../results/h6.md) |
| H7 | Regression, staged scale verification and deployment | REUSE + NEW tests/docs | All released phases | Cutover and sub-agent items done — [result](../results/h7.md); scale verification outstanding |
| H8 | Close the fifteen consumer-report defects (retry/reasoning, completion signaling, payload size, truncation, misclassification, `inspect` search, readonly output, snapshots, LLM profiles, transcript hygiene, tag charset) | PORT + NEW | H0–H3 (retry/wait/finalize), H1 (`inspect`) | In progress — items 1, 3 (wait_* safe ceiling), 11 done, 2026-09-17; rest open |
| H9 | Native sub-agent parity: close the experiential gap against Claude Code's and Codex's native sub-agents | NEW design | H8 (several H9 items are H8 prerequisites) | In progress — spawn-depth prerequisite (fork ancestry) and the OpenHands-ceremony gate done, 2026-09-17; role-shaped profiles (item 3) deferred; rest open |

H7 verification runs with each phase, not only at the end. First release scope
is H0–H3. H4 precedes shared-repository multi-writer scale tests; H5 precedes a
claim of support for 50–100 workers. H6 need not block fixes to file reads or
results. Independent implementation work may run in parallel only with explicit
file ownership and isolated writable workspaces.

Runtime/MCP/CLI own the thin orchestration surface. SDK/tools own model options,
observations and execution semantics. Agent server owns persistence, admission
and authoritative run completion. Extend each existing owner rather than
mirroring its state in the MCP process.

## 3. Phase specifications

### H0 — Direct provider and always-high contract

**Implementation**

1. Capture source revision, dirty-tree paths, installed package locations and
   versions, daemon identity and redacted effective profile settings. Record
   which executable actually runs each test. Add correct runtime version and
   capability reporting; MCP must not identify itself with the MCP library's
   version. Add the conventional non-mutating `agentrt --version` flag so this
   identity does not require a daemon round trip. Preserve existing fields and
   never reveal credentials.
2. Add neutral `AGENTRT_API_KEY` / `AGENTRT_BASE_URL` configuration names while
   retaining legacy `AGENTRT_9ROUTER_*`. Preserve layer precedence: process env,
   state `.env`, development `.env`. Resolve old/new aliases inside each layer;
   conflicting non-empty aliases at the same precedence are an explicit config
   error. Report provenance per setting, with secret values redacted.
3. Add an explicit preview/apply operation for a selected LLM profile. Show
   endpoint/model/policy changes, keep profile IDs and permission settings,
   preserve unrelated settings, write atomically with owner-only permissions.
   Do not use `ensure_profiles(force=True)`. Changing `.env` alone must not be
   advertised as changing a saved profile or an already-created session.
4. Reuse LLM/agent profile stores and the server resolver. Add a secret-free
   allowed LLM reference to dispatch composition; do not send API keys through
   MCP. Only new sessions receive the new resolved policy. Existing sessions
   are marked legacy where appropriate, never silently retargeted on resume.
5. Correct capability detection through the existing registry/helpers. Use the
   already-probed Chat Completions path initially. At the final transport
   boundary send `model=deepseek-flash`, `thinking.type=enabled` and
   `reasoning_effort=high`. Do not infer support from an alias alone or silently
   try a weaker policy after rejection. Responses mode is not required for this
   pass and must not be selected without its own contract tests.
6. Preserve required tool-history reasoning fields without exposing them in MCP
   transcripts. Enforce high for auxiliary LLMs, retries and restored/new-profile
   runs. Validate all creation paths in this deployment, including explicit
   agent/agent-settings payloads, not just named-profile dispatch. Disable or
   constrain worker profile-switch tools so they cannot bypass the allowed
   profile/policy. A supplied low/off override is a clear error.
7. Establish per-call identifiers, service/model/endpoint provenance, raw numeric
   usage fields and normalized usage. Compare both representations before any
   accounting conclusion. Instrument `configured`, `sent`, and provider
   `confirmed/unknown`; token counts are not proof of effective reasoning.

**Primary seams:** runtime `config.py`, `bootstrap.py`, `client.py`, CLI/MCP;
SDK profiles/resolver, `llm/utils/model_features.py`, LLM options/metrics;
server profile composition and info surface.

**Done:** synthetic HTTP contract tests cover worker, condenser, title,
follow-up, retry and restoration. A bounded direct high-thinking multi-tool
round trip passes through the installed stack. Secret redaction and profile
round-trip tests pass; default and auxiliary models cannot diverge unnoticed.
No production session is restarted as part of verification.

### H1 — Contiguous reads, readonly tooling and delivery evidence

**Implementation**

1. Add an `inspect` permission preset between `readonly` and `workspace`. It
   may execute only schema-validated read operations (file paging/search plus
   hardened Git and version/environment inspections), cannot invoke a general
   shell, cannot edit files, and must not expose provider credentials. Treat
   this as a readonly command runner, not an OS sandbox or a string-based shell
   allowlist. Prompt-only "do not write" instructions are not a security
   boundary.
2. Fix the numbering/truncation reproduction first. Never number a head/tail
   splice as a contiguous range. Prefer a page of whole source lines with stable
   continuation rather than a larger arbitrary character cap.
3. Extend the existing file view contract with version/hash, requested and
   returned ranges, continuation cursor, EOF and truncation metadata. Keep old
   parameters usable. A long single line needs an offset continuation and an
   explicit partial-line marker; the cursor must advance even in this case.
4. Bind cursors to file identity/version and range options. Return a typed
   `file_changed` result when continuity cannot be honored. Handle binary files,
   encoding errors, out-of-range requests and empty files explicitly.
5. Add structured search with file, line, bounded context and its own cursor.
   Enforce existing root/credential guards for every candidate and returned
   file, not just the starting directory. Guard fallbacks, symlinks and known
   credential hard links without rejecting all legitimate hard-linked files.
6. Provide narrow readonly git status/diff/log/show and executable-version
   operations. Use validated argv rather than arbitrary shell text. Disable
   execution via external diff, textconv, pager, fsmonitor or other helpers and
   optional index writes as appropriate; do not trust repository configuration.
   Git patches and search results must obey the same protected-file/output
   policy as file reads. Reuse existing git facilities only after confirming
   these properties.
7. Derive read evidence from successful tool observations and, where tracked,
   content included in serialized LLM requests. Record the stage explicitly:
   observed is not necessarily delivered, delivered is not understood. Merge
   ranges only within one file version. Persist through the existing event
   owner; do not add another full transcript store.
8. Warn on repeated identical version/range reads. A cache hit must preserve
   correct content and metadata; never replace a requested page with only a
   hash on the assumption that the model remembers it.
9. Document that an empty artifact listing is the expected positive result for
   a session that made no workspace writes. Keep it distinct from a missing
   workspace, unavailable evidence and artifact-enumeration failure.

**Primary seams:** file editor/output utility, runtime guarded tools,
search/git executors, observation schema and transcript projection.

**Done:** page concatenation reconstructs fixtures exactly; numbers match the
source across long files, long lines, Unicode, CRLF and a missing final newline.
Concurrent edits invalidate the cursor. Tests cover symlink escapes, protected
git patches and malicious git helper configuration. Coverage never claims
unread ranges or an EOF that was not reached.

### H2 — Input consumption, results, errors and progress

**Implementation**

1. Specify the lifecycle contract before changing extraction code. Distinguish
   session identity, submitted request identity, execution/run identity and
   ordered events. Reuse an existing suitable ordering primitive if available;
   otherwise add one at the existing event owner. Wall-clock timestamps alone
   are not the ordering guarantee.
2. Persist which input boundary an LLM step consumed. A message arriving during
   an in-flight completion must not cause that completion to be relabeled as
   answering the new message. Multiple inputs may be consumed together; one
   input may require several continuation runs. Do not assume one send = one run.
3. Expose a current-request result with `pending/final/partial/unavailable`
   metadata and explicit historical selection. Do not return the previous
   answer as the new request's result. Define how legacy sessions without
   boundaries are reported; do not invent provenance during migration.
4. Surface sanitized SDK error code/detail, iterations used/remaining,
   last completed tool/step, partial result and useful progress time. Never
   infer completion of a step from an agent's self-report. Separate stream
   activity from completed progress so a stuck stream is not reported as work.
5. Include error events and tool path/range/IDs in the condensed transcript,
   still excluding secrets/private reasoning. Error selection must use the
   same request/run scope as result selection.
6. Accept and persist an explicit title. Do not schedule auto-title when one is
   supplied, and prevent an earlier asynchronous title task from overwriting a
   later explicit title. Auto-generated titles follow H0's high policy.
7. Keep SDK execution status separate from admission and result status. Expose
   an explicit admission state (`queued`, `preparing`, `admitted`) so a newly
   dispatched or cap-blocked session is never ambiguously returned as `idle`.
   Preserve public legacy representations where necessary; change a pending
   result from `""` to JSON `null` with explicit compatibility/version handling.
   A final empty string remains valid and must be distinguishable by result
   state.
8. Accept title and tags atomically at dispatch. Preserve caller titles, derive
   useful default tags from stage/task/base/role when supplied, and make those
   distinguishing fields visible in compact listings. Auto-title remains a
   fallback, not the only way to tell a large fan-out apart.

**Primary seams:** SDK request/events/response helpers, server run/create paths,
runtime client/status/result/transcript, typed clients if their contract changes.

**Done:** tests cover send after finish/error, send during LLM and tool activity,
multiple pending inputs, resume, missing historical boundaries, process crash
and reload. Results/errors cannot cross input boundaries. Explicit titles
survive races. Golden legacy response/session fixtures still load and parse.

### H3 — Event-backed waiting and safe finalization

**Implementation**

1. Add wait-any/all over existing authoritative completion events, with a cursor
   for reconnect and REST batch fallback. Respect the current maximum of 99 IDs
   per batch request; validate/chunk locally rather than issuing a server error.
2. Return completed, failed, missing, attention-required and still-pending items
   separately, with timeout distinct from failure. Wait-all must not wait
   forever for a deleted session or silently call an approval pause success.
3. Do not complete a wait on the first provisional `finished` event. Honor the
   SDK's post-run/stop-hook completion semantics and bounded fallback. Avoid
   lost wakeups by reconciling snapshot and event cursor at subscription time.
4. Add finalization to the existing control surface. Persist the request, block
   new tool starts and handle in-flight work at a safe boundary. Cancellation
   acknowledgement is not rollback. If an external effect may still be running
   or its outcome is unknown, report that explicitly.
5. Run a final summary with thinking/high and tools disabled in both model
   request and executor admission. Persist it against the consumed input and
   original error, if any. Do not bypass approval/security hooks or silently
   resume normal tool execution when a stop hook rejects termination.
6. Preserve legacy `max_iterations` hard-limit behavior. Offer explicit
   reserved-summary policy within that same allowance and warn before its
   final slot. Do not increase the cap automatically or reset it through a
   hidden follow-up. If no summary call can run, expose a deterministic partial
   progress record rather than pretending an LLM summary exists.
7. Deduplicate repeated finalize requests. A crash after a tool but before the
   summary must not replay the tool merely to obtain a nicer final response.

**Primary seams:** existing event service, local/remote conversation completion
and cancellation, client batch/wait methods and MCP/CLI control.

**Done:** completion at subscription time, reconnect, timeout, missing IDs,
approval pauses and stop-hook rejection are tested. Finalize during a slow tool,
an exhausted run and a provider failure remains bounded and honest. No new tool
starts after the finalization barrier; unknown side effects stay visible.

**Decisions (2026-09-11).** The surface mimics the native sub-agent lifecycle
of the orchestrator that drives it, and no human is assumed to intervene: the
orchestrator resolves every outcome. Consequences for the items above:

- Wait outcomes are reported as `completed`, `partial`, `failed`, `stopped`,
  `still_running` and `missing`. There is no "attention required" bucket and no
  approval-pause branch to design around; a pause is a `stopped` outcome the
  orchestrator resumes or finalizes. Waiting blocks like a foreground launch;
  a timeout returns the unfinished ids as `still_running` with an explicit
  `timed_out` flag -- never as failures and never with partial output. Because
  a provisional `finished` exists mid-run, "settled" is only reported after the
  condition holds across two consecutive samples.
- `finalize` is a barrier over the existing control surface. Its default
  behavior is to stop new tool starts and return the answer the run already
  has; it does not spend a model call. An explicit config/flag enables the
  thinking/high, tools-disabled summary described above, counted within the
  run's existing allowance; with no allowance left the deterministic partial
  record is returned instead.
- Reserved summary is opt-in and off by default, so the legacy `max_iterations`
  hard limit keeps its meaning for every session that does not ask for it.
- Status reads are batched within the server's per-request id cap rather than
  issuing an over-cap request.

### H4 — Revision-pinned workspaces

**Implementation**

1. Add workspace mode independent of permission: existing `shared`, immutable
   review `snapshot`, and writable `isolated_worktree`. Keep the existing
   dispatch default compatible; do not silently snapshot all current workflows.
2. Pin the resolved local commit during dispatch preparation/admission. Do not
   fetch origin or substitute origin/main. Preserve the relative workspace
   subdirectory. Record requested ref, resolved SHA, capture metadata and active
   root; verify workspace construction/persistence has no duplicate side effects.
3. A clean-commit snapshot is the first supported case. Handle no-commit repos,
   submodules and LFS explicitly (unsupported with a clear reason is preferable
   to an incomplete snapshot presented as complete).
4. Dirty overlay is explicit opt-in. Capture chosen modified/deleted/untracked
   files with hashes and exclusion policy. Detect changes during copying and
   retry within a bound or fail. Record a capture interval and consistency level:
   a frozen copied tree is not a proven atomic snapshot of an externally edited
   source. Atomic capture requires stronger isolation/quiescence, not just hashes.
5. Expose preparation state; an item must not become runnable until its snapshot
   and manifest are durable. Queue delay cannot silently select a newer source.
   Reject unsafe symlink escapes and protected-file capture. Do not let a review
   bypass its pinned root through unrestricted external file references.
6. Give writers isolated worktrees; never auto-merge their changes. For shared
   mode, coordinate AgentRT-managed writers, but state clearly that editors and
   unrelated processes do not participate in those locks. Worktree isolation is
   not sandboxing: shell access and shared git metadata remain relevant limits.
7. Tie workspace lifetime to session references. Cleanup is explicit and must
   preserve active/paused/evidence sessions and uncollected modifications.

**Primary seams:** existing server worktree preparation, workspace persistence,
guard roots, artifact/read-evidence provenance, admission preparation metadata.

**Done:** local HEAD behind origin, subdirectory roots, dirty edits before/after
capture, preparation cancellation and two isolated writers are exercised.
Reported hashes match delivered files. No automatic source-tree mutation, merge,
or cleanup of another session occurs.

### H5 — Durable dispatch-many and provider-aware admission

**Implementation**

1. Add per-item batch outcomes and bounded batch size. Preserve single dispatch.
   Validate items before side effects and define partial acceptance explicitly;
   a network retry must return the original accepted items, not recreate them.
   A full execution pool applies backpressure by durably queueing accepted work;
   it does not reject overflow and require caller-managed wave batching.
2. Keep queue/preparation/admission metadata in the existing server persistence
   owner, not the MCP client's memory. Define durable transitions and recovery
   around enqueue, workspace preparation and run launch.
3. Track separate limits for admitted/running sessions, in-flight LLM requests,
   provider account/model, batch and shared-workspace writers. All runtime LLM
   services consume the appropriate provider request slots. Do not count a
   shell-running session as a permanently occupied LLM connection.
4. Admit atomically across clients. Avoid deadlocks and slot leaks: do not hold
   provider capacity while waiting for workspace preparation/locks or sleeping
   through retry backoff. Retain/release writer ownership according to the
   documented mutation boundary, not simply LLM availability.
5. Expose used/available slots, limiting dimension, queue/preparation status,
   and an advisory queue position. Zero disables that specific cap; report
   unbounded availability explicitly, not as a misleading numeric remainder.
   A provider quota and an explicit limit on another dimension still apply.
   A caller must be able to submit N independent items once and later collect N
   terminal outcomes without re-dispatching cap rejects.
6. Add submission idempotency with durable key-to-request mapping. The same key
   with a different payload is a conflict. Define retry lifetime and scope.
   This guarantees deduplicated submission, not exactly-once arbitrary tools.
7. Support queued cancellation, fair scheduling and provider retry/backoff.
   Recover queued work after restart, but reconcile interrupted active work.
   Never automatically replay a tool with an unknown side-effect outcome.
8. Retain legacy client-cap behavior until daemon capability negotiation makes
   queue-backed admission available. Then report the daemon's authoritative
   limits; do not leave both caps silently governing different session subsets.

**Primary seams:** server conversation/admission service and persistence,
LLM request lifecycle, runtime client `dispatch_many` and capacity projection.

**Done:** concurrent clients, duplicate keys, changed-payload keys, partial batch
failure, 429, disconnect, queued cancel and crash points are tested. Slots are
neither leaked nor exceeded. Small batches are not starved by a large one.
Crash tests assert deduplication/reconciliation, not impossible exactly-once
claims for uncooperative external programs.

### H6 — Images, cache visibility and complete accounting

**Implementation**

1. Add image/attachment references to the initial message using existing typed
   content blocks and file-editor image support. Validate actual model/image
   transport capability, MIME, decode limits and allowed roots. Do not fetch
   arbitrary URLs or use attachments to expose credentials/outside files.
2. Freeze referenced attachment versions for snapshot tasks. Any internal asset
   store must have session-scoped read grants and cleanup references; storing an
   asset beside runtime secrets must not make that directory readable to workers.
3. Keep vision on direct DeepSeek thinking/high. A missing capability is a clear
   error, not silent image stripping or fallback to another model.
4. Extend H0 usage/provenance per service, call, request and run: prompt/cache,
   completion/reasoning, queue wait, first-token and full-call latency, completed
   progress, iterations, tools, repeated ranges and result state. Distinguish
   first reasoning token from first user-visible content when measuring latency.
   Expose the per-session projection through MCP/CLI as well as REST so batch
   orchestrators can inspect spend without opening persisted state directly.
5. Normalize provider usage without double-counting reasoning included in output
   or cache included in prompt. Preserve unknown versus zero and handle usage
   missing from an interrupted stream. Retrying/replayed events cannot be counted
   twice; record each actual billed/possibly billed attempt separately.
6. Keep stable reusable prompt prefixes where appropriate, without forcing
   perpetual session resume or compressing evidence to cut cost. SDK cache flags
   do not prove provider cache behavior; verify the actual reported usage.
7. Replace `spend.py`'s single-model hardcoded estimate with versioned pricing and
   provenance. Price DeepSeek calls by their applicable timestamp/rate; keep
   estimates distinct from provider-confirmed charges and unknown rates explicit.
8. Specify aggregate retention before changing deletion: retain only minimal
   usage/accounting data, not conversation content or private reasoning, under
   the existing persistence owner. Expose the policy and an explicit purge
   operation; deleting a session must not silently lower a lifetime total.

**Primary seams:** typed message/attachment inputs, image guards/capabilities,
SDK metrics and server projection/aggregation, `tools/spend.py` and MCP views.

**Done:** synthetic screenshot answers match a known fixture, denied/oversized
assets fail safely, and image bytes survive the actual request path. Numeric
usage reconciliation, partial streams, retry deduplication, pricing intervals,
session deletion and explicit purge have deterministic tests. Cost/telemetry
cannot change the high policy or silently stop a run.

### H7 — Verification, documentation and production cutover

**Implementation and release gates**

1. Before modifying each package, read its nearest `AGENTS.md`; keep domain tests
   with the corresponding existing SDK/server/tools/runtime tests. Run affected
   lint/type checks and compatibility fixtures. Do not invent a full-suite pass
   from the historical Linux baseline or rewrite old results to fit new behavior.
2. Port the demonstrated failures into deterministic fixtures: file numbering,
   historical result, input consumption, title race, hidden error, stop hooks,
   high payload, snapshot preparation and admission/recovery. Add live model
   checks only where mocked HTTP cannot establish the relevant property.
3. Run `cli_loop.py`, `adversarial.py`, `probe_artifacts.py`, `probe_parallel.py`
   and `mcp_e2e.py` only after verifying their paths/cleanup target an isolated
   test state and workspace. Build a per-test baseline for changed areas; keep
   Linux/Windows differences explicit. No production cleanup as a test shortcut.
4. Use AgentRT direct/high for bounded independent implementation/review tasks;
   give exact files, acceptance criteria and a revision. Verify output with
   tests/diffs/events. A worker's `finished` or claimed review is not evidence.
5. Increase concurrency in controlled stages after correctness gates pass.
   Record host CPU/RAM/FD/process pressure, request concurrency, queue fairness,
   p50/p95 latency, errors and completion quality. Agree the maximum load/window
   before a disruptive 50–100 worker trial; no arbitrary quota becomes a default
   merely because it appeared in a proposal.
6. Use a separate installation, state directory, port, token and explicitly
   selected client for development. Two daemons must never write the same state.
   Side-by-side testing is not transparent production session routing; no
   multi-daemon session router is part of this pass.
7. Production cutover is a separate controlled operation: inventory running,
   queued, paused and evidence sessions; drain active work without killing it;
   arrange the cutover window; stop the old state owner, make a consistent
   recoverable backup, validate loading/migration, then reconnect the client.
   Keep all retained session IDs/history and report the policy of resumed legacy
   sessions. If draining is impossible, remain in the test instance rather than
   forcing a restart or pretending new state contains old sessions.
8. Rollback must respect state schema changes: do not run old code on mutated
   new-format state. Keep the pre-cutover backup and reconcile any work created
   after cutover instead of deleting it. Do not hot-replace a live installation
   whose process may import modules lazily.
9. Carry forward the sub-agent comparison recorded on 2026-09-11, now
   implemented and recorded in [the H7 result](../results/h7.md): (a) context
   inheritance ships as `dispatch_from`, which forks *another AgentRT session*
   -- the orchestrator's own conversation is not readable by the daemon, so that
   is the limit, and a fork inherits the source's agent, workspace and
   permission; (b) the stall signal ships as `progress_age_seconds` on the
   answer, reported and never acted on, because a long reasoning turn persists
   nothing and is indistinguishable from a stall -- no automatic abort is
   implemented, deliberately; (c) progress visibility ships in the wait surface,
   where `still_running` items carry the last sampled status, admission, result
   state and iteration counts. A pushed completion remains impossible over the
   stdio transport and is not attempted.

### H8 — Fifteen consumer-report defects

Basis: a consumer feedback report received 2026-09-16, fifteen numbered items
plus a kept-behavior list, and `../research/friction-log.md`'s "the first LLM
call can hang forever" entry from the day before, which items 1 and 12 below
turn out to explain. Every item was checked against the current tree before
being scoped here -- several confirmed exactly as reported, one confirmed
*false* as reported (with the real defect relocated), one confirmed only
partially (the CLI path does not reproduce it; a related, more serious path is
untested). Items are grouped by the consumer's own severity labels.

**Verification performed, so H9/implementation does not have to re-derive it:**

- Item 1 (reasoning_content dropped on resume) and item 12 (0-iteration
  `LLMServiceUnavailableError` after a 900s provider queue timeout) are the
  same failure family as yesterday's friction-log entry, and the missing piece
  is now found: `LLM._retry_listener_fn` (`agentrt/sdk/llm/llm.py`) does not
  log per-attempt retries *by design* -- its own comment says logging every
  retry "would create noisy duplicate error logs" -- and only
  `Telemetry.on_error` logs, after retries are exhausted. `APIConnectionError`
  and `ServiceUnavailableError` are already in `LLM_RETRY_EXCEPTIONS`, so a
  900s-queue rejection is plausibly already being retried, silently, for up to
  `num_retries * (attempt latency + backoff)` -- which is consistent with both
  yesterday's 7m34s hang (a live, cancellable task the whole time, per
  `interrupt()`'s log line) and not needing a new retry *path*, only visibility
  into the one that exists and a bound on how long "silently retrying" is
  allowed to look identical to "hung."
- **Correction, 2026-09-17: this bullet's own conclusion was wrong and stood
  uncorrected for a day.** It said item 5 "does not reproduce against
  `_wait_bucket`... read literally, an empty/blank result string falls
  through to `return "failed"`." A regression test built on that exact claim
  failed on first run: `_wait_bucket` returned `"partial"` for an errored,
  empty-result session, because the `state == "partial"` check came *before*
  the text was ever inspected -- the control flow was misread, not the text
  handling. Fixed in `49a55ad`; see item 5 below for the corrected trace.
- Item 11 (tag key charset) confirmed exactly: `TAG_KEY_PATTERN =
  re.compile(r"^[a-z0-9]+$")` (`agentrt/sdk/conversation/types.py`) rejects
  `superseded-by`. This codebase already has the fix's shape elsewhere --
  `PLUGIN_NAME_PATTERN` and `CANVAS_EXTENSION_NAME_PATTERN`
  (`agent_server/plugins_router.py`, `canvas_extensions_router.py`) are both
  `^[a-z0-9]+(?:-[a-z0-9]+)*$` -- so widening the tag pattern to match is a
  precedented change, not a new one.
- The CLI positional-flag complaint reproduces exactly: `agentrt list --text`
  fails with argparse's generic `unrecognized arguments: --text`, no hint that
  global flags must precede the subcommand.
- The stdout/banner complaint does **not** reproduce over the path tested:
  `agentrt list` (cold daemon, first call, `2>/dev/null`) prints clean JSON
  immediately, because the CLI and the daemon are different, undetached-stdio
  processes by design (`daemon.py`'s own docstring: "fully detached from the
  client's console, stdio, and process group") -- the daemon's own startup
  banner cannot structurally reach the CLI's stdout. What was **not** tested is
  the path where this would be catastrophic rather than cosmetic: `agentrt-mcp`
  shares a process with FastMCP, and stdout *is* the JSON-RPC channel there. If
  the vendored SDK's banner print (suppressed by `AGENTRT_SUPPRESS_BANNER=1`,
  per `CLAUDE.md`) or any of its own startup logging writes to stdout rather
  than stderr inside that process, it corrupts the protocol stream itself, not
  just a terminal's readability. Unverified; check before assuming the
  consumer's report was simply wrong about the mechanism.

**Implementation, grouped by the consumer's severity labels**

*Nghiêm trọng (critical):*

1. **Reasoning-content resend (item 1). Root cause fixed, 2026-09-17
   (`39b89ea`); the retry-after-repair half below is not done.** Trace how
   `LocalConversation` rebuilds message history for a resumed/continued turn
   and confirm whether `reasoning_content` on prior assistant tool-call
   turns is included when the provider is in thinking mode. If it is
   dropped, carry it through serialization and resume, matching what the
   provider's own error names as the requirement. Treat `LLMBadRequestError`
   naming this specific shape as retryable-after-repair (fix the resend,
   then retry once) rather than terminal. Do not build a generic
   non-thinking fallback profile as part of this item -- that is H8 item 9,
   a separate, opt-in profile, not an implicit silent-fallback (which H0
   already excludes as a non-goal).

   **What shipped**: the truthy-check bug below (empty-but-present
   `reasoning_content` silently omitted) is fixed, so the specific mechanism
   this investigation found is closed. **What did not ship**: a session
   that still hits `LLMBadRequestError` naming this shape for some other
   reason is not treated as retryable-after-repair -- that requires wiring
   this specific error into the retry classifier as a distinct, narrow case
   (repair the *next* resend's history, then retry once), which was not
   attempted. Worth a second live example before or instead of building it
   blind, since the mechanism found here may already account for most or
   all real occurrences.

   **Reproduced live, first-party, 2026-09-17**: session `2e1d9496` (one of
   this plan's own audit dispatches, `inspect` preset) hit exactly
   `LLMBadRequestError: litellm.BadRequestError: OpenAIException - The
   reasoning_content in the thinking mode must be passed back to the API`
   -- **correction to an earlier read of this evidence**: the first
   `transcript()` call without a cursor pages backwards from the *most
   recent* events, not from the start, and was misread as "the third
   tool-call turn." The raw event files (`~/.agentrt/conversations/<id>/
   events/*.json`) show the true shape: 45 successful iterations first,
   error at event ~199 of 200. Severity is still real -- a session doing
   ordinary work over 45 turns is not a rare edge case either -- but "hits on
   turn three" was wrong and is retracted.

   **Candidate mechanism, found in the raw events, not yet proven as the sole
   cause**: `Message.to_chat_dict` (`llm/message.py`) gates the resend with
   `if send_reasoning_content and self.reasoning_content:` -- a *truthy*
   check. Several `ActionEvent`s in this session's history carry
   `reasoning_content = ""` (empty string, not `None`) rather than the
   substantial text most turns have; `_combine_action_events`
   (`event/base.py`) takes `events[0].reasoning_content` when a response
   produced parallel tool calls, so a turn whose first event happened to have
   empty reasoning gets the field *omitted from the resend entirely* instead
   of sent as an empty string -- a real, fixable defect regardless of whether
   it is the exact trigger for this specific failure (the turn immediately
   before the error in this session had non-empty reasoning_content, which
   the truthy-check theory alone does not explain, so treat this as strong
   circumstantial evidence, not a closed case). Fixed alongside this entry:
   the condition is now `if send_reasoning_content and self.reasoning_content
   is not None:`, so an empty-but-present reasoning turn is resent as `""`
   rather than dropped.
   Transcript available via `transcript(session="2e1d9496")` as a fixture
   seed.
2. **No completion signal (item 2). CLI half done, 2026-09-17 (`74bba35`).**
   The stdio MCP transport cannot push;
   H7.9 already recorded that limit rather than working around it with
   something fragile. Ship the CLI-side piece that is genuinely missing: a
   blocking `agentrt wait <id> [id...]` that exits 0/timeout-code on settlement,
   so an orchestrator that does not want to hold an MCP call open can background
   the CLI process and get a real process-exit signal instead of a poll loop.
   Do not attempt a webhook/callback registry in this pass -- no deployment
   need for it has been stated, and it is a different trust boundary (an
   outbound call from the daemon to somewhere).
12. **0-iteration provider timeouts are not distinguished (item 12, folded into
    item 4's design).** Add a start deadline: bounded time from dispatch/resume
    admission to the first persisted event (`ActionEvent` or
    `ObservationEvent`). This is deliberately narrower than a stall watchdog --
    H7 already measured and rejected a general one, because a session that
    produces nothing for minutes while composing one long answer is not
    stalled and killing it discards real work. `iterations_used == 0` with zero
    events has nothing to discard. On the deadline, surface a distinct
    execution status (not the existing undifferentiated `error`) naming the
    provider exception class and attempt count, per item 4 below.

*Cao (high):*

3. **Unpaged payloads (item 3).** `wait_all`/`wait_any` return status and
   metadata (length, a content hash) by default instead of full result text;
   add offset/limit paging to `result`, matching the shape `read_evidence`
   already established for transcripts. This is consistent with H1's paging
   work, extended to the two call sites that currently skip it.
4. **Silent truncation (item 4).** Persist and report `finish_reason` (or an
   equivalent explicit `truncated: bool`) on the final message, and keep the
   untruncated text retrievable through the new paged `result` from item 3
   rather than only through raw events. A `final_summary` (H3's finalize
   summary) needs the same field.

*Trung bình (medium):*

5. **`partial` bucket misclassification (item 5). Fixed, 2026-09-17
   (`49a55ad`) -- correcting an earlier conclusion in this same document,
   not just the code.** This document previously traced the bug to
   `agent_server`'s `state` field and said `_wait_bucket` needed no change
   "once its input is honest." That was wrong: `_wait_bucket` checked
   `state == "partial"` and returned early, *before* ever looking at the
   result text -- caught by writing the regression test the earlier
   conclusion implied should already pass, and watching it fail. `state`
   does come from `derive_result_state` (`run_scope.py`) naming every
   non-`finished` terminal "partial" by design, which is a legitimate,
   intentional choice at that layer -- the bug was trusting it alone one
   layer up, in the bucket function, not the state's own meaning. Fixed by
   removing the early branch; the result text now decides every
   non-terminal-status case.
6. **`inspect` search usability (item 6). Fixed, 2026-09-17 (`31e8f14`) --
   deeper than expected.** Return `path:line` per match (bounded count,
   matching the existing truncation convention), accept a single file as
   `scope` instead of only a directory (currently `"Not a directory"`), and
   fix the inconsistent match counts between nested scopes -- a parent
   directory and a file inside it disagreeing on whether a pattern exists
   there is a correctness bug in the search implementation, not a
   documentation gap.

   **What was actually found**: `SearchMatch` (path, line, text) was already
   structured correctly in `matches` -- the count-only symptom was one layer
   up. `to_llm_content` (the SDK's default) only serializes `content` into
   what the model reads in a normal conversation turn; `matches` never
   reached it. Fixed by rendering path:line into `content` itself, halving
   `SEARCH_MATCH_BUDGET` since the same data is now paid for twice in one
   observation's size. File-scope rejection fixed by scanning a single file
   when `path` resolves to one. The "inconsistent counts between nested
   scopes" claim was *not* independently investigated -- it is very plausibly
   the same file-scope bug (a worker passing a file as scope hit the old
   error, not a genuine 0-match result), and extending the fix to it by
   resemblance alone would not meet this project's own verification
   standard, so it stays unconfirmed rather than claimed fixed.
7. **No readonly output channel (item 7). Done, 2026-09-17 (`9f5e515`).** A
   write-only directory outside the dispatched workspace, listed through
   `artifacts` like the workspace itself, for a `readonly`/`inspect` session's
   report -- without granting write access to anything the session can read,
   which would reopen the exact confinement-by-path problem
   `orchestration.md`'s "Limits worth knowing" section already describes for
   hard links. Needs the same device/inode comparison the workspace guard
   already uses, applied to the new directory's boundary.

   **What was built**: no new server-side state. The directory is
   `<persistence_dir>/reports`, the same "subdirectory of `persistence_dir`"
   convention `env_observation_persistence_dir` already uses, computed
   identically on the client side from `config.state_dir() /
   "conversations" / uuid.hex` (verified against the real on-disk layout, and
   against `event_service.py`'s `persistence_dir=str(self.conversations_dir)`
   plus `get_persistence_dir`'s `/ conversation_id.hex`, so the two
   computations are provably the same path, not just usually the same path).
   `GuardedFileEditorExecutor` gets a second root; `permissions.check_path`
   still runs unmodified, closing the hard-link/UNC/dots-and-spaces cases the
   same way it already does for the workspace. `client.artifacts` lists this
   directory as a `reports` field alongside the existing `files` listing,
   unfiltered by time (session-dedicated, not a repository), and checks it
   before the workspace when reading a single `path`.

   **What the first version got wrong, found by writing the tests**: trying
   the reports root before the workspace, for *any* relative path, meant
   every relative write from a readonly/inspect session -- including an
   ordinary illegitimate one like `src/app.py` -- silently landed inside
   `reports/` instead of being refused. A relative path always resolves
   inside whatever root it is joined to, so "not under the reports root"
   almost never fires for one. Fixed by checking the workspace first (as
   before) and falling back to the reports root only for an *absolute* path
   that fails there -- matching what the tool's own description hands the
   model: `reports_root` as an absolute string, not a hint to write a
   relative name that happens to land somewhere new. Verified end-to-end
   against the real daemon: a dispatched `readonly` session wrote its report
   to the exact absolute path from its own tool description, `artifacts`
   listed it under `reports` without the workspace listing changing, and
   `artifacts(session, path="findings.md")` read its content back.
8. **No workspace snapshot for readonly sessions (item 8). Done, 2026-09-17
   (`16693c0`).** A `snapshot=<git ref>` workspace mode that creates a
   detached worktree, records the resolved commit on `status`, and removes
   the worktree on session delete -- the `snapshot` name and shape are
   already reserved in section 4's proposed contract
   (`workspace_mode="snapshot"`) and H4's revision-pinning work; this item is
   exposing that existing design to `readonly`/`inspect` callers who
   currently hand-roll worktree creation and cleanup themselves.

   **What was found**: `_prepare_request_workspace`/`StoredConversation`
   already had this fully built server-side (detached worktree pinned to
   `HEAD`, `workspace_resolved_sha` tracking) -- it was reachable by no
   client. Exposed at `dispatch`/CLI/MCP. Separately, "removes the worktree
   on session delete" was **not** already true and needed a real fix:
   `delete_conversation`'s "workspace is preserved" is correct for the
   default `shared` mode (it is the caller's own directory) and was silently
   wrong for `snapshot`, leaving every one behind in
   `conversation-worktrees/`. Fixed alongside the exposure. Verified live
   against this repository: dispatched with `--workspace-mode snapshot`,
   `workspace_resolved_sha` matched `git rev-parse HEAD` exactly, the
   session's reported `workspace` was the detached worktree (not the real
   repo), and after `agentrt delete` both the worktree and `git worktree
   list` in the real repo were clean.
9. **Only one LLM profile (item 9). Decided 2026-09-17: skipped, not
   deferred -- structurally blocked by a decision H0 already made on
   purpose.** The plan as written assumed a second profile was a
   configuration addition. It is not: `DeploymentLLMPolicy`
   (`deployment_policy.py`) is a single frozen contract enforced on every
   conversation LLM -- `enforce_agent_policy`/`llm_policy_violations` reject
   any LLM whose `thinking_mode` differs from the one deployment-wide value,
   currently `"enabled"`. A second, non-thinking profile would be
   configured successfully and then refused at every dispatch that tried to
   use it, unless `DeploymentLLMPolicy` itself widens from one frozen value
   to an allowlist -- which is not a config change, it is reopening H0's own
   explicit choice ("no automatic effort reduction," a single allowed
   deployment LLM profile). Asked directly; the answer was to keep the
   single H0 policy rather than reopen it. If this is revisited, it is an
   H0-policy decision first, and only a profile-addition task second.

*Thấp (low):*

10. **Transcript hygiene (item 10). Two of three done, 2026-09-17
    (`254a590`); the `thought` half is not a bug and was left alone.**
    ANSI stripping and a deterministic `progress_summary` on an errored
    `result()` shipped as scoped. The `thought`-population half was written
    above as "a condensation defect, not a data-availability one" -- that
    premise did not survive checking a real persisted event: session
    `2e1d9496`'s `ActionEvent`s genuinely have `thought=[]` with
    `reasoning_content` non-empty on the same event, so the condensation
    code (which reads `thought` correctly) has nothing to populate from.
    The actual deliberation lives in `reasoning_content`, which
    `daemon-behavior.md` already excludes from transcripts on purpose ("the
    model's private deliberation, routinely longer than the code it
    produced"). Surfacing it now would be reopening that documented
    exclusion, not fixing condensation -- treated the same way as the
    `DeploymentLLMPolicy` and spawn-depth items: a decision to make, not a
    task to execute quietly.
11. **Tag key charset (item 11). Done, 2026-09-17 (`f64371d`).** Widened
    `TAG_KEY_PATTERN` to `^[a-z0-9]+(?:-[a-z0-9]+)*$`,
    matching the existing `PLUGIN_NAME_PATTERN`/`CANVAS_EXTENSION_NAME_PATTERN`
    precedent; `control`'s docstring now states the constraint with a
    compliant example. Verified against `test_conversation_tags.py`'s
    existing invalid-key fixture, unaffected (it fails on uppercase, not the
    hyphen).

**Also raised, not part of a numbered item above:**

- The MCP-transport stdout-corruption risk flagged during verification, above
  -- resolve by checking (not assuming) before H8 implementation starts.
- **`wait_any`/`wait_all` hanging past a hidden transport ceiling -- fixed,
  2026-09-17 (`f64371d`).** Read directly: `Client.wait()`
  (`agentrt/runtime/client.py`) is a plain synchronous `while True: ...
  time.sleep(...)` loop, and the MCP tool functions `wait_any`/`wait_all`
  (`mcp_server.py`) take no `Context` and never call `report_progress` --
  the whole call sends **zero bytes back over the MCP channel** for up to the
  full `timeout` requested, no matter how long that is. The loop's own logic
  is correct and returns exactly the documented `still_running`/`timed_out`
  schema *if it is allowed to run to completion*. The break is one layer up: a
  consumer report separately measured a ~1800s idle ceiling on the stdio
  transport between the orchestrator and `agentrt-mcp`
  ("sent no response or progress for 1800s; aborting"), and a large or
  cumulatively-long `timeout` (the default is 600s; consumers pass larger
  ones, e.g. 3600s, or several unsettled sessions push the same call close to
  the ceiling) lets that outer timeout fire first -- killing the connection
  with a generic transport error before AgentRT's own code ever gets to
  return its graceful answer. This is a second instance of "a bound the
  caller cannot see is a bound the caller will cross"
  (`../research/friction-log.md` already named this pattern for
  `list --limit`), now with the mechanism traced rather than only reported.
  Interim guidance until fixed, corrected after a live example this same
  session: a *short* `timeout` avoids the transport-ceiling risk, but the
  orchestrator's own foreground is still blocked for that whole span with
  zero interim signal -- caught live when a user watching the conversation
  asked "wait_all nên là lệnh chạy background hoặc schedule/wakeup chứ?"
  after a `wait_all(timeout=300)` was about to be called directly. The
  correct interim pattern is not "call `wait_*` with a short bound," it is
  "do not hold the orchestrator's own turn on `wait_*` at all" -- background
  a CLI poll loop (`agentrt status` on a short interval, exiting once every
  id settles) instead, or poll `status` directly between other work for a
  session expected to run long. The fix belongs with item 3's paging work:
  `wait_*` should self-impose sub-timeouts safely under the real ceiling
  (measure it, do not assume 1800s transfers to every deployment) and return
  `still_running` at a safe interior boundary -- raising/removing the outer
  bound is not this package's to change, since it sits at the transport the
  orchestrator supplies, not in AgentRT. Item 2's blocking `agentrt wait` CLI
  (H9's convergence item 2 also depends on it) is exactly this backgroundable
  poll loop, built in and named, rather than every orchestrator hand-rolling
  one -- worth moving up in priority given it is also the correct answer to
  today's live question, not only a nice-to-have.
- No `transcript --tail N` for a running session's last few steps without
  reading the full JSONL. A thin wrapper over the existing transcript
  cursor/limit machinery, not a new storage format.
- No expiry/supersession marker for old sessions accumulating across a long
  orchestration run. `tags` already exists and is durable
  (`daemon-behavior.md`); the missing piece is a convention/helper for marking
  one session as superseded by another, not new storage.

**Zero-additional-docs onboarding is a stated requirement, checked directly
against the tool docstrings a fresh orchestrator actually sees -- not against
`docs/`, which `CLAUDE.md` already says "never reaches a session working in
another repository."** The project's own design premise
(`mcp_server.py`'s docstrings "are the product's documentation") makes this
checkable: for any Claude/Codex session installed with no other context, does
the MCP surface alone let it operate without hitting an undocumented
surprise? Audited directly (read every tool docstring, not assumed), 2026-09-17:

- **Already met**, worth naming so it is not accidentally weakened while H8
  ships: `dispatch` states a `readonly`/`inspect` session cannot write its
  answer to a file; the "Measured" bullets state iteration exhaustion lands in
  `error` with no message, and `status`'s own docstring gives the workaround
  (`iterations_used`/`iterations_remaining`); `capacity`'s docstring states a
  full pool queues rather than refuses; `control`'s docstring states `delete`
  is irreversible and gives the measured latency difference between `stop`
  (28s, waits for the boundary) and `interrupt` (2s) unprompted. This is the
  bar the gaps below are held to, not a lower one.
- **Gap: `wait_any`/`wait_all`'s docstrings say nothing about the transport
  idle-ceiling risk** traced above. A fresh orchestrator has no warning before
  passing a large `timeout` and hitting the exact failure a live session
  produced today. The fix for the wait-timeout entry above must ship with a
  docstring line stating the safe bound, not only the code change -- the
  behavioral fix alone does not satisfy this phase's own requirement if the
  tool does not say so.
- **Gap: `control`'s `tag` action's own example does not reveal the key
  charset.** It shows `key=value` pairs (`keep=evidence for the guide,
  round=3`) but never states the `^[a-z0-9]+$` constraint item 11 already
  targets -- a natural key like `superseded-by` fails with no docstring
  warning. Item 11's widened pattern needs the docstring to state whatever
  the new constraint is, explicitly, not leave it to be discovered by a
  rejected call.
- **Gap, newly found in this pass: `dispatch`'s docstring never mentions
  first-run/cold-daemon timing.** A fresh install's first daemon start can
  take up to the documented 240s ceiling (`daemon.py`'s own comment, cited
  in earlier migration work); nothing in `dispatch` warns a first-time caller
  that an apparent multi-minute hang on the very first call is expected
  rather than broken. Add one sentence.
- **Resolved by the `AGENTRT_MAX_SESSIONS` default fix (2026-09-17,
  `config.py`), not by documentation.** The client-side session cap that
  just blocked a real dispatch (one Claude/Codex session's unrelated running
  work exhausting a stricter cap than the daemon's own, which already queues
  gracefully) defaulted to 5; it now defaults to unlimited, so most
  deployments never encounter this gap at all -- the better fix here was
  removing the surprise, not documenting it.

Add to every item above's "Done" gate: the docstring of any tool whose
behavior the item changes states the constraint/limit/timing the item was
about, in the same commit as the behavioral fix -- a fix without an updated
docstring has not met this phase's own onboarding requirement.

**Primary seams:** `agentrt/sdk/llm/llm.py` (retry/logging), `event_service.py`
(start deadline, execution status), `client.py`/`mcp_server.py` (paging,
`_wait_bucket`'s upstream `state` input, CLI `wait`), the `inspect` preset's
search tool, the workspace/artifacts guard (readonly output channel,
snapshot mode), `agent_server`'s LLM profile store (second profile),
transcript condensation, `TAG_KEY_PATTERN`.

**Done:** each numbered item above has a fixture reproducing the reported
defect (or, for items 5 and the stdout risk, reproducing the *relocated* real
one) before the fix, and a regression test after. The retry-visibility and
start-deadline work (items 1, 2's CLI half, 12) is checked against a session
that genuinely takes several minutes of legitimate silent thinking, to confirm
H7's rejected-stall-watchdog scenario still is not killed.
10. On each verified release update MCP docstrings first for operational behavior,
   then daemon/orchestrator/migration docs. Keep safety guidance concise and
   accurate; reducing schema prose is not a reason to remove permission caveats.
   Reconnect MCP separately when validating a changed tool schema.

**Done:** release evidence names source/installed/daemon versions, changed tests,
live fixture outcomes and unresolved limitations. Profile/state migration and
rollback are demonstrated on a copy. Any production cutover is separately
authorized and recorded; documentation never marks planned APIs as shipped.

### H9 — Native sub-agent parity

Basis: a direct request to make AgentRT feel as close as possible to Claude
Code's and Codex's own native sub-agent primitives -- researched against
primary sources (official docs and, for Codex, the public source repository)
rather than recalled, per an explicit instruction partway through this phase's
drafting; the first draft's Claude Code framing and its Codex tool list (drawn
from an old binary string-table extraction) were both incomplete or wrong in
ways the research below corrects. This phase is a comparison and a design,
not a reduction of AgentRT to something it structurally is not -- H7's own
reason for existing (a session must survive the orchestrator exiting) is
incompatible with a purely in-process context fork, and nothing here proposes
giving that up.

**What "native" actually means for each reference point, from primary
sources, corrected from an earlier draft:**

- **Claude Code has four distinct multi-work primitives, not one "Task
  tool"** (code.claude.com/docs/en/agent-sdk/subagents;
  code.claude.com/docs/en/docs/claude-code/agents, "Run agents in parallel").
  **Subagents**: in-session delegated workers, isolated context window, only
  the final message returns to the parent -- this is the one the earlier draft
  called "Task" and described mostly correctly (blocking, one result, no
  session id to track afterward). **Agent view** (`claude agents`, "Research
  preview"): dispatch and monitor sessions running in the *background*, each
  moved into its own git worktree automatically -- this is the primitive
  AgentRT structurally resembles, not Subagents: background, listable,
  monitorable, one session per unit of work. **Agent teams** (experimental,
  off by default): coordinated sessions with a shared task list and
  inter-agent messaging, managed by a lead. **Dynamic workflows**: a script
  running many subagents and cross-checking results, for work too large to
  coordinate turn-by-turn -- the `Workflow` tool available in this very
  session is this primitive. A **forked subagent** inherits the *full* parent
  conversation instead of starting fresh -- documented as "a way to spawn a
  subagent, not a separate surface," which is the closest official analogue to
  `dispatch_from`. Multi-agent work costs roughly 4-7x the tokens of a
  single-agent session, stated directly rather than left to be discovered.
- **Codex's multi-agent v2** (developers.openai.com/codex/subagents;
  `codex-rs/tools/src/agent_tool.rs` and
  `codex-rs/core/src/tools/handlers/multi_agents_v2/spawn.rs` in
  github.com/openai/codex, read directly, not paraphrased from memory). Six
  tools: `spawn_agent`, `send_input`, `send_message`, `followup_task`,
  `wait_agent`, `list_agents`, `close_agent`. **Path-based addressing**:
  agents are named by a hierarchical `task_name` under their spawner
  (`/root/analyzer/summarizer`), not a flat id -- richer than AgentRT's flat
  `short_id` namespace. **`fork_turns`**: `"none" | "all" | <N>`, controlling
  how much of the *spawning* agent's history a new one inherits -- a more
  granular version of what `dispatch_from` currently does as an all-or-nothing
  fork. **`agent_max_depth`** (default 3) bounds recursive spawning; AgentRT's
  `dispatch_from` has no equivalent depth cap today. **Interruption is
  model-visible by default** (`agents.interrupt_message`, default `true`): the
  interrupted agent's own context records that it was interrupted, which
  AgentRT does not currently surface to the dispatched agent at all. **Spawn
  is explicit only** -- "Codex only spawns subagents when you explicitly ask
  it to," and a 2026-06 commit tightened the tool's own description further:
  "Default to doing the work yourself... Do not delegate simple tasks, small
  edits, routine searches, or work you can complete quickly yourself" --
  matching AgentRT's own `dispatch` docstring's existing "WHEN THIS IS WORTH
  IT" framing, not a new idea to import. Batch work
  (`spawn_agents_on_csv`) enforces a **named completion contract**: each
  worker calls `report_agent_job_result` exactly once, and a worker that
  exits without calling it is marked `status: error` with `last_error`
  populated -- an enumerated, named failure mode instead of an undifferentiated
  one, the same shape H8 items 4 and 12 are already building for AgentRT.

**What can converge, and the H8 item or precedent each depends on:**

1. **Named terminal reasons, not one `error`.** Both references enumerate why
   an agent stopped: Codex's `report_agent_job_result` contract names
   `status: error`/`last_error` for a worker that never reported; Claude
   Code's Agent view distinguishes a running/background session from a
   settled one with a real outcome. AgentRT currently collapses "ran out of
   iterations," "provider gave up," and "the agent code raised" into the same
   `execution_status: error` with no reason field (`daemon-behavior.md`,
   confirmed unchanged). H8 items 4 and 12 already commit to a start-deadline
   status and a `finish_reason`/`truncated` field; this item asks for the same
   enumeration to also cover iteration exhaustion and provider failure, so
   every terminal state names a reason from a closed set.
2. **A genuinely blocking, single-result call for the common case.**
   `wait_any`/`wait_all` already block, but the orchestrator still holds a
   session id and calls a second tool (`result`, `usage`, `artifacts`) to
   learn what happened. A `dispatch` variant (or a `wait_all(...,
   mode="collect")` addition) that returns the condensed final answer *in the
   same call* that settles -- title, result text (paged per H8 item 3),
   terminal reason, token usage -- removes the second round-trip for a single
   session, matching Subagents' shape even though AgentRT as a whole is closer
   to Agent view. Multi-session fan-out keeps the current dispatch-then-wait
   split; the convenience is for the common one-session case, not a
   replacement for `dispatch_many`/`wait_all`.
3. **A role, not a parameter tuple.** Claude Code's named subagent types
   bundle model + tools + prompt behind one identifier the orchestrator
   chooses instead of assembling; Codex's TOML agent definitions
   (`.codex/agents/*.toml`, `name`/`description`/`developer_instructions`) do
   the same. AgentRT's `agent_profile_id` (permission preset) is the same
   idea for tool access; H8 item 9 adds a second LLM profile. Extending
   profiles to also carry a short task-shaped description turns profile
   selection into role selection -- additive to the existing
   `permission`/`llm_profile` fields, not a replacement for them.
4. **Partial history fork, not only all-or-nothing.** Codex's `fork_turns`
   accepts `"none"`, `"all"`, or a last-N-turns integer. `dispatch_from`
   currently forks a source session's history as a single mode; adding an
   equivalent bound (fork the last N turns, not the whole thing) is a small,
   precedented extension once item 2's paged result work exists to bound the
   response size of a long source history.
5. **A spawn-depth cap.** Codex bounds recursive spawning at
   `agent_max_depth` (default 3) and returns an error instructing the agent to
   solve the task itself past that. `dispatch_from` has no such cap today --
   check whether a chain of forks can recurse unbounded, and if so, add the
   same kind of limit for the same reason (runaway nesting, not a
   theoretical concern once fork depth is possible at all).
6. **Interruption visible to the interrupted agent.** Codex records a
   model-visible message on interrupt by default
   (`agents.interrupt_message`). AgentRT's `interrupt()`/`finalize()` stop a
   session without the agent's own context ever reflecting that it happened --
   irrelevant to a session that is genuinely done, but relevant to one that
   gets resumed later and has no record of why its prior run ended abruptly.
7. **Verb naming, where it is free.** `dispatch_from`/`dispatch_many`/
   `wait_any`/`wait_all`/`finalize` already read as verbs on an agent rather
   than REST-flavored CRUD, and should stay that way; nothing here proposes
   renaming a shipped surface to chase Codex's exact vocabulary. Where H8
   introduces new surface (item 2's `agentrt wait`, item 8's `snapshot=`),
   name it to match this existing convention rather than the REST paths
   underneath.

**Audited against the actual code, 2026-09-17 -- three items were easier than
written, one is harder, and the premise of two was only partly right:**

- **Items 2 and 4 are cheaper than proposed**, not harder: `Client.wait()`
  (`client.py:1649-1664`) already attaches `result()` to every settled item --
  the "second round-trip" item 2 describes does not exist today, only
  `title`/`usage` are missing from the payload. And `dispatch_from`'s partial
  fork (item 4) already exists end-to-end -- `POST /{id}/fork`'s
  `from_event_id`, `conversation_service.fork_conversation`, and
  `BaseConversation.fork` all support it -- only the client/MCP wrapper never
  exposes the parameter. Neither needs new server-side work.
- **Item 1's premise was partly wrong**: a closed failure-reason vocabulary
  already exists (`ConversationErrorEvent.code` +
  `event/error_classification.py`'s `FailureKind`), and `MaxIterationsReached`
  already classifies into it. The real gap is projecting that existing data
  onto `status`/`result`, not inventing an enum -- H8 items 4/12 should reuse
  `ErrorClassification`, not define a second one.
- **Item 6 confirmed directly, unprompted**: one of this section's own audit
  dispatches hit `LLMBadRequestError` naming exactly H8 item 1's failure
  (recorded below) three tool calls in, and its `InterruptEvent` is real,
  persisted (`local_conversation.py:2638`) and confirmed not
  `LLMConvertibleEvent` -- invisible to the agent on resume, as claimed.
- **Item 3 needs a real decision, not just implementation**:
  `AgentProfile`'s `extra="forbid"` plus `AGENT_PROFILE_SCHEMA_VERSION = 2`
  make adding a description field a persisted-schema migration, and today's
  selection model is strictly one profile per permission preset with a
  mismatched `llm_profile` refused outright (`client.py:1071-1106`) -- "role
  selection" needs a new selection dimension, not an additive field on top of
  `permission`/`llm_profile` as written here.
  **Decided 2026-09-17: deferred to a later phase.** Meaningless with only
  two LLM profiles in play (`deepseek-high` plus H8 item 9's second one) --
  revisit once a third profile makes "which role" a real question, not before.
- **Item 5's "check whether" is answered**: no dispatched or forked session
  has any tool that could call `dispatch_from` -- no permission preset grants
  `delegate`, the SDK task-tool set, or the AgentRT MCP server itself, and
  `enable_sub_agents=False` on every AgentRT profile. Internal recursion is
  structurally unreachable today; a depth cap would police the *orchestrator*
  calling `dispatch_from` repeatedly, not live nesting -- and needs "depth"
  defined first, since `fork` inherits the source's `parent_conversation_id`
  rather than setting one, making forks siblings of their source, not
  children of a chain.
  **Decided 2026-09-17: fix the prerequisite, not the cap. Prerequisite
  done (`3c1fe76`); the cap itself is still not built.** `fork_conversation`
  now sets the fork's `parent_conversation_id` to its actual source instead
  of inheriting the source's own parent, so forks are real children in a
  chain rather than siblings at the same level -- `_children_index()` reads
  this field correctly now. No depth cap was added on top: there is still
  nothing live to cap (no dispatched session can call `dispatch_from`), so
  building one now would still be defending against a risk that does not
  exist yet. Revisit if that reachability premise changes -- e.g. an
  operator registers the AgentRT MCP server in a profile's
  `mcp_server_refs`, which the H9 audit flagged as the one conditional path
  to real recursion.

**What cannot converge, and why not -- stated so nobody spends effort chasing
it later:**

- **No true context sharing.** A Claude Code Subagent's or a Codex sub-agent's
  isolation is enforced by both agents living in the same process; AgentRT's
  isolation is enforced by being a *different* process behind a daemon, which
  is the entire point (the session outlives the orchestrator). `dispatch_from`
  already closes the part of this gap that is closable -- forking another
  AgentRT session's history, now with a precedent for partial fork via item 4
  -- and is explicit in its own docstring about the part that is not: the
  daemon cannot read the orchestrator's own conversation.
- **No push notification over stdio.** Recorded in H7.9 and restated in H8:
  the transport cannot push. A background Claude Code session or a Codex
  agent's completion can reach the caller through the same process boundary
  that dispatched it; AgentRT's boundary is a separate daemon over stdio MCP,
  which structurally cannot. Item 2's blocking `agentrt wait` CLI is the
  closest available substitute -- a real process-exit signal for a caller
  willing to background a process -- not a notification. (Not verified: how
  Claude Code's own Agent view, itself a background-dispatch screen and the
  primitive AgentRT structurally resembles, surfaces completion internally --
  if it also polls rather than pushes, this gap may be smaller than it looks.)
- **No implicit shared filesystem/tool default.** A Claude Code Subagent
  shares the parent's cwd and tools unless told otherwise; AgentRT requires an
  explicit `workspace` and an explicit permission preset on every dispatch, by
  design (`orchestration.md`'s confinement-by-path discussion). This is not a
  gap to close -- an implicit shared workspace is exactly the "shared
  workspaces are not coordinated" hazard the same document already names.
  What *should* close, on precedent rather than by AgentRT's own invention:
  Agent view's automatic per-session worktree is exactly H8 item 8's
  `workspace_mode="snapshot"` proposal, already shipped by the reference this
  phase is chasing -- one more reason item 8 belongs in H8 rather than staying
  optional.

**OpenHands ceremony that AgentRT's own surface can never reach, checked
rather than assumed.** "Native-feeling" is not only added convergence --
some of the daily friction is OpenHands machinery running by default that no
AgentRT-dispatched session can even invoke, because Claude Code and Codex are
*already* the sub-agent layer an orchestrator uses; a second, vendored
sub-agent product living one layer further in is pure cost. Traced, not
inferred:

- **The `delegate` tool and its four builtin sub-agents are dead weight for
  every AgentRT preset, provably.** `register_builtins_agents()`
  (`agentrt/tools/preset/default.py`) unconditionally registers four vendored
  agent definitions (`code_explorer`, `bash_runner`, `web_researcher`,
  `default`, shipped as `.md` files inside the `agentrt-tools` package) at
  every daemon boot, and with `enable_browser=True` also probes for Chromium
  purely to decide whether to include the browser-using one. The tool that
  would let a running session *use* any of this -- `delegate`
  (`agentrt/tools/delegate/impl.py`, which calls `get_agent_factory` to spawn
  a registered agent) -- is granted by **none** of AgentRT's four permission
  profiles: `readonly`, `inspect`, `broad` and `workspace` grant only
  `file_editor`/`terminal`/`task_tracker` (`inspect` also `inspect`), checked
  directly against the live profile files. This is OpenHands' own internal
  multi-agent orchestration, a third, competing sub-agent concept underneath
  `dispatch_from` (AgentRT's) and Claude Code's/Codex's own (the actual
  orchestrator), registered, logged and probed at every boot for a capability
  no dispatched session has ever been able to reach.
- **The VSCode service self-evidently does not belong in a headless,
  MCP-driven daemon.** It fails and warns at every single boot
  (`VSCode server binary not found`, `VSCode service failed to start`) because
  nothing about AgentRT's usage connects an editor to it -- the failure itself
  is the evidence; no session workflow depends on it succeeding.
- **Skills loading (63 files, every restart) is a named, open question, not a
  confirmed finding.** No tool definition anywhere in `agentrt-tools` matches
  `invoke_skill`, so skills are not gated by the same per-preset tool grant
  that proved `delegate` unreachable -- they are very plausibly injected into
  the system prompt by a different mechanism this pass did not trace far
  enough to call either way. Verify before deciding whether to gate it; do not
  extend the `delegate` finding to this one by resemblance alone.

**Corrected by an audit dispatch, 2026-09-17 -- the seam claimed above does
not exist, and one factual claim was wrong.** Read directly (`server_launch.py`
in full, `bootstrap.py` in full, the actual import chain from
`daemon.py`→`server_launch.py`→`agent_server.__main__`→`api.py`→
`tool_router.py`): `server_launch.py` only *imports* workspace classes
additively to register union members -- it suppresses nothing, so "the same
kind of seam" H7 used there is not analogous to skipping a call. `bootstrap.py`
runs **client-side**, invoked lazily by `Client._ensure_ready()` in the
MCP/CLI process, and structurally cannot reach the daemon's own import-time
registration at all. The real call site is
`agentrt/agent_server/tool_router.py:15-18`, a module-level side effect
(`register_default_tools`, `register_builtins_agents`, `register_gemini_tools`,
`register_planning_tools`, all called at import) reached through
`api.py`→`__main__.py`→`server_launch.py`→`daemon.py`. And the Chromium claim
above is **wrong**: `discover_builtin_agents(enable_browser=True)` only filters
a name set, it does not probe Chromium; the actual browser-tool cost is
`register_default_tools(enable_browser=True)` (`tool_router.py:15`, called
regardless of the builtin-agents gate) and `ToolPreloadService` under
`AGENTRT_PRELOAD_TOOLS`, both untouched by gating `register_builtins_agents`
alone.

**Corrected scope, split into what each fix actually is -- both done,
2026-09-17 (`3258414`):**

- **VSCode: genuinely one line, but not at the claimed seam.**
  `AGENTRT_ENABLE_VSCODE=0` already exists
  (`agent_server/config.py:364-367`, `env_parser.py`'s bool parser) and
  `get_vscode_service()` already returns `None` when it is set
  (`vscode_service.py:229-255`). Added to `daemon._daemon_env`
  (`agentrt/runtime/daemon.py:156-180`, the same dict that already sets
  `AGENTRT_PERSISTENCE_DIR` and friends) via `setdefault` -- no vendored
  edit, the seam that actually exists, and an operator's own explicit choice
  is not overridden.
- **`register_builtins_agents`: blocked on there being no seam at all** when
  first found, not merely more work than estimated -- resolved by adding one.
  No config field, no env gate existed. The only
  route that touches no vendored file is an import-time monkeypatch of
  `tool_router`'s bound name inside `server_launch.main()` before
  `runpy.run_module` runs -- fragile, not a real precedent, and not what was
  proposed. The honest options are: accept a small vendored edit (one
  `AGENTRT_*`-gated `if` in `tool_router.py`, matching the style
  `enable_vscode` already uses elsewhere in the same package), or accept the
  monkeypatch and document why. Either is a real decision, not a
  already-safe seam to execute against -- do not start this one without
  picking. **Decided 2026-09-17: the vendored edit.** An `AGENTRT_*`-gated
  `if` around the `register_builtins_agents(enable_browser=True)` call in
  `tool_router.py`, matching the existing `enable_vscode` style in the same
  package -- explicit and traceable in a diff against upstream, over a
  monkeypatch that hides the change from anyone reading `tool_router.py`
  directly and would silently stop working if upstream restructures the
  import.
- **Chromium/browser preload is a separate item this section conflated with
  builtin-agent registration.** If startup cost is the actual goal, it needs
  its own line: `AGENTRT_PRELOAD_TOOLS=0` (or equivalent), independent of
  whatever happens to `register_builtins_agents`.

**Primary seams:** `mcp_server.py`/`client.py` (new `wait`/`dispatch` variant,
terminal-reason enum, `dispatch_from`'s fork-depth and partial-history
parameters, interrupt visibility on the target session), `agent_server`'s
execution-status projection (reason enumeration underlying H8 items 4/12),
the LLM/permission profile store (H8 item 9's role description field),
`agentrt` CLI (item 2's `wait` subcommand naming),
`agentrt.runtime.server_launch`/`bootstrap` (skip builtin-subagent and
VSCode-service registration at AgentRT's own startup, not upstream).

**Done:** an orchestrator using only the converged surface (role-shaped
profile choice, one blocking call for the single-session case, a named reason
on every terminal state) can drive a common single-task dispatch without ever
calling `status` to disambiguate an `error`. The three "cannot converge" items
are documented at the point a new contributor would otherwise propose closing
them, with the reason recorded here rather than re-litigated.

## 4. Proposed API contract

Names below are design targets, not commands to run on the current installation.
Keep existing dispatch parameters and permission/workspace defaults. Additive
fields still require schema and old-client tests; enum changes and semantic
changes need explicit compatibility handling under package policy.

```python
dispatch(
    task=...,
    workspace=...,
    permission="readonly",
    llm_profile="deepseek-high",
    workspace_mode="snapshot",
    title=...,
    max_iterations=...,
    idempotency_key=...,
    attachments=[...],
)

dispatch_many(tasks=[...], max_concurrency=..., idempotency_key=...)
wait_any(session_ids=[...], timeout=..., cursor=...)
wait_all(session_ids=[...], timeout=..., cursor=...)
control(session=..., action="finalize")
```

No effort selector is needed for this deployment: `deepseek-high` fixes it.
`profiles` should describe allowed LLM references, capabilities and immutable
policy without credentials. Admission fields describe preparation/queueing;
they do not replace the SDK's existing execution status. Structured results
carry input/run provenance and current result state independently.

## 5. Feedback coverage

| Input feedback/proposal | Resolution |
|---|---|
| 1. Fan-out cap, dispatch-many, queue and slots | H5; authority moves to daemon with compatibility negotiation |
| 2. Readonly cannot run useful read commands | H1 `inspect` preset with schema-validated readonly operations, not a general shell |
| 3. Large-file truncation, unstable reads, repeated ranges | H1 correctness/cursors/versioning |
| 4. Opaque iteration errors and missing partial progress | H2 errors/progress; H3 explicit summary policy |
| 5. No wait-any/all | H3 authoritative event-backed waits |
| 6. Shared workspace races and stale revisions | H4 pinned snapshots and isolated writers |
| 7. No finalize-now | H3 finalization barrier and honest partial result |
| 8. Inconsistent idle/result/title/update behavior | H2 separate contracts, title preservation and progress |
| 9. No evidence of full-file reading | H1 + H4 versioned observed/delivered coverage, not proof of understanding |
| Permission/model coupling | H0 existing stores/resolver; one allowed deployment LLM profile |
| Flash default and effort controls | H0 direct DeepSeek, thinking on/high fixed |
| Provider-aware scheduling | H5 separate resource/account units and durable admission |
| Cache-aware operation | H0/H6 measured usage and stable prefixes; no forced resume |
| Native multimodal | H6 reuse image content with capability and guard tests |
| Per-model/service telemetry | H0 foundations, H6 accounting and retention |
| Sol, tiers, classifier, auto-escalation | Excluded by user direction |
| Wrong version and source/installed confusion | H0 identity, H7 explicit verification/cutover |
| Overflow is rejected instead of queued | H5 durable acceptance/backpressure; one submit, N outcomes |
| New dispatch reports ambiguous `idle` | H2 separate admission state; H5 authoritative queued/preparing transitions |
| Running result is `""` instead of documented `null` | H2 typed result state plus compatibility-gated JSON `null` |
| No per-session token/cost visibility | H0 REST foundation; H6 complete MCP/CLI accounting and unknown-cost semantics |
| Similar titles and undiscoverable tags | H2 atomic title/tags and compact-list projection |
| Missing conventional CLI `--version` | H0 local runtime version flag |
| No command-capable readonly preset | H1 `inspect` preset with no general shell or file mutation |
| Empty artifacts are ambiguous | H1 typed/documented empty-success semantics |
| Consumer report (2026-09-16) 1. Reasoning-content dropped on resend, session dies | H8 item 1: trace history rebuild, repair-then-retry, not a silent fallback |
| Consumer report 2. No completion signal, orchestrator must poll or block | H8 item 2: shipped 2026-09-17 (`74bba35`) -- `agentrt wait`, exit 0/3 on settle/timeout; push remains impossible over stdio (H7.9) |
| Consumer report 3. `wait_*`/`result` payloads unpaged, overflow client limits | H8 item 3: status+metadata default, paged `result`, matching H1's paging shape |
| Consumer report 4. Truncated final answer with no flag | H8 item 4: `finish_reason`/`truncated` on the final message and finalize summary |
| Consumer report 5. Empty-result error bucketed as `partial`, contradicting the wait contract | H8 item 5: fixed 2026-09-17 (`49a55ad`) -- `_wait_bucket` trusted an upstream `state` field ahead of the text itself; text now decides |
| Consumer report 6. `inspect` search has no `path:line`, rejects file scope, inconsistent counts | H8 item 6: fixed 2026-09-17 (`31e8f14`) -- path:line and file scope; the "inconsistent counts" claim left unconfirmed, not independently investigated |
| Consumer report 7. Readonly/inspect has no report-writing channel | H8 item 7: write-only directory outside the workspace, guarded like it |
| Consumer report 8. No workspace snapshot for readonly fan-out | H8 item 8: shipped 2026-09-17 (`16693c0`) -- exposed the already-built server capability, and fixed a real leak found verifying it (delete never tore the worktree down) |
| Consumer report 9. Only one LLM profile, cannot diversify or avoid a bad provider path | H8 item 9: skipped 2026-09-17 -- blocked by H0's single frozen `DeploymentLLMPolicy`, not a config gap; reopening it is an H0-policy decision, declined |
| Consumer report 10. Transcript `thought` always empty, ANSI in output, no error progress summary | H8 item 10: ANSI stripping and progress summary fixed 2026-09-17 (`254a590`); `thought` is genuinely empty at the source for this deployment, not a condensation bug -- left as a decision (reopen the deliberate reasoning_content exclusion, or not) |
| Consumer report 11. Tag key charset undocumented, rejects hyphens | H8 item 11: widen `TAG_KEY_PATTERN`, matching existing kebab-case precedent |
| Consumer report 12. 0-iteration provider timeout not retried/surfaced | H8 item 12: start deadline distinct from H7's rejected stall watchdog |
| Consumer report + live 2026-09-17 observation: hidden transport idle ceiling under the documented `wait_*` timeout | H8: mechanism traced (silent blocking loop, zero MCP-level signal for the full `timeout`); fix is self-imposed sub-timeouts under the real ceiling, paired with item 3's paging |
| Requirement: any Claude/Codex session installed with AgentRT should operate smoothly with no additional docs handed to it | H8: audited directly against tool docstrings, not `docs/`; three gaps named (`wait_*` timeout risk, `tag`'s undocumented key charset, `dispatch`'s unmentioned cold-start timing) and a "Done" gate added to every item requiring the docstring, not only the code, to state what changed |
| Client-side session cap (`AGENTRT_MAX_SESSIONS`, default 5) blocking a dispatch while the daemon's own pool had room and queues gracefully | Fixed 2026-09-17 (`config.py`): default is now unlimited, deferring to the daemon's already-documented queueing instead of a second, stricter, undocumented refusal |
| Consumer report: no `transcript --tail N` | H8, thin wrapper over existing cursor/limit |
| Consumer report: no expiry/supersession marker for accumulated sessions | H8, convention over existing durable `tags`, not new storage |
| Request to mimic Claude Code's and Codex's native sub-agents, researched against primary sources | H9: named terminal reasons, one blocking single-result call, role-shaped profiles, partial-history fork, spawn-depth cap, interrupt visibility; explicit non-goals where the daemon's own reason for existing forbids convergence |
| Request to strip OpenHands over-engineering that daily Claude/Codex use never reaches | H9: gate `register_builtins_agents`/VSCode-service init at AgentRT's own startup seam; `delegate` and its four builtin sub-agents confirmed unreachable by every permission profile |

## 6. Handoff and update convention

H7's cutover and sub-agent items are done; its staged 50-100 worker scale
verification remains outstanding and should be revisited before that claim is
made. The next code task is **H8**: the fifteen consumer-report items, in the
severity order recorded there -- critical items 1 and 2/12 first, since they
are the only ones that can silently destroy a session's work rather than
merely inconvenience reading its output. **H9** (native sub-agent parity)
should follow H8 rather than run in parallel with it: three of H9's four
convergence points cite an H8 item as their prerequisite, and implementing
them out of order would mean redoing the terminal-reason and paging work
twice.

For every phase, append evidence to a dated result record only after execution:
revision/build identity, test command and outcome, disposable fixture locations,
retained evidence session IDs, rejected claims, limitations and rollback impact.
Register a new result document in `../manifest.json` when it exists; do not
create empty result files or link future files as if they were present. Update
this phase table only with measured status, not worker self-reports.

The [self-audit](../research/deepseek-hardening-audit.md) remains a dated record. New facts go
in subsequent evidence/results and, when shipped, current behavior docs. Preserve
the original P1–P4 history and the user's unrelated working-tree changes.
