"""Utility functions for extracting agent responses from conversation events."""

from collections.abc import Sequence

from agentrt.sdk.event import ActionEvent, MessageEvent
from agentrt.sdk.event.base import Event, EventID
from agentrt.sdk.llm.message import content_to_str
from agentrt.sdk.tool.builtins.finish import FinishAction, FinishTool


def index_of_event(events: Sequence[Event], event_id: EventID) -> int | None:
    """Return the position of ``event_id`` in ``events``, or None if absent.

    ``EventLog`` exposes ``get_index``; a plain sequence falls back to a scan.
    """
    get_index = getattr(events, "get_index", None)
    if callable(get_index):
        try:
            index = get_index(event_id)
        except (KeyError, ValueError):
            return None
        return index if isinstance(index, int) else None
    for index, event in enumerate(events):
        if getattr(event, "id", None) == event_id:
            return index
    return None


def get_agent_final_response(
    events: Sequence[Event], *, after_id: EventID | None = None
) -> str:
    """Extract the final response from the agent.

    An agent can end a conversation in two ways:
    1. By calling the finish tool
    2. By returning a text message with no tool calls

    Args:
        events: List of conversation events to search through.
        after_id: When given, only events strictly after this event id are
            considered. This is the input-consumption boundary: an answer
            produced before a later user message must not be returned as that
            message's result. If the boundary id cannot be located the search
            returns no response rather than risk crossing an unknown boundary.

    Returns:
        The final response message from the agent, or empty string if not found.
    """
    boundary = -1
    if after_id is not None:
        index = index_of_event(events, after_id)
        if index is None:
            return ""
        boundary = index

    # Find the last finish action or message event from the agent. Walk the log
    # backwards and stop at the boundary, so an answer produced before a later
    # user message is never returned as that message's result.
    total = len(events)
    for position, event in enumerate(reversed(events)):
        if total - 1 - position <= boundary:
            break
        # Case 1: finish tool call
        if (
            isinstance(event, ActionEvent)
            and event.source == "agent"
            and event.tool_name == FinishTool.name
        ):
            # Extract message from finish tool call
            if event.action is not None and isinstance(event.action, FinishAction):
                return event.action.message
            else:
                break
        # Case 2: text message with no tool calls (MessageEvent)
        elif isinstance(event, MessageEvent) and event.source == "agent":
            text_parts = content_to_str(event.llm_message.content)
            return "".join(text_parts)
    return ""
