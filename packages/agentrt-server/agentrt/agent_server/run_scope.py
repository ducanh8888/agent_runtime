"""H2 request/run-scoped state derived from a conversation's persisted state.

These are pure functions over :class:`~agentrt.sdk.conversation.state.ConversationState`
fields, so both the conversation listing projection and the agent-response route
can report the same answer without either reading the event log.

The two facts everything here rests on:

- ``last_user_message_id`` -- the newest user input the session has accepted.
- ``consumed_user_message_id`` -- the newest user input an actual run stepped on
  (written by ``LocalConversation`` before each ``step``/``astep``).

A result, an error or a progress claim belongs to an input only when the run
consumed it. When the boundary is missing -- a session that has never run under
this contract -- provenance is reported as unavailable rather than inferred.
"""

from __future__ import annotations

from enum import Enum

from agentrt.sdk.conversation.state import (
    ConversationExecutionStatus,
)


class AgentResponseState(str, Enum):
    """How the current request's answer should be read."""

    #: The newest input has not been consumed by a run yet, so there is no
    #: answer for it. ``response`` is null -- never the previous answer.
    PENDING = "pending"
    #: The run that consumed the input finished with a final response. An empty
    #: response is valid here and is distinguishable from ``pending`` by state.
    FINAL = "final"
    #: The run that consumed the input stopped without a final response
    #: (error, limit, stuck or pause). Any text returned is partial.
    PARTIAL = "partial"
    #: No boundary is known -- no input yet, or a session that never ran under
    #: boundary tracking. Provenance is not invented for these.
    UNAVAILABLE = "unavailable"


class AdmissionStatus(str, Enum):
    """Whether accepted work has been admitted to a run.

    Separate from execution status: a freshly dispatched session is not
    ``idle`` in the sense of "nothing to do" -- it has accepted input that no
    run has started on yet.
    """

    #: Input accepted, no run stepping on it yet.
    QUEUED = "queued"
    #: A run has been admitted but has not completed a first step.
    PREPARING = "preparing"
    #: A run consumed the input (or nothing is outstanding).
    ADMITTED = "admitted"


def derive_result_state(state) -> AgentResponseState:
    """Return the response state for the session's current request."""
    latest = state.last_user_message_id
    consumed = state.consumed_user_message_id
    if latest is None:
        return AgentResponseState.UNAVAILABLE
    if consumed is None:
        # Ran before boundary tracking, or never ran. Its last answer is all we
        # have, but it cannot be attributed to a request, so say so.
        return AgentResponseState.UNAVAILABLE
    if consumed != latest:
        # A newer input exists that no run has stepped on. The previous answer
        # is not this input's answer.
        return AgentResponseState.PENDING
    if state.execution_status in (
        ConversationExecutionStatus.RUNNING,
        ConversationExecutionStatus.WAITING_FOR_CONFIRMATION,
    ):
        return AgentResponseState.PENDING
    if state.execution_status == ConversationExecutionStatus.FINISHED:
        return AgentResponseState.FINAL
    return AgentResponseState.PARTIAL


def derive_admission_status(state) -> AdmissionStatus:
    """Return whether the accepted input has been admitted to a run."""
    latest = state.last_user_message_id
    consumed = state.consumed_user_message_id
    status = state.execution_status
    if latest is None:
        return AdmissionStatus.ADMITTED
    if consumed == latest:
        if status == ConversationExecutionStatus.RUNNING and not state.iterations_used:
            return AdmissionStatus.PREPARING
        return AdmissionStatus.ADMITTED
    # An accepted input the current run has not reached. A run that is active or
    # blocked is admitted regardless; a session sitting between runs with
    # unconsumed input is genuinely queued. Terminal sessions are reported
    # admitted because the queued input will not run without a resume.
    if status in (
        ConversationExecutionStatus.IDLE,
        ConversationExecutionStatus.PAUSED,
    ):
        return AdmissionStatus.QUEUED
    return AdmissionStatus.ADMITTED


def iterations_remaining(state) -> int:
    """Steps left in the current run's allowance (never negative)."""
    return max(0, state.max_iterations - state.iterations_used)
