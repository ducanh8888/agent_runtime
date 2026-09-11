"""H2: a final response may not cross an input-consumption boundary.

Deterministic and LLM-free. The boundary is the newest user message a run
stepped on, so an answer produced before a later user message must never be
returned as that message's result.
"""

from __future__ import annotations

from agentrt.sdk.conversation.response_utils import get_agent_final_response
from agentrt.sdk.event import MessageEvent
from agentrt.sdk.llm import Message, TextContent


def _user(event_id: str, text: str) -> MessageEvent:
    return MessageEvent(
        id=event_id,
        source="user",
        llm_message=Message(role="user", content=[TextContent(text=text)]),
    )


def _agent(event_id: str, text: str) -> MessageEvent:
    return MessageEvent(
        id=event_id,
        source="agent",
        llm_message=Message(role="assistant", content=[TextContent(text=text)]),
    )


def test_answer_after_boundary_is_returned() -> None:
    u1 = _user("u1", "first")
    a1 = _agent("a1", "answer one")
    u2 = _user("u2", "second")
    a2 = _agent("a2", "answer two")

    assert get_agent_final_response([u1, a1, u2, a2], after_id="u2") == "answer two"


def test_previous_answer_does_not_cross_the_boundary() -> None:
    """The defect: u2 has no answer yet, so a1 must not be returned for it."""
    u1 = _user("u1", "first")
    a1 = _agent("a1", "answer one")
    u2 = _user("u2", "second")

    assert get_agent_final_response([u1, a1, u2], after_id="u2") == ""


def test_boundary_keeps_later_answer_only() -> None:
    u1 = _user("u1", "first")
    a1 = _agent("a1", "answer one")
    u2 = _user("u2", "second")
    a2 = _agent("a2", "answer two")

    # Bounded at u1, a2 is still after it, so it is the newest answer.
    assert get_agent_final_response([u1, a1, u2, a2], after_id="u1") == "answer two"


def test_unknown_boundary_returns_no_response() -> None:
    """An unlocatable boundary is not crossed on a guess."""
    u1 = _user("u1", "first")
    a1 = _agent("a1", "answer one")

    assert get_agent_final_response([u1, a1], after_id="missing") == ""


def test_no_boundary_keeps_legacy_behaviour() -> None:
    """Sessions without boundary tracking still return their last answer."""
    u1 = _user("u1", "first")
    a1 = _agent("a1", "answer one")
    u2 = _user("u2", "second")

    assert get_agent_final_response([u1, a1, u2]) == "answer one"
