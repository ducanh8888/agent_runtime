# Agent Runtime — DeepSeek hardening plan

Date: 2026-09-11 (Asia/Bangkok).
Basis: [self-audit and measured evidence](../research/deepseek-hardening-audit.md), the user's
nine runtime feedback items, and the DeepSeek optimization proposal as narrowed
by the user to direct DeepSeek with thinking enabled and effort `high`.
Planning baseline: `cce40bea488ee7299d312faa07172d0216dfb875`.
Implementation baseline: `203f493a4fb0736b384a1c370556c30d16c9c421`.

## Status

Active follow-on implementation plan; **H0–H6 are complete** apart from one H5 sub-item (a limit on writers sharing one workspace), and **H7 ran its production cutover on 2026-09-12**. H7's staged scale verification and the sub-agent items in H7.9 remain.
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
| H7 | Regression, staged scale verification and deployment | REUSE + NEW tests/docs | All released phases | Cutover done — [result](../results/h7.md); scale and sub-agent items outstanding |

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
9. Carry forward the sub-agent comparison recorded on 2026-09-11. Comparing
   this surface against the native sub-agent lifecycle it mimics left three
   gaps worth closing here rather than in a new phase: (a) context inheritance
   -- a dispatched session receives the task text and nothing else, where a fork
   would carry the caller's conversation and its warm prompt cache; (b) a stall
   watchdog -- an explicit no-progress window that aborts and reports, distinct
   from the SDK's loop detection and from idle eviction; (c) progress
   visibility -- either a pushed completion (transport permitting) or, failing
   that, durable progress inside `status`, because today the orchestrator reads
   a transcript to learn what a session is doing. Each is a contract change and
   needs its own tests and docstrings; none is a reason to weaken the permission
   or workspace guarantees already in place.
10. On each verified release update MCP docstrings first for operational behavior,
   then daemon/orchestrator/migration docs. Keep safety guidance concise and
   accurate; reducing schema prose is not a reason to remove permission caveats.
   Reconnect MCP separately when validating a changed tool schema.

**Done:** release evidence names source/installed/daemon versions, changed tests,
live fixture outcomes and unresolved limitations. Profile/state migration and
rollback are demonstrated on a copy. Any production cutover is separately
authorized and recorded; documentation never marks planned APIs as shipped.

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

## 6. Handoff and update convention

The next code task is H7: verification, documentation and the production
cutover, which now also carries the three sub-agent gaps recorded in H7.9. The
unimplemented H5 sub-item is recorded in its result and should be revisited
before a 50–100 worker claim.

For every phase, append evidence to a dated result record only after execution:
revision/build identity, test command and outcome, disposable fixture locations,
retained evidence session IDs, rejected claims, limitations and rollback impact.
Register a new result document in `../manifest.json` when it exists; do not
create empty result files or link future files as if they were present. Update
this phase table only with measured status, not worker self-reports.

The [self-audit](../research/deepseek-hardening-audit.md) remains a dated record. New facts go
in subsequent evidence/results and, when shipped, current behavior docs. Preserve
the original P1–P4 history and the user's unrelated working-tree changes.
