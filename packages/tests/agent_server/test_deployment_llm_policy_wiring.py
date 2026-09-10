"""Service-level wiring tests for the H0 deployment LLM policy.

Covers the creation forms (explicit agent, ``agent_settings``, named profile),
policy-off backward compatibility, and the follow-up/retry/restoration paths
that must retain the enforced LLM. Title-profile fallback and explicit-title
preservation are covered too.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from agentrt.agent_server.config import Config
from agentrt.agent_server.conversation_router import (
    _reject_llm_off_policy,
    conversation_router,
)
from agentrt.agent_server.conversation_service import (
    AutoTitleSubscriber,
    ConversationService,
)
from agentrt.agent_server.dependencies import get_conversation_service
from agentrt.agent_server.deployment_policy import (
    DeploymentLLMPolicy,
    DeploymentPolicyError,
)
from agentrt.agent_server.event_service import EventService
from agentrt.agent_server.models import StartConversationRequest
from agentrt.sdk import Agent
from agentrt.sdk.event import MessageEvent
from agentrt.sdk.llm import LLM, Message, TextContent
from agentrt.sdk.profiles.agent_profile import OpenHandsAgentProfile
from agentrt.sdk.security.confirmation_policy import NeverConfirm
from agentrt.sdk.settings.model import OpenHandsAgentSettings
from agentrt.sdk.workspace import LocalWorkspace


_CAPABILITY_OVERRIDES: dict[str, Any] = {
    "thinking_mode": "enabled",
    "supports_reasoning_effort": True,
    "supports_responses_api": False,
}


def _direct_llm(*, stream: bool = False) -> LLM:
    return LLM(
        model="openai/deepseek-flash",
        base_url="https://api.deepseek.com",
        api_mode="chat",
        reasoning_effort="high",
        stream=stream,
        capability_overrides=dict(_CAPABILITY_OVERRIDES),
    )


def _weak_llm() -> LLM:
    return LLM(model="openai/gpt-4o", reasoning_effort="low")


def _agent(llm: LLM, *, include_default_tools: list[str] | None = None) -> Agent:
    return Agent(
        llm=llm,
        tools=[],
        include_default_tools=include_default_tools or ["FinishTool", "ThinkTool"],
    )


def _request(agent: Agent, workspace: Path) -> StartConversationRequest:
    return StartConversationRequest(
        agent=agent,
        workspace=LocalWorkspace(working_dir=str(workspace)),
        confirmation_policy=NeverConfirm(),
    )


@pytest.fixture
def workspace_dir(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return workspace


def _service(tmp_path: Path, *, enforce: bool) -> ConversationService:
    return ConversationService(
        conversations_dir=tmp_path / "conversations",
        deployment_llm_policy=DeploymentLLMPolicy() if enforce else None,
    )


async def _reject(
    service: ConversationService, request: StartConversationRequest
) -> str:
    with pytest.raises(DeploymentPolicyError) as excinfo:
        await service.start_conversation(request)
    return str(excinfo.value)


@pytest.mark.asyncio
async def test_explicit_weak_agent_is_rejected(
    tmp_path: Path, workspace_dir: Path
) -> None:
    async with _service(tmp_path, enforce=True) as service:
        message = await _reject(service, _request(_agent(_weak_llm()), workspace_dir))
    assert "model must be 'deepseek-flash'" in message


@pytest.mark.asyncio
async def test_explicit_agent_with_switch_tool_is_rejected(
    tmp_path: Path, workspace_dir: Path
) -> None:
    agent = _agent(
        _direct_llm(),
        include_default_tools=["FinishTool", "ThinkTool", "SwitchLLMTool"],
    )
    async with _service(tmp_path, enforce=True) as service:
        message = await _reject(service, _request(agent, workspace_dir))
    assert "switch_llm" in message


@pytest.mark.asyncio
async def test_agent_settings_form_is_rejected(
    tmp_path: Path, workspace_dir: Path
) -> None:
    settings = OpenHandsAgentSettings(
        llm=_weak_llm(), tools=[], enable_switch_llm_tool=False
    )
    request = StartConversationRequest(
        agent_settings=settings.model_dump(mode="json"),
        workspace=LocalWorkspace(working_dir=str(workspace_dir)),
        confirmation_policy=NeverConfirm(),
    )
    async with _service(tmp_path, enforce=True) as service:
        message = await _reject(service, request)
    assert "model must be 'deepseek-flash'" in message


@pytest.mark.asyncio
async def test_named_profile_with_weak_llm_is_rejected(
    tmp_path: Path, workspace_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentrt.agent_server.persistence.store import (
        get_agent_profile_store,
        get_llm_profile_store,
    )

    monkeypatch.setattr(
        "agentrt.agent_server.conversation_service.discover_profile_skills",
        lambda: [],
    )
    get_llm_profile_store().save("weak-ref", _weak_llm())
    profile_id = uuid4()
    get_agent_profile_store().save(
        OpenHandsAgentProfile(
            id=profile_id,
            name="weak-profile",
            llm_profile_ref="weak-ref",
            tools=[],
        )
    )
    request = StartConversationRequest(
        agent_profile_id=profile_id,
        workspace=LocalWorkspace(working_dir=str(workspace_dir)),
        confirmation_policy=NeverConfirm(),
    )
    async with _service(tmp_path, enforce=True) as service:
        message = await _reject(service, request)
    assert "model must be 'deepseek-flash'" in message


@pytest.mark.asyncio
async def test_named_profile_with_compliant_llm_starts(
    tmp_path: Path, workspace_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentrt.agent_server.persistence.store import (
        get_agent_profile_store,
        get_llm_profile_store,
    )

    monkeypatch.setattr(
        "agentrt.agent_server.conversation_service.discover_profile_skills",
        lambda: [],
    )
    get_llm_profile_store().save("direct-ref", _direct_llm())
    profile_id = uuid4()
    get_agent_profile_store().save(
        OpenHandsAgentProfile(
            id=profile_id,
            name="direct-profile",
            llm_profile_ref="direct-ref",
            tools=[],
            enable_switch_llm_tool=False,
        )
    )
    request = StartConversationRequest(
        agent_profile_id=profile_id,
        workspace=LocalWorkspace(working_dir=str(workspace_dir)),
        confirmation_policy=NeverConfirm(),
    )
    async with _service(tmp_path, enforce=True) as service:
        info, is_new = await service.start_conversation(request)
    assert is_new
    assert info.agent.llm.model == "openai/deepseek-flash"
    assert info.agent.llm.reasoning_effort == "high"


@pytest.mark.asyncio
async def test_generic_service_without_policy_accepts_weak_llm(
    tmp_path: Path, workspace_dir: Path
) -> None:
    async with _service(tmp_path, enforce=False) as service:
        info, is_new = await service.start_conversation(
            _request(_agent(_weak_llm()), workspace_dir)
        )
    assert is_new
    assert info.agent.llm.model == "openai/gpt-4o"


@pytest.mark.asyncio
async def test_reused_conversation_retains_enforced_llm(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """A follow-up/retry that reuses the id must not swap in a weaker LLM."""
    strong_request = _request(_agent(_direct_llm()), workspace_dir)
    async with _service(tmp_path, enforce=True) as service:
        info, _ = await service.start_conversation(strong_request)
        weak_request = _request(_agent(_weak_llm()), workspace_dir).model_copy(
            update={"conversation_id": info.id}
        )
        reused, is_new = await service.start_conversation(weak_request)
    assert not is_new
    assert reused.id == info.id
    assert reused.agent.llm.model == "openai/deepseek-flash"
    assert reused.agent.llm.reasoning_effort == "high"


@pytest.mark.asyncio
async def test_restored_conversation_is_not_retargeted(
    tmp_path: Path, workspace_dir: Path
) -> None:
    async with _service(tmp_path, enforce=True) as service:
        info, _ = await service.start_conversation(
            _request(_agent(_direct_llm()), workspace_dir)
        )
    async with _service(tmp_path, enforce=True) as restarted:
        restored = await restarted.get_conversation(info.id)
    assert restored is not None
    assert restored.agent.llm.model == "openai/deepseek-flash"
    assert restored.agent.llm.reasoning_effort == "high"


@pytest.mark.asyncio
async def test_legacy_conversation_reloads_unchanged_under_policy(
    tmp_path: Path, workspace_dir: Path
) -> None:
    """A conversation persisted before the policy existed loads, not rejected."""
    async with _service(tmp_path, enforce=False) as service:
        info, _ = await service.start_conversation(
            _request(_agent(_weak_llm()), workspace_dir)
        )
    async with _service(tmp_path, enforce=True) as restarted:
        restored = await restarted.get_conversation(info.id)
    assert restored is not None
    assert restored.agent.llm.model == "openai/gpt-4o"


class _StubTitleService:
    """Minimal stand-in for the fields AutoTitleSubscriber touches."""

    def __init__(self, agent_llm: LLM | None = None) -> None:
        self.stored = SimpleNamespace(title=None, title_llm_profile=None)
        self._conversation = (
            SimpleNamespace(agent=SimpleNamespace(llm=agent_llm))
            if agent_llm is not None
            else None
        )
        self.errors: list[Exception] = []

    def _publish_error_event_sync(self, exc: Exception) -> None:
        self.errors.append(exc)

    async def save_meta(self) -> None:
        return None


def _subscriber(
    service: _StubTitleService, policy: DeploymentLLMPolicy | None
) -> AutoTitleSubscriber:
    return AutoTitleSubscriber(
        service=cast(EventService, service), deployment_llm_policy=policy
    )


def test_title_profile_weaker_than_policy_inherits_agent_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_llm = _direct_llm()
    service = _StubTitleService(agent_llm=agent_llm)
    subscriber = _subscriber(service, DeploymentLLMPolicy())
    monkeypatch.setattr(subscriber, "_load_title_llm", lambda: _weak_llm())
    assert subscriber._select_title_llm(agent_llm) is agent_llm


def test_title_profile_compliant_is_used(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_llm = _direct_llm()
    service = _StubTitleService(agent_llm=agent_llm)
    subscriber = _subscriber(service, DeploymentLLMPolicy())
    profile_llm = _direct_llm()
    monkeypatch.setattr(subscriber, "_load_title_llm", lambda: profile_llm)
    assert subscriber._select_title_llm(agent_llm) is profile_llm


def test_title_no_policy_uses_configured_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent_llm = _direct_llm()
    service = _StubTitleService(agent_llm=agent_llm)
    subscriber = _subscriber(service, None)
    profile_llm = _weak_llm()
    monkeypatch.setattr(subscriber, "_load_title_llm", lambda: profile_llm)
    assert subscriber._select_title_llm(agent_llm) is profile_llm


def test_title_violating_agent_llm_disables_llm_titling() -> None:
    weak = _weak_llm()
    service = _StubTitleService(agent_llm=weak)
    subscriber = _subscriber(service, DeploymentLLMPolicy())
    assert subscriber._select_title_llm(weak) is None


@pytest.mark.asyncio
async def test_explicit_title_is_not_overwritten(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit title set while auto-titling runs wins over the result."""
    service = _StubTitleService(agent_llm=_direct_llm())
    subscriber = _subscriber(service, DeploymentLLMPolicy())
    event = MessageEvent(
        source="user",
        llm_message=Message(role="user", content=[TextContent(text="hello")]),
    )

    def _generate(*_args: Any, **_kwargs: Any) -> str:
        service.stored.title = "Explicit title"
        return "Generated title"

    monkeypatch.setattr(
        "agentrt.agent_server.conversation_service._generate_title_traced", _generate
    )
    await subscriber(event)
    for _ in range(50):
        if service.stored.title == "Explicit title":
            break
        await asyncio.sleep(0.01)
    assert service.stored.title == "Explicit title"


def test_router_rejection_never_echoes_hostile_extra_body() -> None:
    """A secret smuggled through extra_body must not reach the HTTP detail."""
    secret = "sk-do-not-leak-7f3a"
    llm = LLM(
        model="openai/deepseek-flash",
        base_url="https://api.deepseek.com",
        api_mode="chat",
        reasoning_effort="high",
        litellm_extra_body={"reasoning_effort": secret},
        capability_overrides=dict(_CAPABILITY_OVERRIDES),
    )
    service = SimpleNamespace(deployment_llm_policy=DeploymentLLMPolicy())
    with pytest.raises(HTTPException) as excinfo:
        _reject_llm_off_policy(service, llm, action="switch_llm")
    detail = str(excinfo.value.detail)
    assert "litellm_extra_body" in detail
    assert secret not in detail


def test_router_switch_profile_load_error_is_sanitized() -> None:
    """A profile-store ValueError can echo inputs; the response must not."""
    secret = "sk-do-not-leak-7f3a"
    app = FastAPI()
    app.include_router(conversation_router, prefix="/api")
    app.state.config = Config(
        static_files_path=None, session_api_keys=[], secret_key=None
    )
    service = AsyncMock(spec=ConversationService)
    service.deployment_llm_policy = DeploymentLLMPolicy()
    event_service = MagicMock()
    event_service.get_conversation.return_value = MagicMock()
    service.get_event_service.return_value = event_service
    app.dependency_overrides[get_conversation_service] = lambda: service

    store = MagicMock()
    store.load.side_effect = ValueError(f"invalid payload {secret}")
    with patch(
        "agentrt.agent_server.persistence.get_llm_profile_store",
        return_value=store,
    ):
        response = TestClient(app).post(
            f"/api/conversations/{uuid4()}/switch_profile",
            json={"profile_name": "p"},
        )

    assert response.status_code == 400
    assert secret not in response.text
    assert "could not be loaded" in response.json()["detail"].lower()
