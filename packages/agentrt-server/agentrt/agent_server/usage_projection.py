"""Redacted projection of the conversation's LLM usage records.

The SDK owns the usage records: ``ConversationStats`` holds one ``Metrics``
per ``usage_id`` (the service slot: worker, condenser, title, ...) and each
``Metrics`` keeps the per-call ``TokenUsage`` records. This module only
reshapes that existing owner's data for the REST info surface. It stores
nothing, sums nothing across conversations, and never reads a field the stats
owner does not already record.

Two things this projection deliberately does *not* do:

* It does not invent provider-confirmed facts. The stats owner records the
  configured and sent routes/policies, but confirmation stays ``unknown``
  until an explicit provider signal exists. Reasoning-token presence is not
  treated as confirmation.
* It does not emit unbounded free text. Endpoint provenance has already been
  reduced to origin/path by the SDK; no userinfo, query, fragment, API key,
  headers, prompts, completions or private reasoning can enter this view.
"""

from __future__ import annotations

from typing import Final, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from agentrt.sdk.conversation.conversation_stats import ConversationStats
from agentrt.sdk.llm.utils.metrics import Metrics, TokenUsage
from agentrt.sdk.llm.utils.provenance import CallProvenance


#: Longest identifier copied verbatim onto the wire. ``usage_id`` is supplied
#: by the caller, so bound it the way the telemetry sanitizer bounds tokens.
_MAX_IDENTIFIER_LENGTH: Final[int] = 128

ModelSource = Literal["stats", "unavailable"]
CallIdSource = Literal["provider_response_id", "ordinal"]


class RawTokenUsage(BaseModel):
    """Provider-reported token counts, copied verbatim from the stats owner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prompt_tokens: int
    completion_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int
    reasoning_tokens: int
    context_window: int
    per_turn_token: int


class NormalizedTokenUsage(BaseModel):
    """Derived view of :class:`RawTokenUsage`, returned alongside it.

    Only ``input_tokens`` is derived: providers that nest cache reads inside
    ``prompt_tokens`` (litellm/OpenAI) have them subtracted, while providers
    that report cache separately (ACP, where ``cache_read_tokens`` exceeds
    ``prompt_tokens``) already exclude them. ``output_tokens`` and
    ``reasoning_tokens`` are copies — the projection does not decide whether
    reasoning is folded into the completion count.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    input_tokens: int
    cache_read_tokens: int
    output_tokens: int
    reasoning_tokens: int


class UsageCall(BaseModel):
    """One provider completion call recorded by the stats owner."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str = Field(
        description=(
            "Stable per-call identifier: the provider response id when the "
            "stats owner recorded one, otherwise ``<usage_id>:<ordinal>``."
        )
    )
    call_id_source: CallIdSource
    model: str | None = Field(
        default=None,
        description="Model the stats owner recorded for this call, if any.",
    )
    provenance: CallProvenance | None = Field(
        default=None,
        description=(
            "Sanitized configured/sent/provider-confirmation provenance, or "
            "null for legacy records and calls without captured provenance."
        ),
    )
    first_token_latency_ms: float | None = Field(
        default=None,
        description=(
            "Milliseconds from request start to the first user-visible content "
            "token, or null when the call did not stream or the stats owner "
            "recorded nothing. Null means unknown, not zero."
        ),
    )
    first_reasoning_token_latency_ms: float | None = Field(
        default=None,
        description=(
            "Milliseconds from request start to the first reasoning token. Kept "
            "apart from the visible one: a call that thinks for a long time and "
            "then writes quickly is not the same as one that waits to start."
        ),
    )
    usage: RawTokenUsage


class UsageService(BaseModel):
    """Per-service (``usage_id``) usage plus its per-call records."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    usage_id: str = Field(
        description="Service slot the SDK registry assigned, e.g. ``agent``."
    )
    model: str | None = None
    model_source: ModelSource
    accumulated_cost: float = Field(ge=0.0)
    accumulated_token_usage: RawTokenUsage
    normalized: NormalizedTokenUsage
    cache_hit_rate: float | None = Field(
        default=None,
        description=(
            "The SDK's own cache-hit fraction, or null when there is no "
            "denominator. Normalized by the stats owner, not by this "
            "projection."
        ),
    )
    calls: list[UsageCall] = Field(default_factory=list)


class ConversationUsage(BaseModel):
    """Redacted usage provenance for one conversation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    conversation_id: UUID
    services: list[UsageService] = Field(default_factory=list)


def _safe_identifier(value: str, fallback: str) -> str:
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()
    if not cleaned:
        return fallback
    return cleaned[:_MAX_IDENTIFIER_LENGTH]


def _raw_usage(usage: TokenUsage) -> RawTokenUsage:
    return RawTokenUsage(
        prompt_tokens=usage.prompt_tokens,
        completion_tokens=usage.completion_tokens,
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
        reasoning_tokens=usage.reasoning_tokens,
        context_window=usage.context_window,
        per_turn_token=usage.per_turn_token,
    )


def _normalize(raw: RawTokenUsage) -> NormalizedTokenUsage:
    # Mirror MetricsSnapshot.cache_hit_rate's documented denominator rule.
    cached = raw.cache_read_tokens
    if cached <= raw.prompt_tokens:
        input_tokens = raw.prompt_tokens - cached
    else:
        input_tokens = raw.prompt_tokens
    return NormalizedTokenUsage(
        input_tokens=max(0, input_tokens),
        cache_read_tokens=cached,
        output_tokens=raw.completion_tokens,
        reasoning_tokens=raw.reasoning_tokens,
    )


def _project_call(
    usage_id: str,
    index: int,
    usage: TokenUsage,
    first_tokens: dict[str, dict[bool, float]],
) -> UsageCall:
    response_id = usage.response_id.strip()
    if response_id:
        call_id, call_id_source = response_id, "provider_response_id"
    else:
        call_id, call_id_source = f"{usage_id}:{index}", "ordinal"
    seen = first_tokens.get(response_id or "", {})
    return UsageCall(
        call_id=_safe_identifier(call_id, f"{usage_id}:{index}"),
        call_id_source=call_id_source,
        model=usage.model or None,
        provenance=usage.provenance,
        usage=_raw_usage(usage),
        first_token_latency_ms=(
            None if seen.get(False) is None else round(seen[False] * 1000, 3)
        ),
        first_reasoning_token_latency_ms=(
            None if seen.get(True) is None else round(seen[True] * 1000, 3)
        ),
    )


def _first_token_seconds(metrics: Metrics) -> dict[str, dict[bool, float]]:
    """First-token timings by response id: ``{id: {reasoning: seconds}}``.

    Absent on records written before the field existed, which reads as unknown
    rather than as zero.
    """
    out: dict[str, dict[bool, float]] = {}
    for entry in getattr(metrics, "first_token_latencies", None) or []:
        if entry.response_id:
            out.setdefault(entry.response_id, {})[entry.reasoning] = entry.latency
    return out


def _project_service(usage_id: str, metrics: Metrics) -> UsageService:
    snapshot = metrics.get_snapshot()
    accumulated = snapshot.accumulated_token_usage or TokenUsage()
    model = accumulated.model or snapshot.model_name or None
    raw = _raw_usage(accumulated)
    first_tokens = _first_token_seconds(metrics)
    return UsageService(
        usage_id=_safe_identifier(usage_id, "unknown"),
        model=model,
        model_source="stats" if model else "unavailable",
        accumulated_cost=snapshot.accumulated_cost,
        accumulated_token_usage=raw,
        normalized=_normalize(raw),
        cache_hit_rate=snapshot.cache_hit_rate,
        calls=[
            _project_call(usage_id, index, call, first_tokens)
            for index, call in enumerate(metrics.token_usages)
        ],
    )


def project_conversation_usage(
    conversation_id: UUID, stats: ConversationStats
) -> ConversationUsage:
    """Project the stats owner's records into a redacted, additive view."""
    services = [
        _project_service(usage_id, metrics)
        for usage_id, metrics in sorted(stats.usage_to_metrics.items())
    ]
    return ConversationUsage(conversation_id=conversation_id, services=services)
