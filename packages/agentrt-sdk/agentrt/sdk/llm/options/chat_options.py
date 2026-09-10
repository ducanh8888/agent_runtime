from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agentrt.sdk.llm.options.common import (
    apply_call_context,
    apply_defaults_if_absent,
    apply_extra_body,
    apply_extra_headers,
    merge_extra_body_defaults,
)


if TYPE_CHECKING:
    from agentrt.sdk.llm.llm import LLMCallContext


def select_chat_options(
    llm,
    user_kwargs: dict[str, Any],
    has_tools: bool,
    call_context: LLMCallContext | None = None,
) -> dict[str, Any]:
    """Behavior-preserving extraction of _normalize_call_kwargs.

    This keeps the exact provider-aware mappings and precedence.
    """
    # First pass: apply simple defaults without touching user-supplied values
    max_output_tokens = llm.effective_max_output_tokens
    defaults: dict[str, Any] = {
        "top_k": llm.top_k,
        "top_p": llm.top_p,
        "temperature": llm.temperature,
        # OpenAI-compatible param is `max_completion_tokens`
        "max_completion_tokens": max_output_tokens,
    }
    out = apply_defaults_if_absent(user_kwargs, defaults)

    # Azure -> uses max_tokens instead
    if llm.model.startswith("azure"):
        if "max_completion_tokens" in out:
            out["max_tokens"] = out.pop("max_completion_tokens")

    out = apply_extra_headers(out, llm)

    model_features = llm._model_features()
    supports_reasoning_effort = model_features.supports_reasoning_effort
    if supports_reasoning_effort:
        if llm.reasoning_effort is not None:
            out["reasoning_effort"] = llm.reasoning_effort

    model_name = llm._model_name_for_capabilities()
    if model_features.supports_sampling_params is False or (
        model_features.supports_sampling_params is None
        and supports_reasoning_effort
        and "gemini" not in model_name.lower()
    ):
        out.pop("temperature", None)
        out.pop("top_p", None)
        out.pop("top_k", None)

    if model_features.thinking_mode == "manual":
        if llm.extended_thinking_budget and max_output_tokens:
            budget_tokens = min(
                llm.extended_thinking_budget,
                max_output_tokens - 1,
            )
            out["thinking"] = {
                "type": "enabled",
                "budget_tokens": budget_tokens,
            }
            existing = out.get("extra_headers") or {}
            out["extra_headers"] = {
                "anthropic-beta": "interleaved-thinking-2025-05-14",
                **existing,
            }
            out["max_tokens"] = max_output_tokens
        out.pop("temperature", None)
        out.pop("top_p", None)
        out.pop("top_k", None)

    elif model_features.thinking_mode == "enabled":
        # DeepSeek-style thinking: an explicit enabled flag with no token budget.
        out["thinking"] = {"type": "enabled"}

    # Tools: if not using native, strip tool_choice so we don't confuse providers
    if not has_tools:
        out.pop("tools", None)
        out.pop("tool_choice", None)

    # Send prompt_cache_retention only if model supports it
    if model_features.supports_prompt_cache_retention and llm.prompt_cache_retention:
        out["prompt_cache_retention"] = llm.prompt_cache_retention

    out = apply_extra_body(out, llm)

    # OpenAI-compatible aliases (e.g. openai/deepseek-flash) infer a provider
    # whose parameter list excludes reasoning controls, so LiteLLM drops the
    # top-level fields. Forward them via extra_body as well to keep the
    # explicit request on the wire. Scoped to the resolved capability, so a
    # thinking_mode/supports_reasoning_effort override still disables it.
    reasoning_defaults: dict[str, Any] | None = None
    if model_features.thinking_mode == "enabled":
        reasoning_defaults = {"thinking": {"type": "enabled"}}
        if model_features.supports_reasoning_effort and llm.reasoning_effort:
            reasoning_defaults["reasoning_effort"] = llm.reasoning_effort
    out = merge_extra_body_defaults(out, reasoning_defaults)

    out = apply_call_context(out, llm, call_context)

    return out
