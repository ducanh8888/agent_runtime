"""Deployment-only LLM policy enforcement (H0).

AgentRT starts the agent server with an explicit policy so that every
conversation LLM is direct DeepSeek Chat Completions, model ``deepseek-flash``,
thinking enabled and ``reasoning_effort`` ``high``. The policy is opt-in: when
``Config.deployment_llm_policy`` is ``None`` the server keeps its generic
behavior and callers may choose any LLM/profile.

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


#: The API model name the deployment requires. Provider-routing prefixes
#: (``openai/``, ``deepseek/``, ``litellm_proxy/``, ...) are transport-internal,
#: so validation compares the trailing path segment.
DIRECT_MODEL = "deepseek-flash"
#: The direct DeepSeek endpoint. Paths such as ``/v1`` are still the same host.
DIRECT_BASE_URL = "https://api.deepseek.com"
#: Requirements stated once so an error can point at the whole contract.
_POLICY_SUMMARY = (
    "this deployment requires direct DeepSeek Chat Completions, model "
    f"{DIRECT_MODEL!r}, thinking enabled and reasoning_effort 'high'"
)


class DeploymentPolicyError(ValueError):
    """A conversation or auxiliary LLM violates the deployment LLM policy."""


class DeploymentLLMPolicy(BaseModel):
    """Frozen deployment-only LLM contract.

    Absent (``None`` on ``Config``) means generic behavior. Every field has a
    default so the runtime can enable the contract with a single explicit value
    and tests can construct partial policies.
    """

    model_config: ClassVar[ConfigDict] = {"frozen": True, "extra": "forbid"}

    model: str = DIRECT_MODEL
    base_url: str = DIRECT_BASE_URL
    api_mode: str = "chat"
    thinking_mode: str = "enabled"
    reasoning_effort: str = "high"


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
    if "reasoning_effort" in extra and extra["reasoning_effort"] != (
        policy.reasoning_effort
    ):
        conflicts.append(
            f"litellm_extra_body.reasoning_effort must be {policy.reasoning_effort!r}"
        )
    if "thinking" in extra:
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
    if actual_endpoint is None:
        violations.append(
            f"base_url must be {_display_endpoint(policy.base_url)} (got <unset>)"
        )
    elif actual_endpoint[0] != "https":
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

    try:
        features = llm._model_features()
    except Exception:  # pragma: no cover - capability lookup is defensive
        violations.append("could not resolve model capabilities")
        return violations

    if features.thinking_mode != policy.thinking_mode:
        violations.append(f"thinking must be {policy.thinking_mode!r}")
    if not features.supports_reasoning_effort:
        violations.append("model must support reasoning_effort")
    if llm.reasoning_effort != policy.reasoning_effort:
        violations.append(f"reasoning_effort must be {policy.reasoning_effort!r}")
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
            + f". For reference, {_POLICY_SUMMARY}."
        )
