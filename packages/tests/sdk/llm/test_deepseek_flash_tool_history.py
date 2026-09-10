"""Payload-level tests for DeepSeek flash reasoning history across tool calls.

DeepSeek's thinking mode requires the assistant's prior ``reasoning_content`` to
be echoed back on multi-turn tool calls, but that content is private and must not
be merged into user-visible message content.
"""

import pytest

from agentrt.sdk.llm import LLM, Message, MessageToolCall, TextContent


REASONING = "hidden chain of thought"


def _tool_history() -> list[Message]:
    return [
        Message(
            role="assistant",
            content=[TextContent(text="")],
            tool_calls=[
                MessageToolCall(
                    id="call_1",
                    name="read_file",
                    arguments='{"path": "a"}',
                    origin="completion",
                )
            ],
            reasoning_content=REASONING,
        ),
        Message(
            role="tool",
            content=[TextContent(text="file body")],
            tool_call_id="call_1",
            name="read_file",
        ),
        Message(role="user", content=[TextContent(text="continue")]),
    ]


@pytest.mark.parametrize(
    "model",
    [
        "deepseek-flash",
        "openai/deepseek-flash",
        "openai/ds/deepseek-flash",
        "deepseek/deepseek-flash",
    ],
)
def test_deepseek_flash_replays_reasoning_content_on_tool_turns(model):
    payload = LLM(model=model).format_messages_for_llm(_tool_history())

    assistant, tool_result, user = payload
    assert assistant["reasoning_content"] == REASONING
    assert assistant["tool_calls"][0]["id"] == "call_1"
    # Private reasoning must stay out of the visible content and off other turns.
    assert REASONING not in str(assistant.get("content", ""))
    assert "reasoning_content" not in tool_result
    assert "reasoning_content" not in user


def test_deepseek_flash_replays_reasoning_content_under_canonical_alias():
    llm = LLM(
        model="litellm_proxy/customer-flash",
        model_canonical_name="deepseek-flash",
    )
    payload = llm.format_messages_for_llm(_tool_history())

    assert payload[0]["reasoning_content"] == REASONING


def test_non_deepseek_model_omits_reasoning_content_from_tool_turns():
    """Generic SDK behavior is unchanged: reasoning content is not sent by default."""
    payload = LLM(model="gpt-4o").format_messages_for_llm(_tool_history())

    assert "reasoning_content" not in payload[0]
