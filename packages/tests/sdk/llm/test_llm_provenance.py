"""Per-call model/endpoint/policy provenance for LLM completions.

Covers the structured provenance attached to ``TokenUsage`` records:
sanitized endpoints, configured vs. actually-sent reasoning policy,
provider confirmation that is never inferred from token counts, and
retry-safe per-call identity.
"""

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from litellm.exceptions import APIConnectionError
from litellm.types.utils import (
    Choices,
    CompletionTokensDetailsWrapper,
    Message as LiteLLMMessage,
    ModelResponse,
    Usage,
)
from pydantic import SecretStr

from agentrt.sdk.llm import LLM, Message, TextContent
from agentrt.sdk.llm.utils.metrics import Metrics, TokenUsage
from agentrt.sdk.llm.utils.provenance import (
    CallProvenance,
    EndpointProvenance,
    RouteProvenance,
    normalize_thinking_mode,
    policy_from_call_kwargs,
)
from agentrt.sdk.llm.utils.telemetry import Telemetry


def _response(
    response_id: str = "resp-1",
    *,
    prompt_tokens: int = 10,
    completion_tokens: int = 5,
    reasoning_tokens: int = 0,
) -> ModelResponse:
    usage = Usage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
    )
    if reasoning_tokens:
        usage.completion_tokens_details = CompletionTokensDetailsWrapper(
            reasoning_tokens=reasoning_tokens
        )
    return ModelResponse(
        id=response_id,
        choices=[
            Choices(
                finish_reason="stop",
                index=0,
                message=LiteLLMMessage(content="ok", role="assistant"),
            )
        ],
        created=1234567890,
        model="deepseek-flash",
        object="chat.completion",
        usage=usage,
    )


def _user_message() -> Message:
    return Message(role="user", content=[TextContent(text="hi")])


class TestEndpointSanitization:
    """Endpoints are reduced to origin/path with no secret-bearing parts."""

    def test_strips_credentials_query_and_fragment(self):
        endpoint = EndpointProvenance.from_url(
            "https://user:sup3r-secret@api.example.com:8443/v1/chat"
            "?api_key=LEAK#fragment"
        )
        assert endpoint is not None
        assert endpoint.origin == "https://api.example.com:8443"
        assert endpoint.path == "/v1/chat"
        dumped = json.dumps(endpoint.model_dump())
        assert "sup3r-secret" not in dumped
        assert "LEAK" not in dumped
        assert "fragment" not in dumped

    def test_preserves_ipv6_host_without_userinfo(self):
        endpoint = EndpointProvenance.from_url("http://[2001:db8::1]:8080/v1")
        assert endpoint is not None
        assert endpoint.origin == "http://[2001:db8::1]:8080"
        assert endpoint.path == "/v1"

    @pytest.mark.parametrize(
        "value",
        [
            None,
            "",
            "api.example.com/v1",
            "/v1",
            "file:///etc/passwd",
        ],
    )
    def test_unusable_urls_are_unavailable_not_invented(self, value):
        assert EndpointProvenance.from_url(value) is None

    def test_localhost_origin_kept_without_path(self):
        endpoint = EndpointProvenance.from_url("http://127.0.0.1:8000")
        assert endpoint is not None
        assert endpoint.origin == "http://127.0.0.1:8000"
        assert endpoint.path is None


class TestPolicyFromCallKwargs:
    """Sent policy mirrors the final serialization precedence."""

    def test_extra_body_overrides_conflicting_top_level(self):
        policy = policy_from_call_kwargs(
            {
                "reasoning_effort": "high",
                "thinking": {"type": "enabled"},
                "extra_body": {"reasoning_effort": "low"},
            }
        )
        assert policy.reasoning_effort == "low"
        assert policy.thinking_type == "enabled"

    def test_falls_back_to_top_level_when_extra_body_absent(self):
        policy = policy_from_call_kwargs(
            {"reasoning_effort": "high", "thinking": {"type": "enabled"}}
        )
        assert policy.reasoning_effort == "high"
        assert policy.thinking_type == "enabled"

    def test_reads_responses_nested_reasoning_effort(self):
        policy = policy_from_call_kwargs({"reasoning": {"effort": "medium"}})
        assert policy.reasoning_effort == "medium"

    def test_missing_values_stay_none_not_zero(self):
        policy = policy_from_call_kwargs({})
        assert policy.reasoning_effort is None
        assert policy.thinking_type is None
        assert policy.thinking_budget_tokens is None

    def test_thinking_budget_only_when_positive_int(self):
        policy = policy_from_call_kwargs(
            {"thinking": {"type": "enabled", "budget_tokens": 1024}}
        )
        assert policy.thinking_budget_tokens == 1024

        malformed = policy_from_call_kwargs(
            {"thinking": {"type": "enabled", "budget_tokens": -1}}
        )
        assert malformed.thinking_budget_tokens is None


class TestThinkingModeNormalization:
    def test_unknown_and_adaptive_do_not_become_disabled(self):
        assert normalize_thinking_mode("unknown") is None
        assert normalize_thinking_mode("adaptive") is None
        assert normalize_thinking_mode(None) is None

    def test_enabled_and_manual_map_to_enabled(self):
        assert normalize_thinking_mode("enabled") == "enabled"
        assert normalize_thinking_mode("manual") == "enabled"

    def test_none_maps_to_disabled(self):
        assert normalize_thinking_mode("none") == "disabled"


class TestDeepSeekDirectFinalCallKwargs:
    """The configured/sent split reflects the real transport boundary."""

    def test_sent_kwargs_and_provenance(self, monkeypatch):
        llm = LLM(
            model="deepseek/deepseek-flash",
            base_url="https://api.deepseek.com",
            api_key=SecretStr("SUPERSECRET"),
            reasoning_effort="high",
        )
        captured: dict = {}

        def completion(**kwargs):
            captured.update(kwargs)
            return _response(reasoning_tokens=7)

        monkeypatch.setattr("agentrt.sdk.llm.llm.litellm_completion", completion)
        llm.completion(messages=[_user_message()])

        assert captured["model"] == "deepseek-flash"
        assert captured["custom_llm_provider"] == "deepseek"
        assert captured["reasoning_effort"] == "high"
        assert captured["thinking"] == {"type": "enabled"}
        assert captured["extra_body"]["reasoning_effort"] == "high"
        assert captured["extra_body"]["thinking"] == {"type": "enabled"}

        provenance = llm.metrics.token_usages[0].provenance
        assert provenance is not None
        assert provenance.configured.model == "deepseek/deepseek-flash"
        assert provenance.configured.provider == "deepseek"
        assert provenance.configured.endpoint is not None
        assert provenance.configured.endpoint.origin == "https://api.deepseek.com"
        assert provenance.configured.policy.reasoning_effort == "high"
        assert provenance.configured.policy.thinking_type == "enabled"
        assert provenance.sent.model == "deepseek-flash"
        assert provenance.sent.endpoint is not None
        assert provenance.sent.endpoint.origin == "https://api.deepseek.com"
        assert provenance.sent.policy.reasoning_effort == "high"
        assert provenance.sent.policy.thinking_type == "enabled"
        # Reasoning tokens are not evidence the provider honored the policy.
        assert llm.metrics.token_usages[0].reasoning_tokens == 7
        assert provenance.confirmation.reasoning_policy == "unknown"
        assert provenance.call_id == "resp-1"
        assert provenance.call_id_source == "provider_response_id"
        assert "SUPERSECRET" not in json.dumps(provenance.model_dump(mode="json"))

    def test_conflicting_extra_body_low_reports_low_as_sent(self, monkeypatch):
        llm = LLM(
            model="openai/deepseek-flash",
            base_url="https://api.deepseek.com",
            api_key=SecretStr("k"),
            reasoning_effort="high",
            litellm_extra_body={"reasoning_effort": "low"},
        )
        monkeypatch.setattr(
            "agentrt.sdk.llm.llm.litellm_completion",
            lambda **kw: _response(response_id="conflict-1"),
        )
        llm.completion(messages=[_user_message()])

        provenance = llm.metrics.token_usages[0].provenance
        assert provenance is not None
        # Top-level configured effort stays high; the wire value is low.
        assert provenance.configured.policy.reasoning_effort == "high"
        assert provenance.sent.policy.reasoning_effort == "low"

    def test_missing_base_url_does_not_invent_endpoint_from_alias(self, monkeypatch):
        llm = LLM(
            model="litellm_proxy/deepseek-flash",
            api_key=SecretStr("k"),
            reasoning_effort="high",
        )
        monkeypatch.setattr(
            "agentrt.sdk.llm.llm.litellm_completion",
            lambda **kw: _response(response_id="alias-1"),
        )
        llm.completion(messages=[_user_message()])

        provenance = llm.metrics.token_usages[0].provenance
        assert provenance is not None
        assert provenance.configured.endpoint is None
        assert provenance.sent.endpoint is None
        assert provenance.sent.model == "deepseek-flash"


class TestRetriesRecordCompletedCallsOnce:
    def test_failed_attempts_do_not_add_records_or_provenance(self, monkeypatch):
        llm = LLM(
            model="deepseek/deepseek-flash",
            base_url="https://api.deepseek.com",
            api_key=SecretStr("k"),
            reasoning_effort="high",
            num_retries=3,
            retry_min_wait=0,
            retry_max_wait=0,
        )
        failures = [
            APIConnectionError(
                message="fail", llm_provider="deepseek", model="deepseek-flash"
            ),
            APIConnectionError(
                message="fail", llm_provider="deepseek", model="deepseek-flash"
            ),
        ]
        responses = [*failures, _response(response_id="completed-1")]
        calls = {"n": 0}

        def completion(**kwargs):
            result = responses[calls["n"]]
            calls["n"] += 1
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr("agentrt.sdk.llm.llm.litellm_completion", completion)
        llm.completion(messages=[_user_message()])

        assert calls["n"] == 3
        assert len(llm.metrics.token_usages) == 1
        assert len(llm.metrics.response_latencies) == 1
        usage = llm.metrics.token_usages[0]
        assert usage.response_id == "completed-1"
        assert usage.provenance is not None
        assert usage.provenance.call_id == "completed-1"
        # Exactly one completed call was accumulated, not one per attempt.
        assert llm.metrics.accumulated_token_usage is not None
        assert llm.metrics.accumulated_token_usage.prompt_tokens == 10
        assert llm.metrics.accumulated_token_usage.completion_tokens == 5


class TestTelemetryProvenanceLifecycle:
    def _provenance(self) -> CallProvenance:
        return CallProvenance(
            configured=RouteProvenance(model="deepseek/deepseek-flash"),
            sent=RouteProvenance(model="deepseek-flash"),
        )

    def test_completed_call_without_usage_records_nothing(self):
        metrics = Metrics(model_name="deepseek-flash")
        telemetry = Telemetry(model_name="deepseek-flash", metrics=metrics)

        telemetry.on_request({}, provenance=self._provenance())
        telemetry.on_response(ModelResponse(id="no-usage", usage=None))

        assert metrics.token_usages == []
        assert len(metrics.response_latencies) == 1
        assert metrics.accumulated_token_usage is not None
        assert metrics.accumulated_token_usage.prompt_tokens == 0

        # The dropped provenance must not be attributed to the next call.
        telemetry.on_request({}, provenance=self._provenance())
        telemetry.on_response(_response(response_id="with-usage"))

        assert len(metrics.token_usages) == 1
        usage = metrics.token_usages[0]
        assert usage.response_id == "with-usage"
        assert usage.provenance is not None
        assert usage.provenance.call_id == "with-usage"

    def test_call_id_is_explicit_unavailable_when_response_id_missing(self):
        metrics = Metrics(model_name="deepseek-flash")
        telemetry = Telemetry(model_name="deepseek-flash", metrics=metrics)
        telemetry.on_request({}, provenance=self._provenance())
        telemetry.on_response(_response(response_id=""))

        usage = metrics.token_usages[0]
        assert usage.provenance is not None
        assert usage.provenance.call_id == "unavailable"
        assert usage.provenance.call_id_source == "unavailable"

    def test_confirmation_defaults_unknown_without_reasoning_tokens(self):
        metrics = Metrics(model_name="deepseek-flash")
        telemetry = Telemetry(model_name="deepseek-flash", metrics=metrics)
        telemetry.on_request({}, provenance=self._provenance())
        telemetry.on_response(_response(reasoning_tokens=123))

        provenance = metrics.token_usages[0].provenance
        assert provenance is not None
        assert provenance.confirmation.reasoning_policy == "unknown"

    def test_concurrent_calls_keep_their_own_provenance(self):
        """Auto-title and worker calls may share one LLM/Telemetry instance."""
        metrics = Metrics(model_name="deepseek-flash")
        telemetry = Telemetry(model_name="deepseek-flash", metrics=metrics)
        barrier = threading.Barrier(2)

        def complete(name: str) -> None:
            provenance = CallProvenance(
                configured=RouteProvenance(model=f"configured-{name}"),
                sent=RouteProvenance(model=f"sent-{name}"),
            )
            telemetry.on_request({}, provenance=provenance)
            barrier.wait()
            telemetry.on_response(_response(response_id=f"response-{name}"))

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [executor.submit(complete, name) for name in ("worker", "title")]
            for future in futures:
                future.result()

        recorded = {
            usage.response_id: usage.provenance for usage in metrics.token_usages
        }
        assert set(recorded) == {"response-worker", "response-title"}
        for name in ("worker", "title"):
            provenance = recorded[f"response-{name}"]
            assert provenance is not None
            assert provenance.configured.model == f"configured-{name}"
            assert provenance.sent.model == f"sent-{name}"
            assert provenance.call_id == f"response-{name}"


_LEGACY_USAGE = {
    "model": "gpt-4o-mini",
    "prompt_tokens": 100,
    "completion_tokens": 50,
    "cache_read_tokens": 10,
    "cache_write_tokens": 5,
    "reasoning_tokens": 0,
    "context_window": 4096,
    "per_turn_token": 150,
    "response_id": "legacy-1",
}


class TestLegacyJsonCompatibility:
    def test_legacy_payload_loads_without_provenance(self):
        usage = TokenUsage.model_validate(_LEGACY_USAGE)
        assert usage.provenance is None
        assert usage.model_dump() == _LEGACY_USAGE

    def test_records_without_provenance_dump_legacy_shape(self):
        usage = TokenUsage(**_LEGACY_USAGE)
        assert "provenance" not in usage.model_dump()
        assert "provenance" not in usage.model_dump(mode="json")

    def test_metrics_get_preserves_legacy_usage_shape(self):
        metrics = Metrics(model_name="gpt-4o-mini")
        metrics.add_token_usage(100, 50, 10, 5, 4096, "legacy-1")
        payload = metrics.get()
        assert "provenance" not in payload["token_usages"][0]
        assert "provenance" not in payload["accumulated_token_usage"]
