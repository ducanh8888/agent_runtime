"""The AgentRT runtime enables the deployment LLM policy for its own server,
derived from the operator's resolved router config since the OmniRoute
migration (formerly a fixed direct-DeepSeek literal; see H0 in
docs/plans/deepseek-hardening.md and docs/plans/omniroute-migration.md).

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


_ROUTER_SETTINGS = (
    "AGENTRT_API_KEY",
    "AGENTRT_BASE_URL",
    "AGENTRT_DEFAULT_MODEL",
    "AGENTRT_9ROUTER_API_KEY",
    "AGENTRT_9ROUTER_BASE_URL",
)


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    """`_daemon_env` now also provisions `AGENTRT_SECRET_KEY` (H10 item 2),
    generating and persisting one under `config.state_dir()` on first use.
    Without this, every test below would read and write the real
    `~/.agentrt/secret.key` instead of a throwaway one.

    Router settings are cleared too, so the resolvability of
    `AGENTRT_DEPLOYMENT_LLM_POLICY` is controlled per test rather than
    leaking whatever a real operator `.env`/shell happens to export.
    """
    monkeypatch.setenv("AGENTRT_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("AGENTRT_SECRET_KEY", raising=False)
    for name in _ROUTER_SETTINGS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def router_config(monkeypatch):
    """A resolvable, OmniRoute-shaped router config: one opaque virtual
    model behind a local router, not a direct provider endpoint."""
    monkeypatch.setenv("AGENTRT_API_KEY", "test-omniroute-key")
    monkeypatch.setenv("AGENTRT_BASE_URL", "http://127.0.0.1:20128/v1")
    monkeypatch.setenv("AGENTRT_DEFAULT_MODEL", "agentrt-worker")


def test_daemon_env_sets_router_derived_deployment_policy(router_config) -> None:
    """The policy is derived from the runtime's own resolved router config,
    not a literal -- so it reflects whatever AGENTRT_BASE_URL/
    AGENTRT_DEFAULT_MODEL actually say, including a local router's opaque
    virtual model. No thinking_mode/reasoning_effort: this runtime cannot
    honestly assert a fixed reasoning contract for a backend a router
    selects per request. docs/plans/omniroute-migration.md.
    """
    env = daemon._daemon_env("test-token")
    raw = env["AGENTRT_DEPLOYMENT_LLM_POLICY"]
    policy = DeploymentLLMPolicy.model_validate(json.loads(raw))
    assert policy.model == "agentrt-worker"
    assert policy.base_url == "http://127.0.0.1:20128/v1"
    assert policy.api_mode == "chat"
    assert policy.thinking_mode is None
    assert policy.reasoning_effort is None


def test_daemon_env_omits_the_policy_when_router_config_is_unresolvable() -> None:
    """A bare `agentrt daemon start` before `.env` is written must still
    start the daemon -- matching DeploymentLLMPolicy's own documented
    opt-in behavior (server runs generic when the field is absent), not a
    new failure mode introduced by deriving the policy from config."""
    env = daemon._daemon_env("test-token")
    assert "AGENTRT_DEPLOYMENT_LLM_POLICY" not in env


def test_daemon_env_policy_is_secret_free(router_config) -> None:
    env = daemon._daemon_env("test-token")
    payload = env["AGENTRT_DEPLOYMENT_LLM_POLICY"]
    assert "key" not in payload.lower()
    assert "token" not in payload.lower()
    assert "test-token" not in payload
    assert "test-omniroute-key" not in payload


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


def test_daemon_env_raises_the_vendored_run_pool_cap(monkeypatch) -> None:
    """The vendored server's own run-pool cap (AGENTRT_MAX_CONCURRENT_RUNS)
    is a second, independent limit from AGENTRT_MAX_SESSIONS -- one is this
    client refusing to dispatch, the other is the daemon refusing to *run* a
    step on an already-created session. It defaults to 10 there; without
    this, a fan-out beyond that queues on the daemon side even after
    AGENTRT_MAX_SESSIONS is raised."""
    monkeypatch.delenv("AGENTRT_MAX_CONCURRENT_RUNS", raising=False)
    env = daemon._daemon_env("test-token")
    assert int(env["AGENTRT_MAX_CONCURRENT_RUNS"]) > 1000


def test_daemon_env_does_not_override_an_operators_own_run_pool_cap(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AGENTRT_MAX_CONCURRENT_RUNS", "3")
    env = daemon._daemon_env("test-token")
    assert env["AGENTRT_MAX_CONCURRENT_RUNS"] == "3"
