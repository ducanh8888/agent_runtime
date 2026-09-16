"""H8 item 4: why the provider stopped, carried from the response to the caller.

The point is one distinction: an answer cut off at the token limit must not be
indistinguishable from one that finished. Both APIs say it differently -- chat
completions report `length`, the Responses API reports an incomplete response
with `max_output_tokens` -- so the derivation lives on `Message` rather than at
every call site.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from litellm.types.utils import Choices, Message as LiteLLMMessage, ModelResponse

from agentrt.sdk.llm import LLM, Message, TextContent
from agentrt.sdk.llm.utils.metrics import MetricsSnapshot, TokenUsage


def _llm() -> LLM:
    return LLM(model="test-model", api_key="test-key", usage_id="test")


def _response(finish_reason: str | None) -> ModelResponse:
    choice = Choices(
        message=LiteLLMMessage(role="assistant", content="partial answer"),
        index=0,
        finish_reason=finish_reason,
    )
    return ModelResponse(choices=[choice], model="test-model")


# --- the derivation ------------------------------------------------------


@pytest.mark.parametrize(
    "reason,expected",
    [
        ("length", True),
        ("max_output_tokens", True),
        ("stop", False),
        ("tool_calls", False),
        (None, False),
        ("something_new", False),
    ],
)
def test_truncated_recognises_both_apis_and_nothing_else(reason, expected) -> None:
    """An unrecognised reason is not called truncated.

    Reporting a cut-off answer as complete is the failure this prevents, and so
    is the reverse: a caller told "truncated" when it was not would go looking
    for text that does not exist.
    """
    assert Message(role="assistant", finish_reason=reason).truncated is expected


def test_the_default_is_no_reason_reported() -> None:
    """None means "the provider did not say", not "stop"."""
    message = Message(role="assistant")

    assert message.finish_reason is None
    assert message.truncated is False


# --- carried from the provider response ----------------------------------


def test_a_length_completion_carries_its_finish_reason() -> None:
    result = _llm()._build_completion_result(_response("length"))

    assert result.message.finish_reason == "length"
    assert result.message.truncated is True


def test_a_natural_stop_is_recorded_and_not_called_truncated() -> None:
    result = _llm()._build_completion_result(_response("stop"))

    assert result.message.finish_reason == "stop"
    assert result.message.truncated is False


def test_litellm_fills_a_missing_reason_so_the_builder_carries_that() -> None:
    """A response built with no finish_reason does not reach us without one.

    Measured rather than assumed: litellm normalizes a ``None`` finish_reason to
    ``stop``, so the "provider reported nothing" case cannot be constructed
    through ``ModelResponse`` at all -- and the builder carrying ``stop`` is the
    correct translation of what it was given. The genuinely-unset case is
    reachable only through the model default, which is covered separately.
    """
    result = _llm()._build_completion_result(_response(None))

    assert result.message.finish_reason == "stop"
    assert result.message.truncated is False


def _responses(status: str, details: Any) -> Any:
    """A real ``ResponsesAPIResponse`` -- ``LLMResponse`` validates the type.

    ``model_construct`` rather than the constructor: only the fields this reads
    matter here, and the rest of the shape is litellm's business.
    """
    from litellm.types.llms.openai import ResponsesAPIResponse

    return ResponsesAPIResponse.model_construct(
        status=status,
        incomplete_details=details,
        output=[
            {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "partial"}],
            }
        ],
        model="test-model",
    )


@pytest.mark.parametrize(
    "details",
    [{"reason": "max_output_tokens"}, MagicMock(reason="max_output_tokens")],
    ids=["detail-as-a-dict", "detail-as-an-object"],
)
def test_an_incomplete_responses_api_result_maps_onto_the_same_field(details) -> None:
    """Both shapes of ``incomplete_details``.

    litellm has delivered this as a dict and as an object; reading only the
    attribute form loses the reason on the dict, and loses it silently --
    reporting a truncated answer as complete.
    """
    result = _llm()._build_responses_result(_responses("incomplete", details))

    assert result.message.finish_reason == "max_output_tokens"
    assert result.message.truncated is True


def test_a_complete_responses_api_result_carries_no_reason() -> None:
    result = _llm()._build_responses_result(_responses("completed", None))

    assert result.message.finish_reason is None
    assert result.message.truncated is False


# --- the field survives serialization ------------------------------------


def test_the_reason_survives_a_round_trip() -> None:
    """It has to persist with the event, or it is gone by the time anyone asks."""
    message = Message(
        role="assistant",
        content=[TextContent(text="partial")],
        finish_reason="length",
    )

    reloaded = Message.model_validate_json(message.model_dump_json())

    assert reloaded.finish_reason == "length"
    assert reloaded.truncated is True


def test_history_without_the_field_still_loads() -> None:
    """Sessions persisted before the field existed must keep loading."""
    old = '{"role": "assistant", "content": [{"type": "text", "text": "hi"}]}'

    reloaded = Message.model_validate_json(old)

    assert reloaded.finish_reason is None
    assert reloaded.truncated is False


def test_the_metrics_snapshot_is_untouched() -> None:
    """The new field rides on the message, not the metrics."""
    result = _llm()._build_completion_result(_response("length"))

    assert isinstance(result.metrics, MetricsSnapshot)
    assert result.metrics.accumulated_token_usage.model == "test-model"
    assert TokenUsage(model="test-model").model == "test-model"
