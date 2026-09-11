"""H6: first-token timing, kept apart from the full-call latency."""

from __future__ import annotations

from types import SimpleNamespace

from agentrt.sdk.llm.llm import LLM
from agentrt.sdk.llm.utils.metrics import Metrics


class _Telemetry:
    """Records what the streaming loop tells telemetry."""

    def __init__(self) -> None:
        self.calls: list[bool] = []

    def on_first_token(self, *, reasoning: bool) -> None:
        self.calls.append(reasoning)


def _chunk(content: str | None = None, reasoning: str | None = None):
    delta = SimpleNamespace(content=content, reasoning_content=reasoning)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)])


def test_the_streaming_hook_names_each_kind() -> None:
    telemetry = _Telemetry()
    llm = SimpleNamespace(telemetry=telemetry)

    assert LLM._note_first_token(llm, _chunk(reasoning="thinking")) == {True}
    assert LLM._note_first_token(llm, _chunk(content="hello")) == {False}
    # An empty keepalive chunk claims nothing.
    assert LLM._note_first_token(llm, _chunk()) == set()
    assert telemetry.calls == [True, False]


def test_the_hook_tolerates_a_missing_telemetry() -> None:
    llm = SimpleNamespace()

    assert LLM._note_first_token(llm, _chunk(content="x")) == {True, False}


def test_metrics_record_both_kinds_and_merge() -> None:
    first = Metrics(model_name="deepseek-flash")
    first.add_first_token_latency(0.25, reasoning=True, response_id="r1")
    first.add_first_token_latency(0.75, reasoning=False, response_id="r1")
    second = Metrics(model_name="deepseek-flash")
    second.add_first_token_latency(2.0, reasoning=False, response_id="r2")

    first.merge(second)

    assert [(e.reasoning, e.latency) for e in first.first_token_latencies] == [
        (True, 0.25),
        (False, 0.75),
        (False, 2.0),
    ]
    # The snapshot stays snapshot-shaped: no per-call lists.
    assert "first_token_latencies" not in first.get_snapshot().model_dump()
