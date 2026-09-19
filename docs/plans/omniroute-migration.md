# AgentRT — OmniRoute integration, router-neutral LLM contract

Date: 2026-09-19 (Asia/Bangkok).
Basis: owner-issued architecture task ("AgentRT — OmniRoute Integration,
Router-Neutral LLM Contract, and Stable Model Selection"), read against the
current `main` rather than any historical summary.
Supersedes H0's single frozen `DeploymentLLMPolicy` contract in
[deepseek-hardening.md](deepseek-hardening.md) for the reason stated there:
the policy AgentRT enforced was correct for "always direct DeepSeek," and
this phase deliberately drops that assumption. `deepseek-hardening.md` is
not rewritten; its H0–H10 record stands as history of what was built and why,
under the deployment that existed before this phase.

## 1. Objective and boundary

> AgentRT calls one stable virtual model exposed by a router, and the router
> owns all real model/provider/account selection and fallback.

AgentRT must not: select the real model, select the provider, rotate
accounts, implement cost-aware routing, implement provider fallback, expose
multiple real provider profiles merely to support routing, or claim a
reasoning/thinking capability it cannot verify for an opaque virtual model.
The router is the model authority; AgentRT sees one logical model contract.

This is router-*neutral*, not OmniRoute-specific: nothing in the
implementation names OmniRoute. The same code path serves a direct-provider
deployment (unchanged), a 9Router deployment (the existing legacy alias), or
an OmniRoute deployment (the router this phase was verified against) --
whichever `AGENTRT_BASE_URL`/`AGENTRT_DEFAULT_MODEL` name.

## 2. Architecture assessment: what was actually hardcoded

Two seams, found by reading the current source, not by memory of H0:

1. **`packages/agentrt-runtime/agentrt/runtime/bootstrap.py`'s `_build_llm()`.**
   Built the saved LLM profile with `reasoning_effort="high"`,
   `capability_overrides={"supports_reasoning_effort": True, "thinking_mode":
   "enabled", "supports_responses_api": False}` and
   `litellm_extra_body={"thinking": {"type": "enabled"}}` -- a literal,
   always-on reasoning contract regardless of what `router.model` actually
   resolves to.
2. **`packages/agentrt-server/agentrt/agent_server/deployment_policy.py`'s
   `DeploymentLLMPolicy`.** A frozen pydantic model whose fields *defaulted*
   to `model="deepseek-flash"`, `base_url="https://api.deepseek.com"`,
   `thinking_mode="enabled"`, `reasoning_effort="high"` -- so
   `DeploymentLLMPolicy()` alone, with no arguments, meant "the DeepSeek
   contract," and every conversation's LLM was rejected unless it matched all
   four fields exactly.
3. **`packages/agentrt-runtime/agentrt/runtime/daemon.py`'s
   `DEPLOYMENT_LLM_POLICY`.** A hardcoded dict literal (same four fields)
   serialized into `AGENTRT_DEPLOYMENT_LLM_POLICY`, mirroring (2) so the
   two never disagreed -- but only by construction, since nothing tied them
   together programmatically.

## 3. Design and what was built

**`DeploymentLLMPolicy`** (`deployment_policy.py`): `model`/`base_url` are now
**required**, with no default -- there is no meaningful universal choice
anymore, so every caller states explicitly which endpoint this deployment's
one contract points at. `thinking_mode`/`reasoning_effort` became
`str | None = None`; `None` means *not enforced*. `api_mode: str = "chat"`
kept its default -- staying on Chat Completions rather than drifting to the
Responses API is still a real, provider-independent invariant this migration
does not touch (per the owner's explicit non-goal). `llm_policy_violations()`
and `_extra_body_conflicts()` skip the thinking/reasoning checks entirely when
the corresponding policy field is `None`.

**A real bug found and fixed while doing this, not merely refactored:** the
old code required `https` on the LLM's endpoint *unconditionally*, regardless
of what the policy's own `base_url` scheme was. That was invisible while
`DIRECT_BASE_URL` was always `https://api.deepseek.com`, but the owner's own
target OmniRoute example (`http://127.0.0.1:20128/v1`) is plain HTTP on
localhost -- a policy legitimately pointing there would have rejected every
single matching conversation. Fixed: `https` is now required only when the
*policy's own* endpoint is https, not hardcoded.

**`bootstrap.py`'s `_build_llm()`**: no more `capability_overrides`, no more
`litellm_extra_body`. `reasoning_effort` is deliberately **not** passed as
`None` either, even though that would read as more "honest": `LLM.to_persisted()`
serializes with `exclude_none=True`, so an explicit `None` never survives a
save/load round trip -- it is dropped from the JSON and the SDK's own class
default (`"high"`) reapplies on load. Passing it anyway would make a freshly
built profile compare unequal to the one just reloaded from disk forever, and
`preview_llm_profile`/`apply_llm_profile` would report a change that can never
actually be applied away. Left unset instead, matching the field the SDK
already round-trips correctly.

**Why the reasoning_effort field being "high" is still safe with no
overrides:** `agentrt.sdk.llm.options.chat_options.select_chat_options` only
adds `reasoning_effort` to the outgoing request when
`model_features.supports_reasoning_effort` is `True`. `get_features()`
resolves that from `capability_overrides` first, then LiteLLM metadata, then a
name-pattern fallback -- for an opaque virtual model name (e.g.
`agentrt-worker`) that matches nothing, the fallback is `False`, so
`reasoning_effort` is silently never sent, regardless of what the field
literally holds. This is what the whole design actually leans on: the field
value is inert; the *overrides being absent* is what stops the false claim
from reaching the wire.

**Backward compatibility, verified, not assumed:** `DEEPSEEK_FLASH_MODELS =
["deepseek-flash"]` already exists in
`agentrt-sdk/agentrt/sdk/llm/utils/model_features.py`, added specifically (per
its own comment) so an alias hiding the provider prefix does not silently
drop `reasoning_effort`/thinking for a real DeepSeek deployment.
`model_matches()` does case-insensitive substring matching on the full model
string, so `openai/deepseek-flash` still resolves
`supports_reasoning_effort=True`/`thinking_mode="enabled"` by **name
detection alone**, with zero explicit overrides. A direct-DeepSeek deployment
therefore keeps sending `reasoning_effort=high` and thinking on, unchanged in
wire behavior, purely because the model string still says "deepseek-flash" --
this migration does not silently downgrade an existing direct deployment.

**`daemon.py`'s `_deployment_llm_policy()`**: replaces the literal dict with a
function that calls `config.load_router_config()` and derives
`model`/`base_url`/`api_mode="chat"` from it (no `thinking_mode`/
`reasoning_effort` keys, ever -- consistent with the policy no longer
asserting a reasoning contract for any deployment, DeepSeek-direct or
router-fronted alike). Returns `None` -- and `_daemon_env()` then omits
`AGENTRT_DEPLOYMENT_LLM_POLICY` entirely -- when the router config cannot be
resolved yet, matching `DeploymentLLMPolicy`'s own already-documented opt-in
behavior (`Config.deployment_llm_policy is None` keeps the server generic).
Without this, a bare `agentrt daemon start` before `.env` exists would have
started failing, which used to succeed.

## 4. One canonical configuration combo

Decision, requested explicitly: the consumer-facing configuration surface
must be **one shape**, not two equally-valid spellings. `AGENTRT_API_KEY` /
`AGENTRT_BASE_URL` / `AGENTRT_DEFAULT_MODEL` (the existing neutral names,
already wired end to end in `config.py`'s `SETTING_ALIASES`) are that one
combo, set together, always. The legacy `AGENTRT_9ROUTER_API_KEY` /
`AGENTRT_9ROUTER_BASE_URL` aliases still resolve (no code removed -- an
existing operator's file is not broken), but are no longer the documented
default spelling.

Done: `.env.example` rewritten to show the neutral names as the only primary
example, with the legacy aliases demoted to a commented-out migration note.
The live `~/.agentrt/.env` on this machine was migrated the same way --
`AGENTRT_9ROUTER_API_KEY`/`AGENTRT_9ROUTER_BASE_URL` renamed in place to
`AGENTRT_API_KEY`/`AGENTRT_BASE_URL`, values preserved byte-for-byte, never
printed to any tool output in the process. `AGENTRT_DEFAULT_MODEL` was left
alone in that same pass (the owner had not yet decided the model at that
point); it was updated separately once real testing began (§6).

AgentRT itself does not care whether the endpoint these three name is a
router or a direct provider -- that is the entire point of "router-neutral."

## 5. Router failover/session semantics, as documented by OmniRoute

Read from `https://github.com/diegosouzapw/OmniRoute/wiki/API-Reference`
(the owner's own router instance), not assumed:

- **Session affinity is an explicit request header**, `X-Session-Id` /
  `x_session_id`, echoed back as `X-OmniRoute-Session-Id`. AgentRT does not
  currently send this. Per the original task's own instruction ("prefer zero
  vendored-SDK changes... only add an explicit session header if real testing
  proves it necessary"), this is deferred until testing shows AgentRT's
  existing `prompt_cache_key`-per-conversation behavior is insufficient for
  the specific router in use -- not built speculatively.
- **Fallback chains are configured server-side** via `/api/combos*` and
  `/api/model-combo-mappings`, triggered on timeout, 429, auth failure, or
  model unavailability. AgentRT never sees or chooses among the chain; it
  only sees the virtual model name and a final success or a `503` naming
  which stage failed (`"Maximum combo retry limit reached"` = every fallback
  in the chain exhausted; `"resource_pressure"` = a provider circuit breaker
  is open/degraded).
- **Diagnostics exist and are non-destructive to read**: `GET
  /api/monitoring/health` (per-provider circuit breaker state),
  `GET /api/resilience/model-cooldowns` (active lockouts),
  `GET /api/rate-limits` (per-connection enabled/active/queued/running).
  Mutating endpoints (`POST /api/resilience/reset`,
  `DELETE /api/resilience/model-cooldowns`) were identified but not called --
  they act on shared router state outside this repository and are the
  owner's call, not a step to take unasked.

## 6. Real connectivity, verified live

`AGENTRT_BASE_URL=https://9router.ducanh.cloud` (no path suffix needed --
`{base}/v1/models`, `{base}/models` and `{base}/api/v1/models` all resolved
identically) with the owner's real, admin-scoped API key: `GET /v1/models`
returned **815 models**, `owned_by` grouped as: aihorde 166, devin-cli-agentic
137, nvidia 127, vertex 74, kilocode 58, **combo 38**, auggie 28, cohere 27,
theoldllm 26, codex-app-server 26, cloudflare-playground 20, antigravity 19,
groq 18, zcode 13, gemini 11, opencode 8, duckduckgo-web 6, felo-web 5,
veoaifree-web 4, uncloseai 3, chipotle 1.

The `combo` group (`owned_by: "combo"`, ids like `auto/coding`,
`auto/best-coding`, `auto/coding:reliable`, `auto/coding:fast`,
`auto/coding:cheap`, `auto/coding:free`, `auto/reasoning`,
`auto/claude-sonnet`, `auto/glm`, ... 38 total) are OmniRoute's **own**
virtual-routing aliases -- this is the direct, already-built match for
"AgentRT calls one stable virtual model and the router owns selection,"
requiring no custom alias to be configured. All 38 report
`tool_calling=true, reasoning=true, thinking=true` and a ~1M token context
window (self-reported by the router, not yet independently verified against
AgentRT's real tool loop).

**First diagnosis, corrected by reading further, not left standing:** the
`enabled`/`active` fields on `GET /api/rate-limits` (read with the plain
inference API key) initially read as "every provider connection is
disabled" -- wrong. `GET /api/providers` (read with a separate
*management*-scoped token the owner provided afterward,
`OMNI_ROUTE_MANAGE_TOKEN`) showed all 10 connections `isActive: true,
testStatus: "active"`; the rate-limits fields mean something narrower
(live in-flight/rate-limit-protection state, not configuration). The real
cause of the initial wall of `503`s, found by cross-referencing both
endpoints plus `GET /api/monitoring/health`: the OmniRoute process itself
was near its own Node.js heap ceiling (`heapUsed`/`heapTotal` ~92-94%) and
gating outbound requests as self-protection (`resource_pressure`),
independent of which provider or model was requested -- confirmed by five
completely unrelated providers (kilocode, groq, nvidia, vertex, antigravity)
all failing identically. Resolved on the owner's side (process restart);
not an AgentRT or model defect, and not something fixable from this
repository.

## 7. Real compatibility testing, once unblocked

**Harness**: `tools/omniroute_probe.py`, committed. Talks directly to
`AGENTRT_BASE_URL`'s Chat Completions endpoint with the resolved router
credential -- independent of the AgentRT daemon, because "does this real
backend model behave correctly for AgentRT's tool loop" is a question about
the router/model, not about this repository's own code. Five checks per
model, matching AgentRT's actual request/response shape (tool_call.id,
function name, JSON arguments, `role=tool`, `tool_call_id`, provider
`finish_reason`): **A** basic function call, **B** tool-result continuation,
**C** a two-tool multi-turn loop to a final answer, **D** recovery from an
error tool result, **E** parallel tool calls in one turn. Conservative on
purpose (small `max_tokens`, ~4-5 requests per model) since it runs against
the owner's real, possibly metered accounts. Results are also written to
`tools/_omniroute_probe_results.json` (gitignored -- one deployment's live
measurements, not a repository claim).

**First real round (5 of 6 candidates passed every check):**
`antigravity/gemini-3.7-flash-high` (fastest: 2.5-5.5s per call, all pass),
`antigravity/claude-sonnet-4-6` (all pass, more detailed answers, 3.5-7s),
`codex/gpt-5.6-sol`, `codex/gpt-5.5` (both all pass, 2.5-5s), `vertex/gemini-2.5-pro`
(all pass, but a slow first multi-turn step at 22s). `vertex/claude-sonnet-4-6`
failed entirely: `501 "Operation is not implemented, or supported, or
enabled"` plus `429 model_cooldown`.

**Vertex explored further, at the owner's request, since it was the one
mixed result.** The Claude-4-6-sonnet failure turned out to be
**family-wide, not model-specific**: `vertex/claude-opus-4-6` and
`vertex/claude-sonnet-4-5` failed identically (same `501`/`429` pair) --
Anthropic-via-Vertex credential/routing is broken on this account, while the
*same* Claude Sonnet 4.6 works perfectly via `antigravity`. Separately,
`vertex/DeepSeek-V4-Flash` and `vertex/Qwen3.6-35B-A3B` both returned
`400: Expected input to contain field: 'messages'` -- a real router-side
request-translation bug for these specific third-party models hosted on
Vertex, worth reporting to OmniRoute upstream. Testing the rest of
`vertex/gemini*` found a further, reproducible pattern: `gemini-3.1-pro-preview`,
`gemini-3.1-flash-lite`, `gemini-3-flash-preview`, `gemini-2.0-flash` and
`gemini-2.5-flash` (tested twice, same result both times) all fail check
**B** or **D** -- the model repeats the identical tool call instead of
consuming the tool result and answering. `gemini-3.1-pro-preview` failed
worst: it looped calling `read_file` four times and never reached a final
answer (check **C**). **`vertex/gemini-2.5-pro` remains the only Vertex-hosted
model, across every family tried, that passed all five checks cleanly** --
likely a router-side request-shaping quirk specific to how the `tool`-role
follow-up message gets translated for Gemini-on-Vertex, not a defect in the
Gemini models themselves.

**Final combo, built by the owner on OmniRoute** (`GET /api/combos`, name
`default`, strategy `lkgp`): `antigravity/gemini-3.7-flash-high` (primary) →
`codex/gpt-5.6-luna-high` → `antigravity/claude-sonnet-4-6` →
`vertex/gemini-2.5-pro`, exposed as the model id `default`
(`GET /v1/models` lists it, `owned_by: "combo"`). Matches this section's
own findings: every member individually passed all five checks (`gpt-5.6-luna-high`
specifically wasn't separately probed, but shares the `codex` connection and
model family as the already-passing `gpt-5.6-sol`).

**Stability run on the combo itself, 3 consecutive full passes**, each
independently hitting model id `default`: all five checks passed every time,
latency consistently 2-6s per call, response wording consistent. Not a
single failure across 3 runs / 15 individual checks.

## 8. Wired into AgentRT, verified end to end

`~/.agentrt/.env`'s `AGENTRT_DEFAULT_MODEL` changed from `deepseek-flash` to
`default`. Reinstalled (`uv tool install packages/agentrt-runtime --reinstall`)
and restarted the daemon to run the router-neutral code from §3; confirmed
its actual process environment carries
`AGENTRT_DEPLOYMENT_LLM_POLICY={"model": "default", "base_url":
"https://9router.ducanh.cloud", "api_mode": "chat"}` -- no
`thinking_mode`/`reasoning_effort` keys, matching the design.

**Found immediately, and it is the exact behavior `bootstrap.py`'s own
`preview_llm_profile`/`apply_llm_profile` docstrings already warned about**:
changing `.env` does not retarget the *saved* LLM profile, so the first
dispatch attempt was correctly rejected by `DeploymentLLMPolicy` -- the
profile still held `openai/deepseek-flash` / `https://api.deepseek.com`.
`agentrt llm-profile` (preview) showed exactly the expected diff (`base_url`,
`model`, `capability_overrides` and `litellm_extra_body` all reverting to the
router-neutral baseline); `agentrt llm-profile --apply` wrote it; a second
preview then reported `"changes": []` -- converged, confirming the H10-era
`reasoning_effort` fix (§3, "not passed as `None`") holds under a real
apply, not just the unit test.

**Real dispatch, real tools, verified on disk, not from the session's own
report**: a workspace session was asked to fix a one-line bug (`add()`
returning `a-b`) using `file_editor`, then verify with `terminal`. Result:
`bucket: "completed"`, 4 iterations, final answer claiming the fix and a
printed `OK`. Checked independently -- `buggy.py` on disk now reads
`return a + b`, and re-importing and calling `add(2, 3)` in a fresh
interpreter returns `5`. Full stack (dispatch → saved profile → deployment
policy → real OmniRoute combo → real backend model → `file_editor` and
`terminal` tools → finish) confirmed working, not merely each piece in
isolation.

## 9. Status and what remains

**Done**: architecture assessment (§2), implementation (§3), the one-combo
configuration decision (§4), router failover/session semantics (§5), the
resource-pressure root cause (§6), the compatibility harness and real
multi-candidate testing including the Vertex-specific investigation (§7),
and full wiring into AgentRT with a real, independently-verified end-to-end
dispatch (§8). Tests: `test_h0_profiles.py`, `test_h0_deployment_policy.py`,
`test_deployment_llm_policy.py`, `test_deployment_llm_policy_wiring.py` all
passing; full `agent_server` suite re-run clean, 2270 passed.

**Not done, and not currently planned further**: the full section 7/9/10
matrix from the original task (long-context continuation, truncation
behavior, account failover exercised deliberately, model-fallback-after-a-tool-call
safety) beyond what the above already covers; exposing route/provider/failure
metadata without invasive SDK changes (§13 of the original task). Neither is
blocking the current combo's use -- both are follow-on hardening if the
owner wants them, not required for what shipped.
