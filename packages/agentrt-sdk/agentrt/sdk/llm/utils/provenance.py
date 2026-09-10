"""Sanitized, credential-free provenance for a single LLM provider call.

The SDK already owns per-call usage records (``TokenUsage`` on ``Metrics``,
written by ``Telemetry``). This module defines the optional structured
provenance attached to each of those records so callers can distinguish:

* what was *configured* (profile model, parsed provider, sanitized endpoint,
  configured reasoning policy),
* what was actually *sent* at the final LiteLLM-call boundary
  (``reasoning_effort`` / ``thinking``), and
* whether the provider *confirmed* the effective reasoning policy.

Confirmation starts and stays ``"unknown"``. Token counts — including
reasoning tokens — are never evidence that the provider honored a policy.
Likewise, a missing value is ``None`` (unknown), never coerced to
``False`` or ``0``.

Endpoints are reduced to scheme/host/port + path. Userinfo (``user:pass@``),
query strings, and fragments are dropped by construction; API keys, headers,
prompts, completions, and private reasoning are never accepted here.

A completed provider call whose response carries no usage yields no
per-call ``TokenUsage`` entry (a zero-token record would assert "0 tokens"
rather than "unknown"). Such calls are represented as usage-unavailable rather
than zero, and their provenance is discarded instead of being attributed to a
later call.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field


#: Bound copied identifiers so caller/provider data cannot grow unbounded.
_MAX_COMPONENT_LENGTH = 512
_MAX_POLICY_VALUE_LENGTH = 64

ConfirmationStatus = Literal["confirmed", "unknown"]
CallIdSource = Literal["provider_response_id", "unavailable"]

UNAVAILABLE = "unavailable"


def _clean(value: str, *, limit: int) -> str:
    """Drop non-printable characters and bound the result."""
    cleaned = "".join(ch for ch in value if ch.isprintable()).strip()
    return cleaned[:limit]


def _optional_str(value: Any, *, limit: int = _MAX_POLICY_VALUE_LENGTH) -> str | None:
    if not isinstance(value, str):
        return None
    cleaned = _clean(value, limit=limit)
    return cleaned or None


class EndpointProvenance(BaseModel):
    """Credentials-free endpoint identity: origin plus path.

    ``origin`` is ``scheme://host[:port]`` and ``path`` is the path only. Both
    are ``None`` when the input has no absolute origin. Userinfo, query string
    and fragment are stripped; headers and bodies are out of scope entirely.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: str | None = None
    path: str | None = None

    @classmethod
    def from_url(cls, value: str | None) -> EndpointProvenance | None:
        """Sanitize an endpoint URL, or return ``None`` when unavailable.

        A missing, relative, or schemeless value yields ``None`` rather than a
        guessed origin. This is what keeps an alias from inventing an endpoint.
        """
        if not isinstance(value, str):
            return None
        raw = value.strip()
        if not raw:
            return None
        try:
            parts = urlsplit(raw)
        except ValueError:
            return None

        scheme = (parts.scheme or "").lower()
        netloc = parts.netloc
        # Strip userinfo (user:password@). The last "@" is the real separator.
        if "@" in netloc:
            netloc = netloc.rsplit("@", 1)[1]
        if not scheme or not netloc:
            return None

        origin = _clean(f"{scheme}://{netloc}", limit=_MAX_COMPONENT_LENGTH)
        if not origin:
            return None
        path = _clean(parts.path, limit=_MAX_COMPONENT_LENGTH)
        return cls(origin=origin, path=path or None)


class PolicyProvenance(BaseModel):
    """Reasoning policy fields, reduced to scalars.

    Every field is optional; ``None`` means unknown/not specified.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reasoning_effort: str | None = None
    thinking_type: str | None = None
    thinking_budget_tokens: int | None = None


class RouteProvenance(BaseModel):
    """Model/provider/endpoint/policy for one side of a call (configured|sent)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    model: str | None = None
    provider: str | None = None
    endpoint: EndpointProvenance | None = None
    policy: PolicyProvenance = Field(default_factory=PolicyProvenance)


class ProviderConfirmation(BaseModel):
    """What the provider explicitly confirmed, if anything.

    Defaults to ``"unknown"`` and is never derived from usage token counts.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    reasoning_policy: ConfirmationStatus = "unknown"


class CallProvenance(BaseModel):
    """Per-call provenance recorded alongside a completed provider call."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    call_id: str = UNAVAILABLE
    call_id_source: CallIdSource = UNAVAILABLE
    configured: RouteProvenance = Field(default_factory=RouteProvenance)
    sent: RouteProvenance = Field(default_factory=RouteProvenance)
    confirmation: ProviderConfirmation = Field(default_factory=ProviderConfirmation)


def _thinking_fields(thinking: Any) -> tuple[str | None, int | None]:
    if isinstance(thinking, Mapping):
        thinking_type = _optional_str(thinking.get("type"))
        budget = thinking.get("budget_tokens")
        if isinstance(budget, bool) or not isinstance(budget, int) or budget < 0:
            budget = None
        return thinking_type, budget
    if isinstance(thinking, str):
        return _optional_str(thinking), None
    return None, None


def policy_from_call_kwargs(kwargs: Mapping[str, Any]) -> PolicyProvenance:
    """Extract the sanitized reasoning policy present in final call kwargs.

    ``extra_body`` is merged into the request body last, so an explicit
    ``extra_body`` value overrides the same top-level key at the final HTTP
    boundary (see ``merge_extra_body_defaults``). Precedence here mirrors that
    serialization so a conflicting top-level ``high`` plus ``extra_body=low``
    reports ``low`` — the value actually sent — rather than ``high``.
    """
    extra = kwargs.get("extra_body")
    extra = extra if isinstance(extra, Mapping) else {}

    reasoning = extra.get("reasoning_effort")
    if reasoning is None:
        reasoning = kwargs.get("reasoning_effort")
    if reasoning is None:
        # Responses API nests the control as ``reasoning={"effort": ...}``.
        reasoning_struct = kwargs.get("reasoning")
        if isinstance(reasoning_struct, Mapping):
            reasoning = reasoning_struct.get("effort")

    thinking = extra.get("thinking")
    if thinking is None:
        thinking = kwargs.get("thinking")

    thinking_type, budget = _thinking_fields(thinking)
    return PolicyProvenance(
        reasoning_effort=_optional_str(reasoning),
        thinking_type=thinking_type,
        thinking_budget_tokens=budget,
    )


def normalize_thinking_mode(mode: Any) -> str | None:
    """Map a configured capability marker to the wire thinking type, if any.

    ``adaptive`` and ``unknown`` have no explicit wire flag, so they stay
    ``None`` rather than being reported as disabled.
    """
    if mode in ("enabled", "manual"):
        return "enabled"
    if mode == "none":
        return "disabled"
    return None


def call_identity(response_id: str | None) -> tuple[str, CallIdSource]:
    """Return a per-call id from the provider response id, or explicit status."""
    if isinstance(response_id, str) and response_id.strip():
        return response_id, "provider_response_id"
    return UNAVAILABLE, UNAVAILABLE
