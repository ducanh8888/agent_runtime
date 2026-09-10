# DeepSeek hardening — self-audit and evidence

Date: 2026-09-11 (Asia/Bangkok).
Source baseline: `cce40bea488ee7299d312faa07172d0216dfb875`.
Basis: the user's nine AgentRT feedback items, the subsequent DeepSeek
optimization proposal, and the local/API probes recorded in this conversation.
Related: [implementation plan](DEEPSEEK_HARDENING_PLAN.md),
[friction log](FRICTION_LOG.md), [Linux baseline](LINUX_BASELINE.md).

## Status and authority

This is a dated self-audit of the conversational plan, not a release report.
The primary agent performed this review; no new delegated review or paid model
probe was run for this documentation pass. The earlier AgentRT review below is
supporting context, not authority for the conclusions here.

The user fixed the scope to DeepSeek direct, thinking enabled, effort `high`,
with no Sol or cost-driven effort reduction. Writing these documents does not
change runtime configuration or authorize a production restart. No hardening
phase is complete merely because this audit is written.

## 1. Corrections to the conversational plan

| Finding | Decision carried into the plan |
|---|---|
| Reusing P0–P6 would collide with the repository's closed P1–P4 history. | Use H0–H7; keep the original plan and result reports intact. |
| SDK `reasoning_effort="high"` is not proof that HTTP contains the field. | Test the final serialized request for every LLM service; distinguish configured/sent/confirmed-or-unknown. |
| An always-high deployment policy could accidentally become a global SDK restriction. | Enforce it in the selected AgentRT deployment/profile. Preserve generic SDK behavior and existing running sessions. |
| Adding `run_id` alone does not fix an in-flight `send`. | Record input consumption boundaries as well as execution identity; do not label an older answer as answering a later request. |
| Changing empty string to null or adding execution-status enum values is not automatically backward-compatible. | Keep legacy representation where required; add result/admission metadata and an explicit current-request contract. Test serialized old-client fixtures. |
| Automatically spending the last iteration on a summary changes the existing cap's meaning. | Preserve the legacy hard limit. Make reserved-summary behavior explicit and count the summary within the configured allowance. |
| `finalize_now` sounded like immediate, guaranteed cancellation. | Stop new tools, acknowledge in-flight uncertainty, then summarize at a safe boundary. No rollback or exactly-once claim. |
| A dirty overlay is not necessarily an atomic snapshot of an externally edited repository. | Guarantee a frozen captured copy, report capture interval and consistency level, and refuse an atomic-point-in-time claim without stronger isolation. |
| Capturing a snapshot only when dequeued can review a different revision. | Pin the commit at dispatch admission; freeze an optional dirty overlay before the item becomes runnable. Expose preparation separately. |
| Queue ownership/idempotency does not guarantee exactly-once side effects. | Deduplicate submission; stop for reconciliation when execution outcome after a crash is unknown. |
| One concurrency number conflates sessions, LLM requests and workspace writers. | Track separate limits and units. Admission capacity must not hold an LLM slot while waiting for a workspace lock. |
| Structured `git status`/`diff` is not safe merely because `shell=False`. | Disable external helpers, textconv, fsmonitor and optional writes as applicable; guard output paths and test hostile repository configuration. |
| Cache metrics and persistent cost accounting were too late in the sequence. | Add per-call usage/provenance foundations in H0; complete accounting and deletion semantics in H6. |
| A second daemon with a different state directory cannot transparently serve old session IDs. | Use side-by-side instances for explicit tests only. Production cutover requires drain, state compatibility verification, backup and controlled reconnect. |
| Old spend figures and deployment permissions in historical docs could be mistaken for current authorization. | Do not infer an unlimited paid load test or permission to restart from historical prose or from “do not save tokens.” |

The first useful release remains H0–H3. H4 must precede shared-repository
multi-writer scale tests. Vision and the complete accounting view are part of
the planned work, not prerequisites for correcting result or file-read bugs.

## 2. Evidence already obtained

These are observations from the preceding audit/probe turns, not reruns on the
documentation date. Session UUIDs are local evidence references, not portable
fixtures. A future implementation must encode the relevant reproductions in
tests; it must not depend on these sessions still existing.

### Source findings

- [File output formatting](../packages/agentrt-tools/agentrt/tools/file_editor/editor.py)
  calls `maybe_truncate` before line enumeration in `_make_output`. The prior
  synthetic reproduction displayed source line 500 with a much lower line
  number after head/tail clipping. This is a correctness bug, not just a missing
  paging convenience.
- [Final response extraction](../packages/agentrt-sdk/agentrt/sdk/conversation/response_utils.py)
  scans historical agent messages/finish actions without a current-input
  boundary. A later user message can therefore coexist with an older answer
  returned as the final response.
- [Runtime client](../packages/agentrt-runtime/agentrt/runtime/client.py)
  does not surface SDK error events in its condensed transcript and uses a
  client-side running-session cap. Explicit dispatch titles are sent, but the
  create schema inspected in the audit lacks the corresponding title field.
- [Profile resolver](../packages/agentrt-sdk/agentrt/sdk/profiles/resolver.py)
  already composes an agent profile with a referenced LLM profile. A second
  model registry and permission/model/effort Cartesian product are unnecessary.
- [Chat options](../packages/agentrt-sdk/agentrt/sdk/llm/options/chat_options.py)
  gate effort serialization on capability detection. The previous local probe
  of `openai/ds/deepseek-flash` omitted requested `low`, `high` and `max` from
  the selected options. Vision and SDK prompt-cache activation were also false.
  This does not establish that the remote provider disabled thinking/cache.
- File editor already returns image content for supported images. The missing
  work is capability/transport validation and a guarded attachment surface.
- Existing worktree creation is not a promise to pin the user's current HEAD
  or uncommitted files. Do not expose the existing boolean as a snapshot API.
- The installed package is a snapshot, not a live view of checkout changes.
  The prior audit found source/installed server differences. Record both before
  every implementation verification.

### Direct endpoint comparison, 2026-09-10 UTC

The small raw API probe sent the same task and tool schema through both paths,
with thinking enabled and requested effort `low`. This predates the user's
always-high requirement: it is not a high-effort benchmark. Both paths called
the tool and returned the exact supplied marker.

| Raw two-request probe | DeepSeek direct | 9Router |
|---|---:|---:|
| Sum of request durations | 1.320 s | 7.197 s |
| Reported prompt tokens | 735 | 5,499 |
| Reported cache-read tokens | 256 | 896 |
| Reported completion tokens | 110 | 47 |
| Reported reasoning tokens | 63 | 0 |

This is one small sample, not a latency distribution, bill comparison, or
verification that the gateway honored the requested effort. The reported
token difference may reflect gateway-added context, accounting, or other
transport differences; it does not identify the cause.

The paired AgentRT task read exactly two source ranges. Apart from endpoint,
credential and model route, the test settings/task were matched. Both carried
the profile's configured `high`; effective effort at both upstreams was not
independently proven. Existing default profiles were not changed.

| AgentRT session | Result | Prompt tokens | Cache-read tokens |
|---|---|---:|---:|
| Direct `908b8144-2d91-410f-a616-913fe6e3e4c7` | Correct permission mapping and `default="high"`, with correct source lines | 18,045 | 0 |
| 9Router `961755b0-d1a8-4d8d-8427-1eb537f31734` | Incorrectly reported unrelated git text instead of file content | 20,455 | 384 |

The persisted observation text blocks (joined with newlines and hashed as
UTF-8) were byte-identical between the sessions:

- `client.py`, lines 360–380, 1,076 characters:
  `c527b1bab84296e1a6377bca0c50576476cf82782ab516114e8321b048af59ee`.
- `llm.py`, lines 548–558, 664 characters:
  `04d15abcde2a410452e7b3972eff1e8a4fd1a208bb9eb297f40a268e076c83cd`.

Neither observation contains “nothing to commit.” This excludes that text
from the persisted file-viewer output for these calls, but does not prove what
the upstream model received after serialization/gateway handling. Keep the
file-numbering bug separate from this observation/report mismatch.

Direct cache being zero in this AgentRT sample, while the raw API sample had
cache hits, does not by itself prove an SDK accounting bug: the requests and
cache conditions differ. H0 needs a matched raw-usage/normalized-usage probe.

### Earlier design review

Session `66c3d0a8-feea-4b60-8402-57c11a8008b1` used DeepSeek direct with
explicit thinking/high configuration to critique supplied design evidence.
Its useful cautions were auxiliary LLM calls, input-consumption boundaries,
snapshot timing and crash recovery. Its suggestions are not blanket acceptance
criteria: in particular, submission deduplication cannot justify an assertion
that an arbitrary external side effect executes exactly once.

## 3. Constraints and unresolved measurements

- At the end of the endpoint probe, the state `.env` pointed to DeepSeek direct,
  while the saved default LLM profile still pointed to 9Router. This is dated
  state, not a guarantee about the next operator edit. Re-read safe fields
  before applying any migration; never copy keys into docs.
- Need transport-level proof of high for worker and auxiliary services, and
  image round-trip validation through the actual installed stack.
- Need deterministic regressions for in-flight send, stop-hook completion,
  long-line paging and crash recovery before implementation claims are accepted.
- Need provider-account limits and host resource measurements before choosing
  production concurrency numbers. A configured zero is not provider infinity.
- Need a reproducible Linux test baseline for the changed package areas. The
  historical Linux report does not claim a completed full vendored test suite.
- Need an explicit production cutover window. Safe source/doc work can proceed
  without it; replacing a live daemon cannot be treated as automatic cleanup.

## 4. Provider references

References consulted during the preceding research (2026-09-10); re-probe the
installed transport rather than treating these pages as integration evidence:

- [DeepSeek first API call](https://api-docs.deepseek.com/): direct endpoint and
  API model name.
- [Thinking mode](https://api-docs.deepseek.com/guides/thinking_mode/): explicit
  effort and tool-history requirements.
- [Models and pricing](https://api-docs.deepseek.com/quick_start/pricing/):
  provider price schedule, not a 9Router bill.
- [Rate limits](https://api-docs.deepseek.com/quick_start/rate_limit/): account
  request concurrency, not AgentRT live-session count.

No release benchmark is used as a hardening acceptance criterion. Do not export
credentials, raw private reasoning or full private provider request bodies as
test evidence; use synthetic fixtures and redacted metadata/hashes.
