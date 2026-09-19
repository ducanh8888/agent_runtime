"""Deployment-only LLM policy enforcement (H0; router-neutral since the
OmniRoute migration).

AgentRT starts the agent server with an explicit policy so that every
conversation LLM is the one configured model and endpoint -- originally direct
DeepSeek Chat Completions with thinking forced on; now, behind a router such as
OmniRoute, one opaque virtual model whose real backend/provider/reasoning
support the router alone decides. The policy is opt-in: when
``Config.deployment_llm_policy`` is ``None`` the server keeps its generic
behavior and callers may choose any LLM/profile.

``model`` and ``base_url`` are still mandatory on every policy instance --
the invariant that never changed is "no request goes anywhere but the one
configured endpoint, silently." ``thinking_mode`` and ``reasoning_effort`` are
now optional (``None`` means "not enforced"): a virtual model whose backend
the router selects per-request cannot honestly be declared to always think, or
to always support a specific effort level, so a router-neutral policy simply
does not assert either. A deployment that still wants that exact contract
(e.g. because it points straight at one reasoning-capable provider without a
router in front) may still set both explicitly.

Scope and boundaries:

* Enforced on **new** conversations only, after every creation form has been
  resolved (``agent_profile_id``, explicit ``agent``, ``agent_settings``).
  Persisted/legacy sessions are never rejected or retargeted on load.
* The worker LLM and any condenser LLM the agent will actually call must
  satisfy the contract; the ``switch_llm`` tool is refused so a worker cannot
  bypass the allowed profile at runtime.
* Error text names the offending field and the requirement. It never includes
  an API key, credential, prompt or completion content.

The policy model is deliberately small and frozen. It pins the transport the
runtime already writes into its saved profile (see
``agentrt.runtime.bootstrap``); the server, not the SDK, owns enforcement so a
library consumer keeps full provider freedom.
"""

from __future__ import annotations

from typing import ClassVar
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict

from agentrt.sdk.agent.base import AgentBase
from agentrt.sdk.llm import LLM


class DeploymentPolicyError(ValueError):
    """A conversation or auxiliary LLM violates the deployment LLM policy."""


class DeploymentLLMPolicy(BaseModel):
    """Frozen deployment-only LLM contract.

    Absent (``None`` on ``Config``) means generic behavior. ``model`` and
    ``base_url`` have no default: there is no meaningful universal choice, so
    every caller states explicitly which endpoint this deployment's one
    contract points at (``agentrt.runtime.daemon`` derives both from the
    runtime's own resolved router configuration, not a literal). Every other
    field has a default so a caller enabling the contract can supply just the
    two mandatory ones, and tests can construct partial policies.
    """

    model_config: ClassVar[ConfigDict] = {"frozen": True, "extra": "forbid"}

    model: str
    base_url: str
    api_mode: str = "chat"
    thinking_mode: str | None = None
    reasoning_effort: str | None = None


def _policy_summary(policy: DeploymentLLMPolicy) -> str:
    """State one policy instance's requirements, for an error message.

    Built per instance rather than once at import time: ``model``/``base_url``
    are no longer a fixed literal, and ``thinking_mode``/``reasoning_effort``
    may or may not be enforced at all.
    """
    parts = [
        f"model {policy.model!r} at {policy.base_url!r}",
        f"api_mode {policy.api_mode!r}",
    ]
    if policy.thinking_mode is not None:
        parts.append(f"thinking {policy.thinking_mode!r}")
    if policy.reasoning_effort is not None:
        parts.append(f"reasoning_effort {policy.reasoning_effort!r}")
    return "this deployment requires " + ", ".join(parts)


def api_model_name(model: str) -> str:
    """Return the API model name, dropping any provider-routing prefix."""
    return model.rsplit("/", 1)[-1]


def _normalized_endpoint(url: str | None) -> tuple[str, str, int | None] | None:
    """``(scheme, host, port)`` for ``url``, or ``None`` when absent/unparseable.

    Default ports (443/https, 80/http) collapse to ``None`` so an explicit
    ``:443`` still equals the bare host. Userinfo, path, query and fragment are
    intentionally dropped: only the endpoint identity is compared, never
    credentials.
    """
    if not url:
        return None
    raw = url if "://" in url else f"//{url}"
    try:
        split = urlsplit(raw)
        host = (split.hostname or "").lower()
        port = split.port
    except ValueError:
        return None
    if not host:
        return None
    scheme = split.scheme.lower()
    if (scheme, port) in {("https", 443), ("http", 80)}:
        port = None
    return scheme, host, port


def _display_endpoint(url: str | None) -> str:
    """Secret-free rendering of an endpoint for error messages.

    Strips userinfo, path, query and fragment so a credential embedded in a
    base URL can never appear in a rejection message.
    """
    endpoint = _normalized_endpoint(url)
    if endpoint is None:
        return "<unset>"
    scheme, host, port = endpoint
    suffix = f":{port}" if port else ""
    return f"{scheme}://{host}{suffix}" if scheme else f"{host}{suffix}"


def _extra_body_conflicts(llm: LLM, policy: DeploymentLLMPolicy) -> list[str]:
    """Conflicts in ``litellm_extra_body`` that override the model-derived
    reasoning controls at final serialization.

    Only checked when the policy itself enforces ``reasoning_effort``/
    ``thinking_mode`` -- a router-neutral policy with both ``None`` has no
    reasoning contract for an explicit extra_body to override, so nothing
    here is a conflict.

    The SDK emits ``thinking``/``reasoning_effort`` as extra-body defaults and
    lets an explicit user ``extra_body`` win, so a payload setting thinking
    disabled or ``reasoning_effort`` low would reach the provider even though
    the LLM object advertises the required contract.
    """
    extra = llm.litellm_extra_body or {}
    conflicts: list[str] = []
    # Report the field and the requirement only: an explicit extra_body is
    # caller-supplied and could carry an arbitrary (possibly secret) value, so
    # its actual content must never reach a rejection message.
    if (
        policy.reasoning_effort is not None
        and "reasoning_effort" in extra
        and extra["reasoning_effort"] != policy.reasoning_effort
    ):
        conflicts.append(
            f"litellm_extra_body.reasoning_effort must be {policy.reasoning_effort!r}"
        )
    if policy.thinking_mode is not None and "thinking" in extra:
        thinking = extra["thinking"]
        enabled = isinstance(thinking, dict) and thinking.get("type") == "enabled"
        if not enabled:
            conflicts.append("litellm_extra_body.thinking must be enabled")
    return conflicts


def llm_policy_violations(llm: LLM | None, policy: DeploymentLLMPolicy) -> list[str]:
    """Human-readable reasons ``llm`` fails the contract; empty when it passes.

    Only model/transport/policy fields are named. No key or secret is read, and
    the reported endpoint is sanitized of userinfo/query.
    """
    if llm is None:
        return ["agent must use the deployment LLM"]

    violations: list[str] = []
    actual_model = api_model_name(llm.model)
    if actual_model != policy.model:
        violations.append(f"model must be {policy.model!r}")

    expected_endpoint = _normalized_endpoint(policy.base_url)
    actual_endpoint = _normalized_endpoint(llm.base_url)
    # https is required only when the policy's own endpoint is https --
    # not hardcoded, because a router that AgentRT talks to on this machine
    # (e.g. OmniRoute at http://127.0.0.1:20128/v1) is a legitimate policy
    # endpoint in its own right, not a downgrade of anything.
    requires_https = expected_endpoint is not None and expected_endpoint[0] == "https"
    if actual_endpoint is None:
        violations.append(
            f"base_url must be {_display_endpoint(policy.base_url)} (got <unset>)"
        )
    elif requires_https and actual_endpoint[0] != "https":
        violations.append(
            f"base_url must use https (got {_display_endpoint(llm.base_url)})"
        )
    elif actual_endpoint != expected_endpoint:
        violations.append(
            f"base_url must be {_display_endpoint(policy.base_url)} "
            f"(got {_display_endpoint(llm.base_url)})"
        )

    if llm.api_mode != policy.api_mode:
        violations.append(f"api_mode must be {policy.api_mode!r}")

    if policy.thinking_mode is not None or policy.reasoning_effort is not None:
        try:
            features = llm._model_features()
        except Exception:  # pragma: no cover - capability lookup is defensive
            violations.append("could not resolve model capabilities")
            return violations

        if policy.thinking_mode is not None and features.thinking_mode != (
            policy.thinking_mode
        ):
            violations.append(f"thinking must be {policy.thinking_mode!r}")
        if policy.reasoning_effort is not None:
            if not features.supports_reasoning_effort:
                violations.append("model must support reasoning_effort")
            if llm.reasoning_effort != policy.reasoning_effort:
                violations.append(
                    f"reasoning_effort must be {policy.reasoning_effort!r}"
                )
    violations.extend(_extra_body_conflicts(llm, policy))
    return violations


def llm_satisfies_policy(llm: LLM | None, policy: DeploymentLLMPolicy) -> bool:
    """Whether ``llm`` fully satisfies ``policy``."""
    return not llm_policy_violations(llm, policy)


def switch_llm_enabled(agent: AgentBase) -> bool:
    """Whether ``agent`` exposes the built-in ``switch_llm`` tool.

    ``create_agent()`` adds ``SwitchLLMTool`` by class name to
    ``include_default_tools``; an explicit ``tools`` list can also name it.
    Both are checked so a payload cannot smuggle the tool past enforcement.
    """
    from agentrt.sdk.tool.builtins import SwitchLLMTool

    if SwitchLLMTool.__name__ in agent.include_default_tools:
        return True
    return any(tool.name == "switch_llm" for tool in agent.tools)


def refuse_unsupported_attachments(content, llm) -> None:
    """Refuse an image attachment the worker model cannot see.

    Sending it anyway would strip it silently at the provider, and the caller
    would read a confident answer about an image that never arrived.
    """
    has_image = any(getattr(block, "type", None) == "image" for block in content or ())
    if not has_image:
        return
    if getattr(llm, "vision_is_active", lambda: False)():
        return
    raise DeploymentPolicyError(
        "this session's model has no active vision capability, so an image "
        "attachment would be dropped before the model saw it; remove the "
        "attachment or dispatch under a vision-capable model"
    )


def enforce_agent_policy(agent: AgentBase, policy: DeploymentLLMPolicy) -> None:
    """Raise :class:`DeploymentPolicyError` when ``agent`` breaks ``policy``.

    Validates the worker LLM, every auxiliary LLM the agent holds (the
    settings-built condenser is a ``model_copy`` of the worker, so it is
    normally the same contract), and refuses the ``switch_llm`` bypass tool.
    Raises once with every violation so a caller sees all problems in a single,
    secret-free message.
    """
    problems: list[str] = []
    for violation in llm_policy_violations(agent.llm, policy):
        problems.append(f"agent LLM: {violation}")
    for llm in agent.get_all_llms():
        if llm is agent.llm:
            continue
        for violation in llm_policy_violations(llm, policy):
            problems.append(f"auxiliary LLM ({llm.usage_id}): {violation}")
    if switch_llm_enabled(agent):
        problems.append(
            "switch_llm tool is not permitted (disable enable_switch_llm_tool)"
        )
    if problems:
        raise DeploymentPolicyError(
            "Conversation rejected by the deployment LLM policy: "
            + "; ".join(problems)
            + f". For reference, {_policy_summary(policy)}."
        )
