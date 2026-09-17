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


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """`_daemon_env` now also provisions `AGENTRT_SECRET_KEY` (H10 item 2),
    generating and persisting one under `config.state_dir()` on first use.
    Without this, every test below would read and write the real
    `~/.agentrt/secret.key` instead of a throwaway one."""
    monkeypatch.setenv("AGENTRT_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AGENTRT_SECRET_KEY", raising=False)


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


def test_daemon_env_gates_openhands_ceremony_off_by_default(monkeypatch) -> None:
    """No AgentRT permission preset can reach the builtin sub-agents or
    VSCode, so the daemon's own environment defaults both off.
    docs/plans/deepseek-hardening.md H9 (OpenHands ceremony), 2026-09-17.
    """
    monkeypatch.delenv("AGENTRT_REGISTER_BUILTIN_SUBAGENTS", raising=False)
    monkeypatch.delenv("AGENTRT_ENABLE_VSCODE", raising=False)
    env = daemon._daemon_env("test-token")
    assert env["AGENTRT_REGISTER_BUILTIN_SUBAGENTS"] == "0"
    assert env["AGENTRT_ENABLE_VSCODE"] == "0"


def test_daemon_env_does_not_override_an_operator_s_own_choice(monkeypatch) -> None:
    """An operator who explicitly restored either var is not silently
    overridden by the daemon's own default."""
    monkeypatch.setenv("AGENTRT_REGISTER_BUILTIN_SUBAGENTS", "1")
    monkeypatch.setenv("AGENTRT_ENABLE_VSCODE", "1")
    env = daemon._daemon_env("test-token")
    assert env["AGENTRT_REGISTER_BUILTIN_SUBAGENTS"] == "1"
    assert env["AGENTRT_ENABLE_VSCODE"] == "1"


def test_daemon_env_provisions_a_stable_secret_key() -> None:
    """H10 item 2: without a cipher key, the vendored server redacts a
    conversation's persisted secrets (an LLM's API key among them) on save --
    silently, with only a log line. A fresh dispatch never notices; a fork
    always round-trips through exactly that save/reload before it can run,
    so it always lost its credential this way. Reproduced live against a
    real daemon before this fix: a fork of a session that had just run three
    successful tool calls failed its first step with a provider credential
    error, at iterations_used: 0.
    """
    first = daemon._daemon_env("test-token")["AGENTRT_SECRET_KEY"]
    assert first
    # Stable across restarts, unlike the per-start session token: it
    # decrypts what earlier processes encrypted, so a new value on every
    # start would make already-persisted secrets unreadable rather than
    # merely absent.
    second = daemon._daemon_env("other-token")["AGENTRT_SECRET_KEY"]
    assert second == first


def test_daemon_env_does_not_override_an_operators_own_secret_key(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AGENTRT_SECRET_KEY", "operator-chosen-key")
    env = daemon._daemon_env("test-token")
    assert env["AGENTRT_SECRET_KEY"] == "operator-chosen-key"
