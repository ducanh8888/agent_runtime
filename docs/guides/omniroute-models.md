# OmniRoute models for AgentRT

Use OmniRoute's stable router model names, not an upstream provider/model ID. AgentRT's LLM contract is router-neutral: configure the endpoint and key with `AGENTRT_BASE_URL` and `AGENTRT_API_KEY`, choose the default alias with `AGENTRT_DEFAULT_MODEL`, and let the router resolve that alias to a provider. Always send `max_tokens` explicitly; do not rely on a bridge default.

## Choose a model

Set the normal-session default to:

```dotenv
AGENTRT_DEFAULT_MODEL=agentrt-worker
```

This is OmniRoute's session-sticky worker combo. Add named choices in `AGENTRT_LLM_PROFILES` as comma-separated `name=model` pairs:

```dotenv
AGENTRT_LLM_PROFILES=audit=agentrt-audit,sol-xhigh=agentrt-sol-xhigh,sol-high=agentrt-sol-high
```

Then have the orchestrator dispatch an audit session with `llm_profile="audit"` (or `llm_profile="sol-xhigh"` / `llm_profile="sol-high"`). The session keeps that model for its whole life, including forks; selection is per session, not per call, preserving prompt-cache stickiness. AgentRT tags the session `llm=<name>` for counting.

## Apply configuration changes

After **every `.env` change**, first pull the intended source revision, then reinstall the runtime snapshot:

```bash
git pull --ff-only
uv tool install --reinstall packages/agentrt-runtime
```

Restart the daemon only when no sessions are running (`agentrt daemon stop` then `agentrt daemon start`). The installed runtime is a snapshot, and a restart is required for the daemon to load it.

## Streaming, timeouts, and provenance

Streaming is enabled by default for non-local endpoints. Keep it enabled when reaching OmniRoute through Cloudflare: Cloudflare can close a response that sends no bytes for 120 seconds, while a long reasoning turn may send nothing until completion without streaming. `AGENTRT_LLM_STREAM=1` forces streaming; `AGENTRT_LLM_STREAM=0` disables it. Local/LAN endpoints default to streaming off.

OmniRoute may wait silently for 90 seconds before cutting an upstream attempt and trying the next route. Stream keepalive chunks can keep an idle timer alive without meaningful progress, so AgentRT bounds a streamed call by a total deadline and retries after timeout. A few failed 90-second attempts can exceed AgentRT's 300-second per-call timeout; do not treat keepalives as evidence that model work is progressing.

To see which upstream actually answered each call, run `agentrt usage <session>` and inspect each usage entry's `provenance.router` fields. They are populated from router response headers and may be absent when the router provides none.
