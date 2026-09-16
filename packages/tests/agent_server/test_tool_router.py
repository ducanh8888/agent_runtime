"""Tests for tool_router module-level initialization."""

import importlib

import pytest

from agentrt.sdk.subagent.registry import (
    _reset_registry_for_tests,
    get_agent_factory,
)


def test_builtin_agents_registered_on_tool_router_import():
    """Importing tool_router should register builtin agents (default, explore, bash).

    The agent-server includes tool_router at startup, so this verifies that
    builtin sub-agents are available as soon as the server starts -- when
    AGENTRT_REGISTER_BUILTIN_SUBAGENTS is unset, matching the gate's own
    default of "on" (the daemon's own deployment turns this off via
    daemon._daemon_env; a bare import of the vendored module does not).
    """
    import agentrt.agent_server.tool_router as mod

    # Reset and reload to simulate a fresh import
    _reset_registry_for_tests()
    importlib.reload(mod)

    for name in ("default", "explore", "bash"):
        factory = get_agent_factory(name)
        assert factory is not None, f"Builtin agent '{name}' not registered"
        assert callable(factory.factory_func)

    _reset_registry_for_tests()


def test_builtin_agents_skipped_when_gated_off(monkeypatch):
    """AGENTRT_REGISTER_BUILTIN_SUBAGENTS=0 skips registration entirely.

    No AgentRT permission preset grants the `delegate` tool these builtin
    sub-agents need, so a deployment that turns this off should see no
    registration attempt at all, not merely an unreachable one.
    docs/plans/deepseek-hardening.md H9 (OpenHands ceremony), 2026-09-17.
    """
    import agentrt.agent_server.tool_router as mod

    monkeypatch.setenv("AGENTRT_REGISTER_BUILTIN_SUBAGENTS", "0")
    _reset_registry_for_tests()
    importlib.reload(mod)

    for name in ("default", "explore", "bash"):
        with pytest.raises(ValueError, match="Unknown agent"):
            get_agent_factory(name)

    _reset_registry_for_tests()
    monkeypatch.delenv("AGENTRT_REGISTER_BUILTIN_SUBAGENTS", raising=False)
    importlib.reload(mod)
    _reset_registry_for_tests()
