"""Payload-level tests for DeepSeek flash reasoning history across tool calls.

DeepSeek's thinking mode requires the assistant's prior ``reasoning_content`` to
be echoed back on multi-turn tool calls, but that content is private and must not
be merged into user-visible message content.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import ClassVar
from unittest.mock import patch

import pytest
from litellm.types.utils import (
    Choices,
    Message as LiteLLMMessage,
    ModelResponse,
    Usage,
)

from agentrt.sdk.llm import LLM, Message, MessageToolCall, TextContent


REASONING = "hidden chain of thought"


def _mock_response() -> ModelResponse:
    return ModelResponse(
        id="chatcmpl-test",
        choices=[
            Choices(
                finish_reason="stop",
                index=0,
                message=LiteLLMMessage(content="ok", role="assistant"),
            )
        ],
        created=1,
        model="deepseek-flash",
        object="chat.completion",
        usage=Usage(prompt_tokens=1, completion_tokens=1, total_tokens=2),
    )


class _CaptureHandler(BaseHTTPRequestHandler):
    """Fake OpenAI-compatible endpoint that records the serialized request."""

    captured: ClassVar[list[str]] = []

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self.__class__.captured.append(self.rfile.read(length).decode())
        data = json.dumps(_mock_response().model_dump()).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:
        pass


@pytest.fixture
def capture_endpoint():
    _CaptureHandler.captured = []
    server = HTTPServer(("127.0.0.1", 0), _CaptureHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", _CaptureHandler.captured
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


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


@pytest.mark.parametrize("model", ["deepseek/deepseek-flash", "openai/deepseek-flash"])
def test_deepseek_flash_final_http_payload_has_reasoning_controls(
    capture_endpoint, model
):
    """The fully serialized request, after LiteLLM filtering, keeps both controls.

    ``agentrt.sdk.llm.llm.litellm_completion`` is the SDK→LiteLLM boundary, which
    still holds top-level fields. This exercises the real HTTP serialization so
    an alias whose inferred provider strips unsupported params is caught.
    """
    base_url, captured = capture_endpoint
    llm = LLM(
        model=model,
        api_key="sk-test",
        base_url=base_url,
        reasoning_effort="high",
        num_retries=0,
    )

    llm.completion(messages=_tool_history())

    assert captured, "no request reached the fake endpoint"
    raw = captured[-1]
    body = json.loads(raw)
    assert body["reasoning_effort"] == "high"
    assert body["thinking"] == {"type": "enabled"}
    # A duplicate key would let conflicting provider parsing win.
    assert raw.count('"reasoning_effort"') == 1
    assert raw.count('"thinking"') == 1
    # reasoning_content survives for the multi-turn tool call.
    assistant = next(m for m in body["messages"] if m["role"] == "assistant")
    assert assistant["reasoning_content"] == REASONING
    visible = json.dumps([m for m in body["messages"] if m["role"] != "assistant"])
    assert REASONING not in visible


@pytest.mark.parametrize("model", ["deepseek/deepseek-flash", "openai/deepseek-flash"])
@patch("agentrt.sdk.llm.llm.litellm_completion")
def test_deepseek_flash_completion_carries_reasoning_controls(mock_completion, model):
    """At the SDK→LiteLLM boundary both controls are present for every alias."""
    mock_completion.return_value = _mock_response()
    llm = LLM(model=model, api_key="sk-test", reasoning_effort="high")

    llm.completion(messages=_tool_history())

    kwargs = mock_completion.call_args.kwargs
    assert kwargs["reasoning_effort"] == "high"
    assert kwargs["thinking"] == {"type": "enabled"}
    assert kwargs["extra_body"]["reasoning_effort"] == "high"
    assert kwargs["extra_body"]["thinking"] == {"type": "enabled"}
    assert kwargs["messages"][0]["reasoning_content"] == REASONING
