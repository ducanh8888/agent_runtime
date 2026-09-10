# H0 — direct DeepSeek/high contract and usage foundation

Date: 2026-09-11 (Asia/Bangkok).
Implementation baseline: `203f493a4fb0736b384a1c370556c30d16c9c421`.
Verified code revision: `80de088` (documentation/release record follows it).

## Outcome

H0 passed its source, compatibility, adversarial and disposable live gates.
New AgentRT-created sessions are pinned to the direct DeepSeek/high contract;
the generic SDK/server remains provider-neutral, and persisted sessions keep
their original configuration. The candidate was not installed into the
production daemon; installation and restart remain an H7 cutover action.

H0 uses direct `https://api.deepseek.com`, API model `deepseek-flash`, Chat
Completions, thinking enabled and reasoning effort `high`. The policy applies
to sessions created by the AgentRT deployment; it does not turn the generic SDK
into a DeepSeek-only library. The production daemon was not restarted.

## What shipped

- Neutral `AGENTRT_API_KEY` and `AGENTRT_BASE_URL` aliases with the legacy
  `AGENTRT_9ROUTER_*` names retained. Resolution is per setting across process,
  state and checkout layers; conflicting aliases in one layer fail without
  printing their values.
- Secret-redacted config provenance and the AgentRT runtime's own MCP version.
- Preview/apply for one saved LLM profile, with atomic store writes, stable
  profile IDs and unrelated fields preserved. A provider-linked target is
  refused because its effective endpoint/key cannot be changed truthfully by
  rewriting the profile alone.
- Secret-free LLM profile selection at dispatch. AgentRT permission presets are
  migrated to `enable_switch_llm_tool=false`; already-created sessions are not
  rewritten.
- DeepSeek Flash capability detection and `reasoning_content` replay across
  tool turns. The final serialized Chat Completions JSON retains
  `thinking.type=enabled` and `reasoning_effort=high` even when LiteLLM would
  otherwise drop the top-level fields.
- Correct server/runtime build identity, conventional `agentrt --version`, and
  a redacted per-service/per-call usage projection with raw and normalized
  token views.
- Deployment-only validation after named-profile, explicit-agent and
  agent-settings creation resolve. It validates worker and auxiliary LLMs,
  refuses switch-model bypasses, keeps title generation on a compliant LLM,
  and leaves generic servers and persisted legacy sessions unchanged.
- Optional per-call configured/sent/confirmed provenance in the existing SDK
  metrics owner. Endpoints omit userinfo/query/fragment, sent policy mirrors
  final serialization precedence, and provider confirmation remains `unknown`
  unless an explicit provider signal exists. Legacy records omit the field.
- Context-local provenance prevents concurrent worker and auto-title calls on
  one LLM from consuming or misattributing each other's metadata; deep-copied
  LLMs receive a fresh empty request-local holder.

## Verification

Targeted integration tests:

```text
runtime H0 tests                                      21 passed
server H0/conversation compatibility tests           304 passed
SDK LLM + conversation suite                         1,838 passed, 2 baseline + 1 flaky failure
post-race model-copy/ask-agent regression             44 passed
changed SDK/server files under Pyright                 0 errors
changed runtime files under Pyright + package paths    0 errors
Ruff, pycodestyle, import rules, tool registration     passed
```

The SDK failure is
`test_reasoning_effort_support[openrouter/moonshotai/kimi-k2-thinking-False]`;
it reproduces unchanged on `main@203f493` with the installed LiteLLM metadata.
The broader SDK LLM run found one additional baseline failure in
`test_runtime_metadata.py::test_effective_unchanged_before_resolution`; it also
reproduces on the baseline. The final broad run also hit the existing timing-
sensitive `test_fifo_lock_fairness` once; its expected order passed on three
immediate isolated reruns and no H0 source touches the FIFO lock.

The repository-wide Pyright target is not a clean baseline: its checked
examples still import the pre-rename `openhands` namespace, and the root config
does not include `agentrt-runtime`. The H0 gate therefore ran Pyright over every
changed SDK/server file and over changed runtime files with all local package
paths supplied; both scoped runs completed with zero errors. The nested
pre-commit config also resolves helper-script paths from the Git root rather
than its own directory, so the same Ruff, pycodestyle, import-rule and
tool-registration hooks were run directly from `packages/`.

The SDK's localhost HTTP capture proved each reasoning field occurs exactly
once in the final JSON for both `deepseek/deepseek-flash` and
`openai/deepseek-flash`. It also proved assistant `reasoning_content` is replayed
without merging it into visible content.

A disposable candidate daemon used state
`/tmp/agentrt-h0-live.uH3nJ5` and workspace
`/tmp/agentrt-h0-live-ws.DZtcEe`. Session
`aebb068d-c218-45be-b61f-8e0abc485a94` called `file_editor`, `terminal` and
`task_tracker` in order, returned `SMOKE_OK 2`, then reloaded and returned
`FOLLOWUP_OK` from a follow-up. Its persisted worker and condenser both carried
the direct/high/thinking policy. Six provider calls recorded 313 reasoning
tokens and 59,008 cache-read tokens out of 64,717 prompt tokens. The candidate
daemon was stopped after capture; production PID 20796 was not restarted.

A later live session, `d3f59e63-143a-41c4-abf8-916aa003cdb6`, exposed one
missing provenance record when worker and auto-title calls overlapped. That
claim was rejected, the singleton pending field was replaced with context-local
state, and the deep-copy regression it initially caused was also caught and
fixed. After restart, session `359b2637-196a-49e6-a56d-451c1ab21f27` returned
`PROVENANCE_OK 11`; both of its two provider calls carried direct endpoint,
sent high/thinking-enabled policy, distinct call IDs and honest `unknown`
provider confirmation. The disposable daemon was stopped again.

Final commands included:

```text
uv run pytest tests/runtime -q
  21 passed
uv run pytest <H0 server identity/usage/policy/conversation set> -q
  304 passed
uv run pytest tests/sdk/llm tests/sdk/conversation -q
  1,838 passed; 2 baseline failures; 1 FIFO timing failure
uv run pytest tests/sdk/conversation/test_fifo_lock.py::test_fifo_lock_fairness -q
  passed on 3/3 isolated reruns
uv run pytest <model-copy/provenance/ask-agent/span regression set> -q
  44 passed
uv run pyright <all changed SDK/server files>
  0 errors
PYTHONPATH=<all local package roots> uv run pyright <changed runtime files>
  0 errors
uv run agentrt --version
  agentrt-runtime 0.1.0
```

## Review evidence and rejected claims

AgentRT evidence sessions retained:

- writers: `d8a860cd`, `05454820`, `230e7b72`, `cef7a889`, `90e8d5c9`;
- adversarial/gate review: `51044cd7`, `6556d6b2`, `d7750ef5`, `58fccb37`,
  `bce491c4`;
- next-stage seam audits: `64b85a40` (H1), `785685bb` (H2).

Claims were not accepted from worker summaries alone. Root inspection rejected
three intermediate claims: the first runtime build used ignored `service_id`
and omitted the full high/thinking policy; provider-linked apply reported an
endpoint change it did not make; old AgentRT presets retained the `switch_llm`
bypass. Corrective commits and regressions followed each finding.

The first `01f257f` integration snapshot was also rejected as “H0 complete”:
only the worker HTTP path was covered, title/explicit creation paths could
diverge, the live multi-tool gate had not run and policy provenance was partial.
The final live gate then rejected the first concurrent provenance design and
the full SDK gate rejected its first ContextVar fix because it broke deep
`model_copy`. Both failures have deterministic regressions. Final reviewer
`bce491c4` reported PASS after the original blockers were closed; root retained
the transcript because the same session twice hit opaque iteration/error
behavior before producing a result.

## Remaining limits

- Finalization does not exist until H3. Its no-tools/high summary contract is an
  H3 gate, not evidence that can truthfully be produced in H0. The original
  runbook's H0-finalize check was therefore a dependency error and is recorded
  as such rather than marked passed.
- Pending call provenance is now concurrency-safe, but the pre-existing shared
  request timing/log context remains outside H0; H6 owns complete per-call
  latency and missing-usage accounting.
- The direct model is not mapped by the installed LiteLLM cost table, so a
  stored `accumulated_cost=0.0` means “no positive cost recorded”, not “free”.
  H6 owns explicit price normalization and unknown-vs-zero treatment.
- The live smoke reproduced the existing explicit-title overwrite race; H2 owns
  the request/title lifecycle fix.
- Additional consumer feedback was integrated into the active plan: H1 now owns
  an `inspect` preset and typed empty-artifact semantics; H2 owns admission/result
  states plus atomic title/tags; H5 explicitly queues overflow instead of
  rejecting it; H6 exposes per-session usage through MCP/CLI.

## Rollback

The pre-H0 state/profile backup is
`/home/ducanh/.agentrt-backups/pre-h0-20260911-TxMyf5`. Source rollback is a
normal revert of the H0 commits; no schema migration is required for legacy
sessions. Do not run an older binary over state after a future migration.
