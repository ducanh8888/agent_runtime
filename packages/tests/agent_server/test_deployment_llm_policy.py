"""Unit tests for the deployment-only LLM policy contract (H0; router-neutral
since the OmniRoute migration).

The policy is opt-in on ``Config.deployment_llm_policy``, and its ``model``/
``base_url`` are supplied per instance rather than defaulted (see ``_policy``
below) -- these tests exercise the mechanism generically, using a DeepSeek-
shaped contract as example data: direct https, model ``deepseek-flash``, Chat
Completions, thinking enabled and ``reasoning_effort`` high, with no switch_llm
bypass and no ``litellm_extra_body`` override that wins at serialization.
"""

from __future__ import annotations

import pytest

from agentrt.agent_server.deployment_policy import (
    DeploymentLLMPolicy,
    DeploymentPolicyError,
    enforce_agent_policy,
    llm_policy_violations,
    llm_satisfies_policy,
    switch_llm_enabled,
)
from agentrt.sdk import Agent
from agentrt.sdk.context.condenser import LLMSummarizingCondenser
from agentrt.sdk.llm import LLM
from agentrt.sdk.settings.model import OpenHandsAgentSettings


DIRECT_MODEL = "openai/deepseek-flash"
DIRECT_ENDPOINT = "https://api.deepseek.com"
_CAPABILITY_OVERRIDES = {
    "thinking_mode": "enabled",
    "supports_reasoning_effort": True,
    "supports_responses_api": False,
}


def _policy(**overrides: object) -> DeploymentLLMPolicy:
    """The exact DeepSeek-shaped contract these tests use as example data.

    ``model``/``base_url`` have no class default any more (router-neutral
    policy), so every call site states them explicitly.
    """
    values: dict[str, object] = {
        "model": "deepseek-flash",
        "base_url": DIRECT_ENDPOINT,
        "thinking_mode": "enabled",
        "reasoning_effort": "high",
    }
    values.update(overrides)
    return DeploymentLLMPolicy(**values)  # type: ignore[arg-type]


def _direct_llm(**overrides: object) -> LLM:
    """A DeepSeek-flash LLM that satisfies the default policy."""
    values: dict[str, object] = {
        "model": DIRECT_MODEL,
        "base_url": DIRECT_ENDPOINT,
        "api_mode": "chat",
        "reasoning_effort": "high",
        "capability_overrides": dict(_CAPABILITY_OVERRIDES),
    }
    values.update(overrides)
    return LLM(**values)  # type: ignore[arg-type]


def _agent(llm: LLM, *, include_default_tools: list[str] | None = None) -> Agent:
    return Agent(
        llm=llm,
        tools=[],
        include_default_tools=include_default_tools or ["FinishTool", "ThinkTool"],
    )


@pytest.mark.parametrize(
    "model",
    ["openai/deepseek-flash", "deepseek/deepseek-flash", "deepseek-flash"],
)
def test_direct_https_flash_passes(model: str) -> None:
    llm = _direct_llm(model=model)
    assert llm_satisfies_policy(llm, _policy())
    enforce_agent_policy(_agent(llm), _policy())


def test_http_endpoint_is_rejected() -> None:
    """A plain-http endpoint is not the direct provider even on the same host."""
    llm = _direct_llm(base_url="http://api.deepseek.com")
    violations = llm_policy_violations(llm, _policy())
    assert any("https" in v for v in violations), violations


def test_wrong_endpoint_error_is_sanitized() -> None:
    """Userinfo/query credentials must never appear in a rejection message."""
    secret = "sk-do-not-leak-7f3a"
    llm = _direct_llm(base_url=f"https://user:{secret}@evil.example/v1?token={secret}")
    violations = llm_policy_violations(llm, _policy())
    assert violations, "off-policy endpoint must be rejected"
    joined = "; ".join(violations)
    assert secret not in joined
    assert "evil.example" in joined
    assert "user:" not in joined


def test_model_rejection_never_echoes_hostile_value() -> None:
    """The free-form caller-controlled model stays out of policy errors."""
    secret = "sk-do-not-leak-7f3a"
    llm = _direct_llm(model=f"openai/{secret}")
    violations = llm_policy_violations(llm, _policy())
    assert violations
    assert all(secret not in violation for violation in violations)


def test_missing_endpoint_is_rejected() -> None:
    violations = llm_policy_violations(_direct_llm(base_url=None), _policy())
    assert any("base_url" in v for v in violations), violations


@pytest.mark.parametrize(
    ("extra_body", "needle"),
    [
        ({"reasoning_effort": "low"}, "reasoning_effort"),
        ({"thinking": {"type": "disabled"}}, "thinking"),
    ],
)
def test_extra_body_override_is_rejected(
    extra_body: dict[str, object], needle: str
) -> None:
    """An explicit extra_body wins on the wire, so it must be checked too."""
    llm = _direct_llm(litellm_extra_body=extra_body)
    violations = llm_policy_violations(llm, _policy())
    assert any(needle in v and "litellm_extra_body" in v for v in violations), (
        violations
    )


def test_extra_body_rejection_never_echoes_hostile_values() -> None:
    """Caller-supplied extra_body values must not reach the rejection message."""
    secret = "sk-do-not-leak-7f3a"
    for extra_body in (
        {"reasoning_effort": secret},
        {"thinking": {"type": secret}},
        {"thinking": secret},
    ):
        violations = llm_policy_violations(
            _direct_llm(litellm_extra_body=extra_body), _policy()
        )
        assert violations, extra_body
        assert all(secret not in v for v in violations), violations


def test_matching_extra_body_controls_are_allowed() -> None:
    llm = _direct_llm(
        litellm_extra_body={
            "thinking": {"type": "enabled"},
            "reasoning_effort": "high",
        }
    )
    assert llm_satisfies_policy(llm, _policy())


@pytest.mark.parametrize(
    ("api_mode", "thinking", "effort", "model"),
    [
        ("responses", "enabled", "high", DIRECT_MODEL),
        ("chat", "none", "high", DIRECT_MODEL),
        ("chat", "enabled", "low", DIRECT_MODEL),
        ("chat", "enabled", "high", "openai/gpt-4o"),
    ],
)
def test_each_contract_axis_is_enforced(
    api_mode: str, thinking: str, effort: str, model: str
) -> None:
    llm = _direct_llm(
        api_mode=api_mode,
        model=model,
        reasoning_effort=effort,
        capability_overrides={
            **_CAPABILITY_OVERRIDES,
            "thinking_mode": thinking,
        },
    )
    assert llm_policy_violations(llm, _policy())


def test_switch_llm_default_tool_is_refused() -> None:
    agent = _agent(
        _direct_llm(),
        include_default_tools=["FinishTool", "ThinkTool", "SwitchLLMTool"],
    )
    assert switch_llm_enabled(agent)
    with pytest.raises(DeploymentPolicyError, match="switch_llm"):
        enforce_agent_policy(agent, _policy())


def test_enforcement_reports_all_problems_in_one_message() -> None:
    weak = LLM(model="openai/gpt-4o", reasoning_effort="low")
    agent = _agent(weak, include_default_tools=["FinishTool", "SwitchLLMTool"])
    with pytest.raises(DeploymentPolicyError) as excinfo:
        enforce_agent_policy(agent, _policy())
    message = str(excinfo.value)
    assert "model must be" in message
    assert "api_mode" in message
    assert "switch_llm" in message


def test_settings_built_condenser_inherits_worker_llm() -> None:
    """The settings condenser is a model_copy of the worker, so it cannot be a
    weaker independent profile."""
    settings = OpenHandsAgentSettings(
        llm=_direct_llm(), tools=[], enable_switch_llm_tool=False
    )
    agent = settings.create_agent()
    condenser = agent.condenser
    assert isinstance(condenser, LLMSummarizingCondenser)
    assert condenser.llm is not agent.llm
    assert condenser.llm.model == agent.llm.model
    assert condenser.llm.base_url == agent.llm.base_url
    assert condenser.llm.reasoning_effort == agent.llm.reasoning_effort
    assert condenser.llm.usage_id == "condenser"
    enforce_agent_policy(agent, _policy())


def test_weak_auxiliary_llm_is_rejected() -> None:
    """A hand-built agent with a weaker condenser LLM fails enforcement."""
    weak_condenser = LLMSummarizingCondenser(
        llm=LLM(model="openai/gpt-4o", reasoning_effort="low"),
        max_size=20,
    )
    agent = Agent(
        llm=_direct_llm(),
        tools=[],
        include_default_tools=["FinishTool"],
        condenser=weak_condenser,
    )
    with pytest.raises(DeploymentPolicyError, match="auxiliary LLM"):
        enforce_agent_policy(agent, _policy())
