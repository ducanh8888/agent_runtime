"""The AgentRT runtime enables the H0 deployment policy for its own server.

An agent-server started any other way must stay generic; only the runtime's
spawned daemon injects the explicit contract. These tests pin the emitted env
payload without launching a process or touching real credentials.
"""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from agentrt.agent_server.deployment_policy import DeploymentLLMPolicy


# ``agentrt-runtime`` is not on pyright's extraPaths, so resolve the module
# dynamically to keep this file type-clean.
daemon = cast(Any, pytest.importorskip("agentrt.runtime.daemon"))


def test_daemon_env_sets_explicit_deployment_policy() -> None:
    env = daemon._daemon_env("test-token")
    raw = env["AGENTRT_DEPLOYMENT_LLM_POLICY"]
    policy = DeploymentLLMPolicy.model_validate(json.loads(raw))
    assert policy.model == "deepseek-flash"
    assert policy.base_url == "https://api.deepseek.com"
    assert policy.api_mode == "chat"
    assert policy.thinking_mode == "enabled"
    assert policy.reasoning_effort == "high"
    assert policy == DeploymentLLMPolicy()


def test_daemon_env_policy_is_secret_free() -> None:
    env = daemon._daemon_env("test-token")
    payload = env["AGENTRT_DEPLOYMENT_LLM_POLICY"]
    assert "key" not in payload.lower()
    assert "token" not in payload.lower()
    assert "test-token" not in payload
