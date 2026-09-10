"""H0: selected-profile preview/apply and secret-free LLM dispatch selection."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from agentrt.agent_server.persistence import (
    get_agent_profile_store,
    get_llm_profile_store,
    reset_stores,
)
from agentrt.runtime import bootstrap, client as client_mod
from agentrt.sdk.profiles.agent_profile import OpenHandsAgentProfile


@pytest.fixture
def state(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    for name in (
        "AGENTRT_API_KEY",
        "AGENTRT_BASE_URL",
        "AGENTRT_DEFAULT_MODEL",
        "AGENTRT_9ROUTER_API_KEY",
        "AGENTRT_9ROUTER_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGENTRT_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTRT_PERSISTENCE_DIR", str(tmp_path))
    monkeypatch.setenv("AGENTRT_API_KEY", "test-key")
    monkeypatch.setenv("AGENTRT_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("AGENTRT_DEFAULT_MODEL", "deepseek-flash")
    monkeypatch.setenv("AGENTRT_MAX_SESSIONS", "0")
    reset_stores()
    yield str(tmp_path)
    reset_stores()


def _digest(path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_new_profiles_are_constrained_and_ids_reused(state) -> None:
    first = bootstrap.ensure_profiles()
    second = bootstrap.ensure_profiles()

    assert first == second
    assert bootstrap.allowed_llm_profiles() == ["default"]
    assert bootstrap.agent_profile_llm_ref("workspace") == "default"
    for preset in ("readonly", "workspace", "broad"):
        profile = get_agent_profile_store().load(preset)
        assert isinstance(profile, OpenHandsAgentProfile)
        assert profile.enable_switch_llm_tool is False


def test_saved_llm_profile_carries_exact_direct_high_contract(state) -> None:
    bootstrap.ensure_profiles()
    llm = get_llm_profile_store().load("default")

    # Assert the serialized profile, so this holds even before the SDK's
    # name-based detection understands the deepseek-flash alias.
    dumped = llm.model_dump()
    assert dumped["model"] == "openai/deepseek-flash"
    assert dumped["usage_id"] == "agent"
    assert dumped["api_mode"] == "chat"
    assert dumped["reasoning_effort"] == "high"
    assert dumped["capability_overrides"]["supports_reasoning_effort"] is True
    assert dumped["capability_overrides"]["thinking_mode"] == "enabled"
    assert dumped["capability_overrides"]["supports_responses_api"] is False
    assert dumped["litellm_extra_body"] == {"thinking": {"type": "enabled"}}
    assert dumped["base_url"] == "https://api.deepseek.com"
    assert llm.api_key is not None
    assert "test-key" not in repr(dumped)


def test_rebuild_preserves_switch_llm_turned_on_by_operator(state) -> None:
    bootstrap.ensure_profiles()
    store = get_agent_profile_store()
    profile = store.load("workspace")
    assert isinstance(profile, OpenHandsAgentProfile)
    store.save(profile.model_copy(update={"enable_switch_llm_tool": True}))

    bootstrap.ensure_profiles()

    reloaded = get_agent_profile_store().load("workspace")
    assert isinstance(reloaded, OpenHandsAgentProfile)
    assert reloaded.enable_switch_llm_tool is True
    assert reloaded.id == profile.id


def test_preview_writes_nothing_and_apply_preserves_unrelated_settings(
    state,
) -> None:
    ids = bootstrap.ensure_profiles()
    profile_path = Path(state) / "profiles" / "default.json"
    before = _digest(profile_path)

    preview = bootstrap.preview_llm_profile()
    assert preview["exists"] is True
    assert _digest(profile_path) == before

    llm_store = get_llm_profile_store()
    llm = llm_store.load("default")
    # A stale, inconsistent policy plus an unrelated field the operator set.
    llm_store.save(
        "default",
        llm.model_copy(
            update={
                "reasoning_effort": "medium",
                "api_mode": "auto",
                "capability_overrides": {},
                "litellm_extra_body": {},
                "usage_id": "custom",
                "temperature": 0.25,
            }
        ),
        include_secrets=True,
    )

    preview = bootstrap.preview_llm_profile()
    assert preview["exists"] is True
    assert "test-key" not in repr(preview)
    changed = {change["field"] for change in preview["changes"]}
    assert {
        "reasoning_effort",
        "api_mode",
        "capability_overrides",
        "litellm_extra_body",
        "usage_id",
    } <= changed

    applied = bootstrap.apply_llm_profile()
    assert applied["applied"] is True
    assert applied["agent_profiles_unchanged"] is True

    reloaded = llm_store.load("default")
    assert reloaded.model_dump()["reasoning_effort"] == "high"
    assert reloaded.api_mode == "chat"
    assert reloaded.capability_overrides["thinking_mode"] == "enabled"
    assert reloaded.litellm_extra_body == {"thinking": {"type": "enabled"}}
    assert reloaded.usage_id == "agent"
    assert reloaded.temperature == 0.25  # unrelated field preserved
    assert reloaded.api_key is not None
    assert bootstrap.preview_llm_profile()["changes"] == []
    assert bootstrap.ensure_profiles() == ids
    if os.name != "nt":
        assert profile_path.stat().st_mode & 0o777 == 0o600


class _StubClient(client_mod.Client):
    """A Client whose HTTP surface records the request instead of sending it."""

    def __init__(self, profiles):
        super().__init__()
        self._profile_ref = profiles
        self._daemon_info = object()
        self.sent: list[dict] = []

    def _send(self, method, path, **kwargs):
        self.sent.append({"method": method, "path": path, **kwargs})
        return httpx.Response(200, json={"id": "00000000-0000-0000-0000-000000000001"})


def test_dispatch_accepts_default_llm_profile_and_refuses_unknown(state) -> None:
    profiles = bootstrap.ensure_profiles()
    client = _StubClient(profiles)

    result = client.dispatch("do it", state, permission="workspace")

    assert result["llm_profile"] == "default"
    assert client.sent[0]["json"]["agent_profile_id"] == str(profiles["workspace"])

    client.sent.clear()
    with pytest.raises(client_mod.ClientError):
        client.dispatch("do it", state, permission="workspace", llm_profile="nope")
    assert client.sent == []


def test_dispatch_refuses_allowed_but_unbound_llm_profile(state) -> None:
    profiles = bootstrap.ensure_profiles()
    get_llm_profile_store().save(
        "other", get_llm_profile_store().load("default"), include_secrets=True
    )
    readonly = get_agent_profile_store().load("readonly")
    get_agent_profile_store().save(
        readonly.model_copy(update={"llm_profile_ref": "other"})
    )
    assert bootstrap.allowed_llm_profiles() == ["default", "other"]

    client = _StubClient(profiles)
    with pytest.raises(client_mod.ClientError, match="bound to LLM profile"):
        client.dispatch("do it", state, permission="workspace", llm_profile="other")
    assert client.sent == []
