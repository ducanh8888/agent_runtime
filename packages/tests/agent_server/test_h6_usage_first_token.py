"""H6: first-token latency reaches the usage projection, unknown distinct."""

from __future__ import annotations

from uuid import uuid4

from agentrt.agent_server.usage_projection import project_conversation_usage
from agentrt.sdk.conversation.conversation_stats import ConversationStats
from agentrt.sdk.llm.utils.metrics import Metrics


def _metrics(*, with_first_tokens: bool) -> Metrics:
    metrics = Metrics(model_name="deepseek-flash")
    metrics.add_token_usage(
        prompt_tokens=10,
        completion_tokens=2,
        cache_read_tokens=0,
        cache_write_tokens=0,
        context_window=100,
        response_id="r1",
        reasoning_tokens=1,
    )
    if with_first_tokens:
        metrics.add_first_token_latency(0.25, reasoning=True, response_id="r1")
        metrics.add_first_token_latency(0.75, reasoning=False, response_id="r1")
    return metrics


def test_the_projection_reports_both_first_token_timings() -> None:
    stats = ConversationStats(
        usage_to_metrics={"worker": _metrics(with_first_tokens=True)}
    )

    usage = project_conversation_usage(uuid4(), stats)

    call = usage.services[0].calls[0]
    assert call.first_reasoning_token_latency_ms == 250.0
    assert call.first_token_latency_ms == 750.0


def test_a_record_without_the_field_is_unknown_not_zero() -> None:
    stats = ConversationStats(
        usage_to_metrics={"worker": _metrics(with_first_tokens=False)}
    )

    usage = project_conversation_usage(uuid4(), stats)

    call = usage.services[0].calls[0]
    assert call.first_token_latency_ms is None
    assert call.first_reasoning_token_latency_ms is None
