from unittest.mock import patch

import httpx
import pytest
import tenacity
from litellm.exceptions import (
    APIConnectionError,
    BadGatewayError,
    ServiceUnavailableError,
)
from litellm.types.utils import Choices, Message as LiteLLMMessage, ModelResponse, Usage
from pydantic import SecretStr

from agentrt.sdk.llm import LLM, LLMResponse, Message, TextContent
from agentrt.sdk.llm.exceptions import LLMServiceUnavailableError


def create_mock_response(content: str = "Test response", response_id: str = "test-id"):
    """Helper function to create properly structured mock responses."""
    return ModelResponse(
        id=response_id,
        choices=[
            Choices(
                finish_reason="stop",
                index=0,
                message=LiteLLMMessage(content=content, role="assistant"),
            )
        ],
        created=1234567890,
        model="gpt-4o",
        object="chat.completion",
        system_fingerprint="test",
        usage=Usage(prompt_tokens=10, completion_tokens=5, total_tokens=15),
    )


@pytest.fixture
def default_config():
    return LLM(
        usage_id="test-llm",
        model="gpt-4o",
        api_key=SecretStr("test_key"),
        num_retries=2,
        retry_min_wait=1,
        retry_max_wait=2,
    )


def capture_retry_sleeps(monkeypatch):
    sleeps = []
    tenacity_retry = tenacity.retry

    def retry_with_fake_sleep(**kwargs):
        return tenacity_retry(sleep=sleeps.append, **kwargs)

    monkeypatch.setattr(
        "agentrt.sdk.llm.utils.retry_mixin.retry", retry_with_fake_sleep
    )
    return sleeps


@patch("agentrt.sdk.llm.llm.litellm_completion")
def test_completion_retries_api_connection_error(
    mock_litellm_completion, default_config
):
    """Test that APIConnectionError is properly retried."""
    mock_response = create_mock_response("Retry successful")

    # Mock the litellm_completion to first raise an APIConnectionError,
    # then return a successful response
    mock_litellm_completion.side_effect = [
        APIConnectionError(
            message="API connection error",
            llm_provider="test_provider",
            model="test_model",
        ),
        mock_response,
    ]

    # Create an LLM instance and call completion
    llm = LLM(
        model="gpt-4o",
        api_key=SecretStr("test_key"),
        num_retries=2,
        retry_min_wait=1,
        retry_max_wait=2,
        usage_id="test-service",
    )
    response = llm.completion(
        messages=[Message(role="user", content=[TextContent(text="Hello!")])],
    )

    # Verify that the retry was successful
    assert isinstance(response, LLMResponse)
    assert response.raw_response == mock_response
    assert mock_litellm_completion.call_count == 2  # Initial call + 1 retry


@patch("agentrt.sdk.llm.llm.litellm_completion")
def test_completion_retries_bad_gateway_error(mock_litellm_completion, default_config):
    mock_response = create_mock_response("Gateway retry successful")
    mock_litellm_completion.side_effect = [
        BadGatewayError(
            message="Provider returned empty content",
            llm_provider="test_provider",
            model="test_model",
        ),
        mock_response,
    ]

    response = default_config.completion(
        messages=[Message(role="user", content=[TextContent(text="Hello!")])],
    )

    assert response.raw_response == mock_response
    assert mock_litellm_completion.call_count == 2


@patch("agentrt.sdk.llm.llm.litellm_completion")
def test_completion_honors_retry_after_header(
    mock_litellm_completion, monkeypatch, default_config
):
    sleeps = capture_retry_sleeps(monkeypatch)
    mock_response = create_mock_response("Service retry successful")
    response = httpx.Response(
        503,
        headers={"Retry-After": "23", "X-OmniRoute-Combo-Pool-Empty": "1"},
        request=httpx.Request("POST", "https://router.invalid"),
    )
    mock_litellm_completion.side_effect = [
        ServiceUnavailableError(
            message="all_targets_cooling_down",
            llm_provider="test_provider",
            model="test_model",
            response=response,
        ),
        mock_response,
    ]

    response = default_config.completion(
        messages=[Message(role="user", content=[TextContent(text="Hello!")])],
    )

    assert response.raw_response == mock_response
    assert sleeps == [23.0]
    assert mock_litellm_completion.call_count == 2


@patch("agentrt.sdk.llm.llm.litellm_completion")
def test_completion_caps_retry_after_header(mock_litellm_completion, monkeypatch):
    sleeps = capture_retry_sleeps(monkeypatch)
    mock_response = create_mock_response("Service retry successful")
    response = httpx.Response(
        503,
        headers={"Retry-After": "3600"},
        request=httpx.Request("POST", "https://router.invalid"),
    )
    mock_litellm_completion.side_effect = [
        ServiceUnavailableError(
            message="all_targets_cooling_down",
            llm_provider="test_provider",
            model="test_model",
            response=response,
        ),
        mock_response,
    ]

    llm = LLM(
        usage_id="test-service",
        model="gpt-4o",
        api_key=SecretStr("test_key"),
        num_retries=2,
        retry_min_wait=1,
        retry_max_wait=2,
    )
    response = llm.completion(
        messages=[Message(role="user", content=[TextContent(text="Hello!")])],
    )

    assert response.raw_response == mock_response
    assert sleeps == [300.0]


@patch("agentrt.sdk.llm.llm.litellm_completion")
def test_completion_falls_back_when_retry_after_is_unparseable(
    mock_litellm_completion, monkeypatch
):
    sleeps = capture_retry_sleeps(monkeypatch)
    mock_response = create_mock_response("Service retry successful")
    response = httpx.Response(
        503,
        headers={"Retry-After": "not-a-duration"},
        request=httpx.Request("POST", "https://router.invalid"),
    )
    mock_litellm_completion.side_effect = [
        ServiceUnavailableError(
            message="all_targets_cooling_down",
            llm_provider="test_provider",
            model="test_model",
            response=response,
        ),
        mock_response,
    ]
    llm = LLM(
        usage_id="test-service",
        model="gpt-4o",
        api_key=SecretStr("test_key"),
        num_retries=2,
        retry_min_wait=1,
        retry_max_wait=2,
        retry_multiplier=1,
    )

    result = llm.completion(
        messages=[Message(role="user", content=[TextContent(text="Hello!")])],
    )

    assert result.raw_response == mock_response
    assert sleeps == [1]


@patch("agentrt.sdk.llm.llm.litellm_completion")
def test_completion_max_retries_api_connection_error(
    mock_litellm_completion, default_config
):
    """Test that APIConnectionError respects max retries and is mapped to SDK error."""
    # Mock the litellm_completion to raise APIConnectionError multiple times
    mock_litellm_completion.side_effect = [
        APIConnectionError(
            message="API connection error 1",
            llm_provider="test_provider",
            model="test_model",
        ),
        APIConnectionError(
            message="API connection error 2",
            llm_provider="test_provider",
            model="test_model",
        ),
        APIConnectionError(
            message="API connection error 3",
            llm_provider="test_provider",
            model="test_model",
        ),
    ]

    # Create an LLM instance and call completion
    llm = LLM(
        model="gpt-4o",
        api_key=SecretStr("test_key"),
        num_retries=2,
        retry_min_wait=1,
        retry_max_wait=2,
        usage_id="test-service",
    )

    # The completion should raise an SDK typed error after exhausting all retries

    with pytest.raises(LLMServiceUnavailableError) as excinfo:
        llm.completion(
            messages=[Message(role="user", content=[TextContent(text="Hello!")])],
        )

    # Verify that the correct number of retries were attempted
    # The actual behavior is that it tries num_retries times total
    assert mock_litellm_completion.call_count == default_config.num_retries

    # The exception should contain connection error information
    assert "API connection error" in str(excinfo.value)

    # Ensure the original provider exception is preserved as the cause
    assert isinstance(excinfo.value.__cause__, APIConnectionError)


@patch("agentrt.sdk.llm.llm.litellm_completion")
def test_completion_no_retry_on_success(mock_litellm_completion, default_config):
    """Test that successful calls don't trigger retries."""
    mock_response = create_mock_response("Success on first try")
    mock_litellm_completion.return_value = mock_response

    # Create an LLM instance and call completion
    llm = LLM(
        model="gpt-4o",
        api_key=SecretStr("test_key"),
        num_retries=2,
        retry_min_wait=1,
        retry_max_wait=2,
        usage_id="test-service",
    )
    response = llm.completion(
        messages=[Message(role="user", content=[TextContent(text="Hello!")])],
    )

    # Verify that no retries were needed
    assert isinstance(response, LLMResponse)
    assert response.raw_response == mock_response
    assert mock_litellm_completion.call_count == 1  # Only the initial call


@patch("agentrt.sdk.llm.llm.litellm_completion")
def test_completion_no_retry_on_non_retryable_error(
    mock_litellm_completion, default_config
):
    """Test that non-retryable errors don't trigger retries."""
    # Mock a non-retryable error (e.g., ValueError)
    mock_litellm_completion.side_effect = ValueError("Invalid input")

    # Create an LLM instance and call completion
    llm = LLM(
        model="gpt-4o",
        api_key=SecretStr("test_key"),
        num_retries=2,
        retry_min_wait=1,
        retry_max_wait=2,
        usage_id="test-service",
    )

    # The completion should raise the ValueError immediately without retries
    with pytest.raises(ValueError) as excinfo:
        llm.completion(
            messages=[Message(role="user", content=[TextContent(text="Hello!")])],
        )

    # Verify that no retries were attempted
    assert mock_litellm_completion.call_count == 1  # Only the initial call
    assert "Invalid input" in str(excinfo.value)


def test_retry_configuration_validation():
    """Test that retry configuration is properly validated."""
    # Test with zero retries
    llm_no_retry = LLM(
        model="gpt-4o",
        api_key=SecretStr("test_key"),
        num_retries=0,
        usage_id="test-llm",
    )
    assert llm_no_retry.num_retries == 0

    # Test with custom retry settings
    llm_custom = LLM(
        usage_id="test-llm",
        model="gpt-4o",
        api_key=SecretStr("test_key"),
        num_retries=5,
        retry_min_wait=2,
        retry_max_wait=10,
        retry_multiplier=2.0,
    )
    assert llm_custom.num_retries == 5
    assert llm_custom.retry_min_wait == 2
    assert llm_custom.retry_max_wait == 10
    assert llm_custom.retry_multiplier == 2.0


@patch("agentrt.sdk.llm.llm.litellm_completion")
def test_retry_listener_callback(mock_litellm_completion, default_config):
    """Test that retry listener callback is called during retries."""
    retry_calls = []

    def retry_listener(attempt: int, max_attempts: int, _err: BaseException | None):
        retry_calls.append((attempt, max_attempts, _err))

    mock_response = create_mock_response("Success after retry")

    mock_litellm_completion.side_effect = [
        APIConnectionError(
            message="Connection failed",
            llm_provider="test_provider",
            model="test_model",
        ),
        mock_response,
    ]

    # Create an LLM instance with retry listener
    llm = LLM(
        usage_id="test-llm",
        model="gpt-4o",
        api_key=SecretStr("test_key"),
        num_retries=2,
        retry_min_wait=1,
        retry_max_wait=2,
        retry_listener=retry_listener,
    )
    response = llm.completion(
        messages=[Message(role="user", content=[TextContent(text="Hello!")])],
    )

    # Verify that the retry listener was called
    assert isinstance(response, LLMResponse)
    assert response.raw_response == mock_response
    assert len(retry_calls) >= 1  # At least one retry attempt should be logged

    # Check that retry listener received correct parameters
    if retry_calls:
        attempt, max_attempts, err = retry_calls[0]
        assert isinstance(attempt, int)
        assert isinstance(max_attempts, int)
        assert isinstance(err, APIConnectionError)
        assert attempt >= 1
        assert max_attempts == default_config.num_retries
