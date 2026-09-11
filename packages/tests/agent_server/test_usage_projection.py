"""Tests for the redacted conversation usage projection."""

from uuid import uuid4

from agentrt.agent_server.usage_projection import (
    UsageCall,
    project_conversation_usage,
)
from agentrt.sdk.conversation.conversation_stats import ConversationStats
from agentrt.sdk.llm.utils.metrics import Metrics
from agentrt.sdk.llm.utils.provenance import (
    CallProvenance,
    EndpointProvenance,
    PolicyProvenance,
    ProviderConfirmation,
    RouteProvenance,
)


def _agent_metrics() -> Metrics:
    metrics = Metrics(model_name="deepseek-flash")
    metrics.add_cost(0.25)
    metrics.add_token_usage(
        prompt_tokens=1000,
        completion_tokens=120,
        cache_read_tokens=200,
        cache_write_tokens=0,
        context_window=65536,
        response_id="resp-1",
        reasoning_tokens=64,
        provenance=CallProvenance(
            call_id="resp-1",
            call_id_source="provider_response_id",
            configured=RouteProvenance(
                model="openai/deepseek-flash",
                provider="openai",
                endpoint=EndpointProvenance(
                    origin="https://api.deepseek.com", path="/v1"
                ),
                policy=PolicyProvenance(
                    reasoning_effort="high", thinking_type="enabled"
                ),
            ),
            sent=RouteProvenance(
                model="deepseek-flash",
                provider="openai",
                endpoint=EndpointProvenance(
                    origin="https://api.deepseek.com", path="/v1"
                ),
                policy=PolicyProvenance(
                    reasoning_effort="high", thinking_type="enabled"
                ),
            ),
            confirmation=ProviderConfirmation(reasoning_policy="unknown"),
        ),
    )
    metrics.add_token_usage(
        prompt_tokens=500,
        completion_tokens=40,
        cache_read_tokens=0,
        cache_write_tokens=10,
        context_window=65536,
        response_id="resp-2",
        reasoning_tokens=8,
    )
    return metrics


def _condenser_metrics() -> Metrics:
    metrics = Metrics(model_name="deepseek-flash")
    metrics.add_token_usage(
        prompt_tokens=100,
        completion_tokens=20,
        cache_read_tokens=300,
        cache_write_tokens=0,
        context_window=65536,
        response_id="",
    )
    return metrics


def test_projects_service_and_call_provenance():
    stats = ConversationStats(
        usage_to_metrics={
            "agent": _agent_metrics(),
            "condenser": _condenser_metrics(),
        }
    )

    usage = project_conversation_usage(uuid4(), stats)

    assert [service.usage_id for service in usage.services] == ["agent", "condenser"]

    agent = usage.services[0]
    assert agent.model == "deepseek-flash"
    assert agent.model_source == "stats"
    assert agent.accumulated_cost == 0.25
    assert [call.call_id for call in agent.calls] == ["resp-1", "resp-2"]
    assert all(call.call_id_source == "provider_response_id" for call in agent.calls)
    assert agent.calls[0].usage.prompt_tokens == 1000
    assert agent.calls[0].usage.reasoning_tokens == 64
    assert agent.calls[0].provenance is not None
    assert agent.calls[0].provenance.configured.model == "openai/deepseek-flash"
    assert agent.calls[0].provenance.sent.endpoint is not None
    assert agent.calls[0].provenance.sent.endpoint.origin == "https://api.deepseek.com"
    assert agent.calls[0].provenance.sent.policy.reasoning_effort == "high"
    assert agent.calls[0].provenance.sent.policy.thinking_type == "enabled"
    assert agent.calls[0].provenance.confirmation.reasoning_policy == "unknown"
    assert agent.calls[1].provenance is None

    condenser = usage.services[1]
    assert [call.call_id for call in condenser.calls] == ["condenser:0"]
    assert condenser.calls[0].call_id_source == "ordinal"


def test_normalized_usage_handles_nested_and_separate_cache():
    nested = project_conversation_usage(
        uuid4(), ConversationStats(usage_to_metrics={"agent": _agent_metrics()})
    ).services[0]
    separate = project_conversation_usage(
        uuid4(), ConversationStats(usage_to_metrics={"condenser": _condenser_metrics()})
    ).services[0]

    # litellm/OpenAI nest cache reads inside prompt tokens.
    assert nested.accumulated_token_usage.prompt_tokens == 1500
    assert nested.normalized.input_tokens == 1300
    assert nested.normalized.cache_read_tokens == 200
    assert nested.normalized.output_tokens == 160

    # ACP-style providers report cache separately (cache > prompt).
    assert separate.accumulated_token_usage.prompt_tokens == 100
    assert separate.normalized.input_tokens == 100
    assert separate.normalized.cache_read_tokens == 300


def test_cache_hit_rate_comes_from_the_stats_owner():
    service = project_conversation_usage(
        uuid4(), ConversationStats(usage_to_metrics={"agent": _agent_metrics()})
    ).services[0]
    assert service.cache_hit_rate == 200 / 1500

    empty = project_conversation_usage(
        uuid4(),
        ConversationStats(usage_to_metrics={"agent": Metrics(model_name="m")}),
    ).services[0]
    assert empty.cache_hit_rate is None
    assert empty.normalized.input_tokens == 0


def test_empty_stats_project_to_no_services():
    usage = project_conversation_usage(uuid4(), ConversationStats())
    assert usage.services == []


def test_projection_carries_only_numeric_and_identifier_fields():
    usage = project_conversation_usage(
        uuid4(), ConversationStats(usage_to_metrics={"agent": _agent_metrics()})
    )
    call = usage.services[0].calls[0]
    assert isinstance(call, UsageCall)
    assert set(call.model_dump()) == {
        "call_id",
        "call_id_source",
        "model",
        "provenance",
        "usage",
        "first_token_latency_ms",
        "first_reasoning_token_latency_ms",
    }
    assert set(call.usage.model_dump()) == {
        "prompt_tokens",
        "completion_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "reasoning_tokens",
        "context_window",
        "per_turn_token",
    }


def test_caller_supplied_usage_id_is_bounded_and_stripped():
    noisy_usage_id = "  service\n" + "x" * 300
    service = project_conversation_usage(
        uuid4(),
        ConversationStats(usage_to_metrics={noisy_usage_id: _condenser_metrics()}),
    ).services[0]

    assert "\n" not in service.usage_id
    assert service.usage_id.startswith("service")
    assert len(service.usage_id) <= 128
