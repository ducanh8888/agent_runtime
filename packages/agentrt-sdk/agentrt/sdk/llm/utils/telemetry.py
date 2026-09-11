import json
import os
import time
import traceback
import uuid
import warnings
from collections.abc import Callable
from contextvars import ContextVar
from typing import Any, ClassVar

from litellm.cost_calculator import completion_cost as litellm_completion_cost
from litellm.types.llms.openai import ResponseAPIUsage, ResponsesAPIResponse
from litellm.types.utils import CostPerToken, ModelResponse, Usage
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

from agentrt.sdk.llm.utils.litellm_provider import LLMProvider
from agentrt.sdk.llm.utils.metrics import Metrics
from agentrt.sdk.llm.utils.openhands_provider import litellm_call_kwargs
from agentrt.sdk.llm.utils.provenance import CallProvenance, call_identity
from agentrt.sdk.logger import get_logger


logger = get_logger(__name__)


class _RequestLocalProvenance:
    """Context-local provenance holder that can be deep-copied with an LLM."""

    def __init__(self) -> None:
        self._value: ContextVar[CallProvenance | None] = ContextVar(
            "agentrt_pending_llm_provenance", default=None
        )

    def get(self) -> CallProvenance | None:
        return self._value.get()

    def set(self, value: CallProvenance | None) -> None:
        self._value.set(value)

    def __deepcopy__(self, memo: dict[int, Any]) -> "_RequestLocalProvenance":
        # Runtime request state must not cross into a copied LLM/Telemetry.
        copied = type(self)()
        memo[id(self)] = copied
        return copied


class _RequestLocalTiming:
    """First-token timing for one request, held per context.

    One LLM instance runs the worker and the auto-title call concurrently (the
    same reason `_RequestLocalProvenance` exists), so timing kept on the
    instance is attributed to whichever call reads it first.
    """

    def __init__(self) -> None:
        self._value: ContextVar[dict | None] = ContextVar(
            "agentrt_request_timing", default=None
        )

    def start(self, *, at: float) -> None:
        """Begin tracking, reusing the request's own start time."""
        self._value.set({"req_start": at, "first": {}})

    def note(self, *, reasoning: bool) -> None:
        state = self._value.get()
        if state is None:
            return
        state["first"].setdefault(reasoning, time.time())

    def take(self) -> tuple[float, dict[bool, float]] | None:
        state = self._value.get()
        if state is None:
            return None
        self._value.set(None)
        return state["req_start"], state["first"]

    def __deepcopy__(self, memo: dict[int, Any]) -> "_RequestLocalTiming":
        copied = type(self)()
        memo[id(self)] = copied
        return copied


class Telemetry(BaseModel):
    """
    Handles latency, token/cost accounting, and optional logging.
    All runtime state (like start times) lives in private attrs.
    """

    # --- Config fields ---
    model_name: str = Field(default="unknown", description="Name of the LLM model")
    log_enabled: bool = Field(default=False, description="Whether to log completions")
    log_dir: str | None = Field(
        default=None, description="Directory to write logs if enabled"
    )
    input_cost_per_token: float | None = Field(
        default=None, ge=0, description="Custom Input cost per token (USD)"
    )
    output_cost_per_token: float | None = Field(
        default=None, ge=0, description="Custom Output cost per token (USD)"
    )

    metrics: Metrics = Field(..., description="Metrics collector instance")

    # --- Runtime fields (not serialized) ---
    _req_start: float = PrivateAttr(default=0.0)
    _req_ctx: dict[str, Any] = PrivateAttr(default_factory=dict)
    # LLM instances can serve the worker and auto-title concurrently. Keep
    # request provenance local to the current async/task or thread context so
    # one response cannot consume another in-flight call's metadata.
    _pending_provenance: _RequestLocalProvenance = PrivateAttr(
        default_factory=_RequestLocalProvenance
    )
    _last_latency: float = PrivateAttr(default=0.0)
    # First token of each kind, per request and per context. A response that
    # never saw an on_request reads as "nothing recorded" rather than raising.
    _timing: _RequestLocalTiming = PrivateAttr(default_factory=_RequestLocalTiming)
    _log_completions_callback: Callable[[str, str], None] | None = PrivateAttr(
        default=None
    )
    _stats_update_callback: Callable[[], None] | None = PrivateAttr(default=None)

    model_config: ClassVar[ConfigDict] = ConfigDict(
        extra="forbid", arbitrary_types_allowed=True
    )

    # ---------- Lifecycle ----------
    def set_log_completions_callback(
        self, callback: Callable[[str, str], None] | None
    ) -> None:
        """Set a callback function for logging instead of writing to file.

        Args:
            callback: A function that takes (filename, log_data) and handles the log.
                     Used for streaming logs in remote execution contexts.
        """
        self._log_completions_callback = callback

    def set_stats_update_callback(self, callback: Callable[[], None] | None) -> None:
        """Set a callback function to be notified when stats are updated.

        Args:
            callback: A function called whenever metrics are updated.
                     Used for streaming stats updates in remote execution contexts.
        """
        self._stats_update_callback = callback

    def on_request(
        self,
        telemetry_ctx: dict | None,
        provenance: CallProvenance | None = None,
    ) -> None:
        self._req_start = time.time()
        self._req_ctx = telemetry_ctx or {}
        self._timing.start(at=self._req_start)
        self._pending_provenance.set(provenance)

    def on_first_token(self, *, reasoning: bool) -> None:
        """Note when the first token of a kind arrived for this request.

        Idempotent per kind: a later token never overwrites the first one. A
        no-op when no request is in flight in this context.
        """
        self._timing.note(reasoning=reasoning)

    def on_response(
        self,
        resp: ModelResponse | ResponsesAPIResponse,
        raw_resp: ModelResponse | None = None,
        provider_info: LLMProvider | None = None,
    ) -> Metrics:
        """
        Side-effects:
          - records latency, tokens, cost into Metrics
          - optionally writes a JSON log file
        """
        # 1) latency
        self._last_latency = time.time() - (self._req_start or time.time())
        response_id = resp.id or ""
        self.metrics.add_response_latency(self._last_latency, response_id)
        # First-token timing is attached here because the response id is only
        # known once the call returns.
        timing = self._timing.take()
        if timing is not None:
            started, firsts = timing
            for reasoning, at in firsts.items():
                self.metrics.add_first_token_latency(
                    at - started, reasoning=reasoning, response_id=response_id
                )
        provenance = self._take_provenance(response_id)

        # 2) cost
        cost = self._compute_cost(resp, provider_info=provider_info)
        # Intentionally skip logging zero-cost (0.0) responses; only record
        # positive cost
        if cost:
            self.metrics.add_cost(cost)

        # 3) tokens - use typed usage field when available
        usage = getattr(resp, "usage", None)

        if usage and self._has_meaningful_usage(usage):
            self._record_usage(
                usage,
                response_id,
                self._req_ctx.get("context_window", 0),
                provenance=provenance,
            )
        elif provenance is not None:
            # A completed provider call whose response carries no usage still
            # has provenance, but writing a zero-token TokenUsage would assert
            # "0 tokens" rather than "unknown". The stats owner has no
            # zero/unknown distinction here, so the call is left out of the
            # per-call list and its provenance is consumed (not misattributed
            # to a later call). It is therefore reported as usage-unavailable.
            logger.debug(
                "Completed call %s has no provider usage; provenance not "
                "recorded as a per-call usage entry.",
                response_id or "<unavailable>",
            )

        # 4) optional logging
        if self.log_enabled:
            self.log_llm_call(resp, cost, raw_resp=raw_resp)

        # 5) notify about stats update
        if self._stats_update_callback is not None:
            try:
                self._stats_update_callback()
            except Exception:
                logger.exception("Stats update callback failed", exc_info=True)

        return self.metrics.deep_copy()

    def on_error(self, _err: BaseException) -> None:
        # Best-effort logging for failed requests (so we can debug malformed
        # request payloads, e.g. orphaned Responses reasoning items).
        self._last_latency = time.time() - (self._req_start or time.time())

        if not self.log_enabled:
            return
        if not self.log_dir and not self._log_completions_callback:
            return

        try:
            filename = (
                f"{self.model_name.replace('/', '__')}-"
                f"{time.time():.3f}-"
                f"{uuid.uuid4().hex[:4]}-error.json"
            )

            data = self._req_ctx.copy()
            data["error"] = {
                "type": type(_err).__name__,
                "message": str(_err),
                "repr": repr(_err),
                "traceback": "".join(
                    traceback.format_exception(type(_err), _err, _err.__traceback__)
                ),
            }
            data["timestamp"] = time.time()
            data["latency_sec"] = self._last_latency
            data["cost"] = 0.0

            log_data = json.dumps(data, default=_safe_json, ensure_ascii=False)

            if self._log_completions_callback:
                self._log_completions_callback(filename, log_data)
            elif self.log_dir:
                os.makedirs(self.log_dir, exist_ok=True)
                fname = os.path.join(self.log_dir, filename)
                with open(fname, "w", encoding="utf-8") as f:
                    f.write(log_data)
        except Exception as e:
            warnings.warn(f"Telemetry error logging failed: {e}")
        return

    # ---------- Helpers ----------
    def _has_meaningful_usage(self, usage: Usage | ResponseAPIUsage | None) -> bool:
        """Check if usage has meaningful (non-zero) token counts.

        Supports both Chat Completions Usage and Responses API Usage shapes.
        """
        if usage is None:
            return False
        try:
            prompt_tokens = getattr(usage, "prompt_tokens", None)
            if prompt_tokens is None:
                prompt_tokens = getattr(usage, "input_tokens", 0)
            completion_tokens = getattr(usage, "completion_tokens", None)
            if completion_tokens is None:
                completion_tokens = getattr(usage, "output_tokens", 0)

            pt = int(prompt_tokens or 0)
            ct = int(completion_tokens or 0)
            return pt > 0 or ct > 0
        except Exception:
            return False

    def _take_provenance(self, response_id: str) -> CallProvenance | None:
        """Bind the pending request provenance to this completed response's id.

        The pending value is consumed once so a retry cannot double-count or
        attach a previous attempt's provenance to the next completed call.
        """
        provenance = self._pending_provenance.get()
        self._pending_provenance.set(None)
        if provenance is None:
            return None
        call_id, call_id_source = call_identity(response_id)
        return provenance.model_copy(
            update={"call_id": call_id, "call_id_source": call_id_source}
        )

    def _record_usage(
        self,
        usage: Usage | ResponseAPIUsage,
        response_id: str,
        context_window: int,
        provenance: CallProvenance | None = None,
    ) -> None:
        """
        Record token usage, supporting both Chat Completions Usage and
        Responses API Usage.

        Chat shape:
          - prompt_tokens, completion_tokens
          - prompt_tokens_details.cached_tokens
          - completion_tokens_details.reasoning_tokens
          - _cache_creation_input_tokens for cache_write
        Responses shape:
          - input_tokens, output_tokens
          - input_tokens_details.cached_tokens
          - output_tokens_details.reasoning_tokens
        """
        prompt_tokens = int(
            getattr(usage, "prompt_tokens", None)
            or getattr(usage, "input_tokens", 0)
            or 0
        )
        completion_tokens = int(
            getattr(usage, "completion_tokens", None)
            or getattr(usage, "output_tokens", 0)
            or 0
        )

        cache_read = 0
        p_details = getattr(usage, "prompt_tokens_details", None) or getattr(
            usage, "input_tokens_details", None
        )
        if p_details is not None:
            cache_read = int(getattr(p_details, "cached_tokens", 0) or 0)

        # Kimi-K2-thinking populate usage.cached_tokens field
        if not cache_read and hasattr(usage, "cached_tokens"):
            cache_read = int(getattr(usage, "cached_tokens", 0) or 0)

        reasoning_tokens = 0
        c_details = getattr(usage, "completion_tokens_details", None) or getattr(
            usage, "output_tokens_details", None
        )
        if c_details is not None:
            reasoning_tokens = int(getattr(c_details, "reasoning_tokens", 0) or 0)

        # Chat-specific: litellm may set a hidden cache write field
        cache_write = int(getattr(usage, "_cache_creation_input_tokens", 0) or 0)

        self.metrics.add_token_usage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cache_read_tokens=cache_read,
            cache_write_tokens=cache_write,
            reasoning_tokens=reasoning_tokens,
            context_window=context_window,
            response_id=response_id,
            provenance=provenance,
        )

    def _compute_cost(
        self,
        resp: ModelResponse | ResponsesAPIResponse,
        provider_info: LLMProvider | None = None,
    ) -> float | None:
        """Try provider header → litellm direct. Return None on failure."""
        extra_kwargs = {}
        if (
            self.input_cost_per_token is not None
            and self.output_cost_per_token is not None
        ):
            cost_per_token = CostPerToken(
                input_cost_per_token=self.input_cost_per_token,
                output_cost_per_token=self.output_cost_per_token,
            )
            logger.debug(f"Using custom cost per token: {cost_per_token}")
            extra_kwargs["custom_cost_per_token"] = cost_per_token

        try:
            hidden = getattr(resp, "_hidden_params", {}) or {}
            cost = hidden.get("additional_headers", {}).get(
                "llm_provider-x-litellm-response-cost"
            )
            if cost is not None:
                return float(cost)
        except Exception as e:
            logger.debug(f"Failed to get cost from LiteLLM headers: {e}")

        if provider_info is None:
            call_kwargs = litellm_call_kwargs(self.model_name, None)
            provider_info = LLMProvider.from_model(
                model=call_kwargs["model"],
                api_base=call_kwargs["api_base"],
            )
        extra_kwargs.update(provider_info.as_litellm_call_kwargs())
        try:
            return float(
                litellm_completion_cost(completion_response=resp, **extra_kwargs)
            )
        except Exception as e:
            warnings.warn(f"Cost calculation failed: {e}")
            return None

    def log_llm_call(
        self,
        resp: ModelResponse | ResponsesAPIResponse,
        cost: float | None,
        raw_resp: ModelResponse | ResponsesAPIResponse | None = None,
    ) -> None:
        # Skip if neither file logging nor callback is configured
        if not self.log_dir and not self._log_completions_callback:
            return
        try:
            # Prepare filename and log data
            filename = (
                f"{self.model_name.replace('/', '__')}-"
                f"{time.time():.3f}-"
                f"{uuid.uuid4().hex[:4]}.json"
            )

            data = self._req_ctx.copy()
            data["response"] = (
                resp  # ModelResponse | ResponsesAPIResponse;
                # serialized via _safe_json
            )
            data["cost"] = float(cost or 0.0)
            data["timestamp"] = time.time()
            data["latency_sec"] = self._last_latency

            # Usage summary (prompt, completion, reasoning tokens) for quick inspection
            try:
                usage = getattr(resp, "usage", None)
                if usage:
                    prompt_tokens = int(
                        getattr(usage, "prompt_tokens", None)
                        or getattr(usage, "input_tokens", 0)
                        or 0
                    )
                    completion_tokens = int(
                        getattr(usage, "completion_tokens", None)
                        or getattr(usage, "output_tokens", 0)
                        or 0
                    )
                    details = getattr(
                        usage, "completion_tokens_details", None
                    ) or getattr(usage, "output_tokens_details", None)
                    reasoning_tokens = (
                        int(getattr(details, "reasoning_tokens", 0) or 0)
                        if details
                        else 0
                    )
                    p_details = getattr(
                        usage, "prompt_tokens_details", None
                    ) or getattr(usage, "input_tokens_details", None)
                    cache_read_tokens = (
                        int(getattr(p_details, "cached_tokens", 0) or 0)
                        if p_details
                        else 0
                    )

                    data["usage_summary"] = {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "reasoning_tokens": reasoning_tokens,
                        "cache_read_tokens": cache_read_tokens,
                    }
            except Exception:
                # Best-effort only; don't fail logging
                pass

            # Raw response *before* nonfncall -> call conversion
            if raw_resp:
                data["raw_response"] = (
                    raw_resp  # ModelResponse | ResponsesAPIResponse;
                    # serialized via _safe_json
                )
            # Pop duplicated tools to avoid logging twice
            if (
                "tools" in data
                and isinstance(data.get("kwargs"), dict)
                and "tools" in data["kwargs"]
            ):
                data["kwargs"].pop("tools")

            log_data = json.dumps(data, default=_safe_json, ensure_ascii=False)

            # Use callback if set (for remote execution), otherwise write to file
            if self._log_completions_callback:
                self._log_completions_callback(filename, log_data)
            elif self.log_dir:
                # Create log directory if it doesn't exist
                os.makedirs(self.log_dir, exist_ok=True)
                if not os.access(self.log_dir, os.W_OK):
                    raise PermissionError(f"log_dir is not writable: {self.log_dir}")
                fname = os.path.join(self.log_dir, filename)
                with open(fname, "w", encoding="utf-8") as f:
                    f.write(log_data)
        except Exception as e:
            warnings.warn(f"Telemetry logging failed: {e}")


def _safe_json(obj: Any) -> Any:
    # Centralized serializer for telemetry logs.
    # Prefer robust serialization for Pydantic models first to avoid cycles.
    # Typed LiteLLM responses
    if isinstance(obj, ModelResponse) or isinstance(obj, ResponsesAPIResponse):
        return obj.model_dump(mode="json", exclude_none=True)

    # Any Pydantic BaseModel (e.g., ToolDefinition, ChatCompletionToolParam, etc.)
    if isinstance(obj, BaseModel):
        # Use Pydantic's serializer which respects field exclusions (e.g., executors)
        return obj.model_dump(mode="json", exclude_none=True)

    # Fallbacks for other non-serializable objects used elsewhere in the log payload
    try:
        return obj.__dict__
    except Exception:
        return str(obj)
