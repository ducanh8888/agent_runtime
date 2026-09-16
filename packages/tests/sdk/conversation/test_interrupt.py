"""Tests for conversation.interrupt() — instant cancellation of arun().

Covers:
- Async path verification (acompletion is actually called, not sync fallback)
- CancelledError not re-raised from arun()
- interrupt() after natural completion (no-op)
- Multiple rapid interrupt() calls
- Cancellation token lifecycle (created per run, cleared on exit)
- ParallelToolExecutor skips cancelled tools
"""

import asyncio
import threading
from unittest.mock import MagicMock, patch

import pytest
from litellm.types.utils import ModelResponse
from pydantic import PrivateAttr

from agentrt.sdk.agent import Agent
from agentrt.sdk.conversation.cancellation import CancellationToken
from agentrt.sdk.conversation.impl.local_conversation import LocalConversation
from agentrt.sdk.conversation.state import ConversationExecutionStatus
from agentrt.sdk.event import AgentErrorEvent, InterruptEvent
from agentrt.sdk.llm import (
    LLM,
    LLMResponse,
    Message,
    MessageToolCall,
    TextContent,
    content_to_str,
)
from agentrt.sdk.llm.utils.metrics import MetricsSnapshot, TokenUsage
from agentrt.sdk.tool.schema import Action


class _InterruptProbeAction(Action):
    """Minimal executable action for the orphan test.

    The name is deliberately unique across the suite: pydantic registers
    ``Action`` subclasses by class name process-wide, so reusing a name another
    test module already took (``test_conversation_tree._MockAction``) makes
    *that* module's ``model_validate_json`` fail with a duplicate-class error.
    """

    command: str


def _make_response(model_name: str = "test-slow") -> LLMResponse:
    return LLMResponse(
        message=Message(
            role="assistant",
            content=[TextContent(text="done")],
        ),
        metrics=MetricsSnapshot(
            model_name=model_name,
            accumulated_cost=0.0,
            max_budget_per_task=0.0,
            accumulated_token_usage=TokenUsage(model=model_name),
        ),
        raw_response=MagicMock(spec=ModelResponse, id="s1"),
    )


class SlowLLM(LLM):
    """LLM that blocks in acompletion to simulate a long-running call."""

    _sleep_seconds: float = PrivateAttr(default=10.0)

    def __init__(self, *, sleep_seconds: float = 10.0):
        super().__init__(model="test-slow", usage_id="test-slow")
        self._sleep_seconds = sleep_seconds

    def completion(  # type: ignore[override]
        self, messages, tools=None, **kw
    ) -> LLMResponse:
        import time

        time.sleep(self._sleep_seconds)
        return _make_response()

    async def acompletion(  # type: ignore[override]
        self, messages, tools=None, **kw
    ) -> LLMResponse:
        await asyncio.sleep(self._sleep_seconds)
        return _make_response()


def _make_conversation(llm: LLM, tmp_path) -> LocalConversation:
    agent = Agent(llm=llm, tools=[])
    conv = LocalConversation(
        agent=agent,
        workspace=str(tmp_path),
        visualizer=None,
    )
    conv.send_message("hello")
    return conv


@pytest.mark.asyncio
async def test_interrupt_cancels_arun_immediately(tmp_path):
    """interrupt() should cancel arun() mid-LLM-call and set PAUSED."""
    conv = _make_conversation(SlowLLM(sleep_seconds=60.0), tmp_path)

    task = asyncio.create_task(conv.arun())

    # Let the event loop start arun() and enter the LLM sleep
    await asyncio.sleep(0.05)

    # Interrupt should cancel the in-flight LLM call
    conv.interrupt()

    # arun() should return quickly (it catches CancelledError)
    await asyncio.wait_for(task, timeout=2.0)

    assert conv.state.execution_status == ConversationExecutionStatus.PAUSED

    # An InterruptEvent should have been emitted
    events = list(conv.state.events)
    interrupt_events = [e for e in events if isinstance(e, InterruptEvent)]
    assert len(interrupt_events) == 1


@pytest.mark.asyncio
async def test_interrupt_without_arun_falls_back_to_pause(tmp_path):
    """interrupt() with no active arun() should fall back to pause()."""
    conv = _make_conversation(SlowLLM(sleep_seconds=60.0), tmp_path)

    # Set to RUNNING manually to verify pause fallback
    conv._state.execution_status = ConversationExecutionStatus.RUNNING

    conv.interrupt()

    assert conv.state.execution_status == ConversationExecutionStatus.PAUSED


@pytest.mark.asyncio
async def test_arun_task_cleared_after_interrupt(tmp_path):
    """_arun_task should be None after arun() finishes (via interrupt)."""
    conv = _make_conversation(SlowLLM(sleep_seconds=60.0), tmp_path)

    task = asyncio.create_task(conv.arun())
    await asyncio.sleep(0.05)
    conv.interrupt()
    await asyncio.wait_for(task, timeout=2.0)

    assert conv._arun_task is None


@pytest.mark.asyncio
async def test_interrupt_is_resumable(tmp_path):
    """After interrupt, conversation can be resumed with a new arun()."""

    class CountingLLM(LLM):
        """LLM that completes instantly, counting calls."""

        _call_count: int = PrivateAttr(default=0)

        def __init__(self):
            super().__init__(model="test-counting", usage_id="test-c")

        def completion(  # type: ignore[override]
            self, messages, tools=None, **kw
        ) -> LLMResponse:
            self._call_count += 1
            return _make_response("test-counting")

        async def acompletion(  # type: ignore[override]
            self, messages, tools=None, **kw
        ) -> LLMResponse:
            self._call_count += 1
            return _make_response("test-counting")

    llm = CountingLLM()
    conv = _make_conversation(llm, tmp_path)

    # First run should complete normally (agent says "done" → FINISHED)
    await conv.arun()
    assert conv.state.execution_status == ConversationExecutionStatus.FINISHED
    assert llm._call_count == 1

    # Send another message and run again — should work
    conv.send_message("continue")
    await conv.arun()
    assert llm._call_count == 2


@pytest.mark.asyncio
async def test_arun_calls_acompletion_not_completion(tmp_path):
    """Verify that arun() exercises the async path (acompletion)."""

    class TrackingLLM(LLM):
        _sync_calls: int = PrivateAttr(default=0)
        _async_calls: int = PrivateAttr(default=0)

        def __init__(self):
            super().__init__(model="test-track", usage_id="test-t")

        def completion(self, messages, tools=None, **kw) -> LLMResponse:  # type: ignore[override]
            self._sync_calls += 1
            return _make_response("test-track")

        async def acompletion(self, messages, tools=None, **kw) -> LLMResponse:  # type: ignore[override]
            self._async_calls += 1
            return _make_response("test-track")

    llm = TrackingLLM()
    conv = _make_conversation(llm, tmp_path)
    await conv.arun()

    assert llm._async_calls == 1, "arun() should call acompletion"
    assert llm._sync_calls == 0, "arun() should NOT call sync completion"


@pytest.mark.asyncio
async def test_arun_does_not_raise_cancelled_error(tmp_path):
    """CancelledError must NOT propagate out of arun()."""
    conv = _make_conversation(SlowLLM(sleep_seconds=60.0), tmp_path)

    task = asyncio.create_task(conv.arun())
    await asyncio.sleep(0.05)
    conv.interrupt()

    # If CancelledError propagated, wait_for would raise it.
    # arun() should return cleanly with no exception.
    await asyncio.wait_for(task, timeout=2.0)
    # If we reach here, no CancelledError was raised — test passes.


@pytest.mark.asyncio
async def test_interrupt_after_natural_completion_is_noop(tmp_path):
    """interrupt() after arun() completes naturally should be a safe no-op."""

    class InstantLLM(LLM):
        def __init__(self):
            super().__init__(model="test-instant", usage_id="test-i")

        def completion(self, messages, tools=None, **kw) -> LLMResponse:  # type: ignore[override]
            return _make_response("test-instant")

        async def acompletion(self, messages, tools=None, **kw) -> LLMResponse:  # type: ignore[override]
            return _make_response("test-instant")

    conv = _make_conversation(InstantLLM(), tmp_path)
    await conv.arun()
    assert conv.state.execution_status == ConversationExecutionStatus.FINISHED

    # interrupt() after completion — should not crash or change status
    conv.interrupt()
    assert conv.state.execution_status == ConversationExecutionStatus.FINISHED


@pytest.mark.asyncio
async def test_multiple_rapid_interrupts(tmp_path):
    """Multiple rapid interrupt() calls should not crash."""
    conv = _make_conversation(SlowLLM(sleep_seconds=60.0), tmp_path)

    task = asyncio.create_task(conv.arun())
    await asyncio.sleep(0.05)

    # Fire multiple interrupts rapidly
    conv.interrupt()
    conv.interrupt()
    conv.interrupt()

    await asyncio.wait_for(task, timeout=2.0)
    assert conv.state.execution_status == ConversationExecutionStatus.PAUSED


# ── CancellationToken unit tests ──────────────────────────────────────


def test_cancellation_token_basic():
    """CancellationToken starts uncancelled, becomes cancelled after cancel()."""
    token = CancellationToken()
    assert not token.is_cancelled
    token.cancel()
    assert token.is_cancelled
    # Idempotent
    token.cancel()
    assert token.is_cancelled


# ── Token lifecycle tests ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_token_created_and_cleared_during_arun(tmp_path):
    """A fresh CancellationToken is created in arun() and cleared in finally."""

    class InstantLLM(LLM):
        def __init__(self):
            super().__init__(model="test-instant", usage_id="test-i")

        def completion(self, messages, tools=None, **kw) -> LLMResponse:  # type: ignore[override]
            return _make_response("test-instant")

        async def acompletion(self, messages, tools=None, **kw) -> LLMResponse:  # type: ignore[override]
            return _make_response("test-instant")

    conv = _make_conversation(InstantLLM(), tmp_path)
    assert conv._cancel_token is None  # Before any run

    await conv.arun()

    # After arun() completes, token should be cleared
    assert conv._cancel_token is None


@pytest.mark.asyncio
async def test_interrupt_sets_cancel_token(tmp_path):
    """interrupt() should set the cancel token before cancelling the task."""
    conv = _make_conversation(SlowLLM(sleep_seconds=60.0), tmp_path)

    task = asyncio.create_task(conv.arun())
    await asyncio.sleep(0.05)

    # Token should exist while arun is active
    assert conv._cancel_token is not None
    assert not conv._cancel_token.is_cancelled

    conv.interrupt()
    await asyncio.wait_for(task, timeout=2.0)

    # After an interrupt the cancelled token is retained (not cleared) so tool
    # threads that outlive arun() can still observe it.
    assert conv._cancel_token is not None
    assert conv._cancel_token.is_cancelled


@pytest.mark.asyncio
async def test_cancel_token_stays_observable_after_interrupt(tmp_path):
    """A tool polling conversation.cancel_token from a worker thread that
    outlives arun() must still see the cancellation, not the None the finally
    used to clear. A fresh token is swapped in on the next run."""
    conv = _make_conversation(SlowLLM(sleep_seconds=60.0), tmp_path)

    task = asyncio.create_task(conv.arun())
    await asyncio.sleep(0.05)
    conv.interrupt()
    await asyncio.wait_for(task, timeout=2.0)

    # arun() has run its finally; a late poll via the public property (what
    # tools use) must still observe the cancellation.
    assert conv.cancel_token is not None
    assert conv.cancel_token.is_cancelled

    # The next run replaces it with a fresh, uncancelled token.
    conv.send_message("again")
    resumed = asyncio.create_task(conv.arun())
    await asyncio.sleep(0.05)
    assert conv.cancel_token is not None
    assert not conv.cancel_token.is_cancelled
    conv.interrupt()
    await asyncio.wait_for(resumed, timeout=2.0)


# ── ParallelToolExecutor cancellation tests ───────────────────────────


def _make_action_event(tool_name: str, call_id: str):
    """Build a minimal ActionEvent for executor tests."""
    from agentrt.sdk.event import ActionEvent

    return ActionEvent(
        thought=[TextContent(text="test")],
        tool_call=MessageToolCall(
            id=call_id,
            name=tool_name,
            arguments="{}",
            origin="completion",
        ),
        tool_name=tool_name,
        tool_call_id=call_id,
        llm_response_id="resp-1",
    )


def test_run_safe_skips_cancelled_tool():
    """_run_safe should skip execution when token is already cancelled."""
    from agentrt.sdk.agent.parallel_executor import ParallelToolExecutor

    executor = ParallelToolExecutor(max_workers=1)
    token = CancellationToken()
    token.cancel()

    action = _make_action_event("my_tool", "tc1")
    runner_called = False

    def tool_runner(ae):
        nonlocal runner_called
        runner_called = True
        return []

    result = executor._run_safe(action, tool_runner, cancel_token=token)

    assert not runner_called, "Tool should not have been executed"
    assert len(result) == 1
    assert isinstance(result[0], AgentErrorEvent)
    assert "cancelled" in result[0].error.lower()


def test_run_safe_runs_without_token():
    """_run_safe should execute normally when no token is provided."""
    from agentrt.sdk.agent.parallel_executor import ParallelToolExecutor

    executor = ParallelToolExecutor(max_workers=1)
    action = _make_action_event("my_tool", "tc2")
    runner_called = False

    def tool_runner(ae):
        nonlocal runner_called
        runner_called = True
        return []

    executor._run_safe(action, tool_runner, cancel_token=None)
    assert runner_called


@pytest.mark.asyncio
async def test_execute_batch_skips_all_on_pre_cancelled_token():
    """execute_batch with a pre-cancelled token skips all tool calls."""
    from agentrt.sdk.agent.parallel_executor import ParallelToolExecutor

    executor = ParallelToolExecutor(max_workers=2)
    token = CancellationToken()
    token.cancel()

    actions = [_make_action_event(f"tool_{i}", f"tc-{i}") for i in range(3)]

    runner_calls: list[str] = []

    def tool_runner(ae):
        runner_calls.append(ae.tool_name)
        return []

    results = executor.execute_batch(actions, tool_runner, cancel_token=token)

    assert len(runner_calls) == 0, "No tools should have been called"
    assert len(results) == 3
    for r in results:
        assert len(r) == 1
        assert isinstance(r[0], AgentErrorEvent)
        assert "cancelled" in r[0].error.lower()


@pytest.mark.asyncio
async def test_arun_runs_init_off_the_event_loop(tmp_path):
    """arun() offloads _ensure_agent_ready() to a worker thread.

    Regression for agent-canvas#1072: an ACP agent resolves its credentials in
    init_state via a *blocking* LookupSecret.get_value() (a synchronous
    httpx.get). If arun() ran that inline on the event loop and the lookup
    pointed back at the same single-process server, it would freeze the loop
    that has to serve it — a self-deadlock. Verify init runs on a different
    thread than the one driving the loop.
    """
    conv = _make_conversation(SlowLLM(sleep_seconds=0.0), tmp_path)

    loop_thread_id = threading.get_ident()
    captured: dict[str, int] = {}
    real_ensure = conv._ensure_agent_ready

    def spy_ensure() -> None:
        captured["thread_id"] = threading.get_ident()
        real_ensure()

    with patch.object(conv, "_ensure_agent_ready", spy_ensure):
        await asyncio.wait_for(conv.arun(), timeout=5.0)

    assert captured["thread_id"] != loop_thread_id
    assert conv.state.execution_status == ConversationExecutionStatus.FINISHED


# ── Interrupt visibility to the agent itself (H9 item 6) ──────────────
#
# InterruptEvent is not an LLMConvertibleEvent, so an interrupt that lands
# between turns (no in-flight tool call) left the agent's own history with no
# trace of it. These cover the environment message that now records it, and
# the two cases where recording it would be false or noise.


class RecordingLLM(LLM):
    """Records the messages of every call; sleeps only on the first one.

    The sleep is what makes the interrupt land between turns: acompletion is
    in flight, so no ActionEvent exists yet and ``_emit_orphaned_action_errors``
    has nothing to backfill.
    """

    _calls: list[list[Message]] = PrivateAttr(default_factory=list)
    _sleep_first: float = PrivateAttr(default=60.0)

    def __init__(self, *, sleep_first: float = 60.0):
        super().__init__(model="test-recording", usage_id="test-rec")
        self._sleep_first = sleep_first

    def completion(self, messages, tools=None, **kw) -> LLMResponse:  # type: ignore[override]
        return _make_response("test-recording")

    async def acompletion(self, messages, tools=None, **kw) -> LLMResponse:  # type: ignore[override]
        self._calls.append(list(messages))
        if len(self._calls) == 1:
            await asyncio.sleep(self._sleep_first)
        return _make_response("test-recording")

    def resume_messages(self) -> list[Message]:
        """Messages of the first call after the interrupt."""
        assert len(self._calls) >= 2, "expected a resumed completion call"
        return self._calls[1]


async def _interrupt_between_turns(conv: LocalConversation, llm: RecordingLLM):
    """Run, interrupt mid-LLM-call, and wait for arun() to settle."""
    task = asyncio.create_task(conv.arun())
    await asyncio.sleep(0.05)
    assert len(llm._calls) == 1, "the first completion call should be in flight"
    conv.interrupt()
    await asyncio.wait_for(task, timeout=5.0)


def _environment_notice_texts(conv: LocalConversation) -> list[str]:
    """Text of every environment-sourced MessageEvent in the history."""
    from agentrt.sdk.event import MessageEvent

    return [
        part
        for event in conv.state.events
        if isinstance(event, MessageEvent) and event.source == "environment"
        for part in content_to_str(event.llm_message.content)
    ]


@pytest.mark.asyncio
async def test_interrupt_between_turns_is_visible_to_the_resumed_llm(tmp_path):
    """The whole point of the item: the agent learns it was interrupted.

    Before the fix the resumed context was the original prompt and the new
    one, with nothing in between saying the first run had been cut short.
    """
    llm = RecordingLLM()
    conv = _make_conversation(llm, tmp_path)

    await _interrupt_between_turns(conv, llm)
    assert conv.state.execution_status == ConversationExecutionStatus.PAUSED

    conv.send_message("continue")
    await asyncio.wait_for(conv.arun(), timeout=10.0)

    resumed = llm.resume_messages()
    blob = " ".join(
        part for message in resumed for part in content_to_str(message.content)
    )
    assert "interrupted" in blob.lower(), (
        f"resumed context carries no record of the interrupt: {blob!r}"
    )


@pytest.mark.asyncio
async def test_interrupt_notice_is_llm_convertible(tmp_path):
    """The notice must survive the event → message projection.

    The gap existed because InterruptEvent is not an LLMConvertibleEvent, so
    assert the replacement actually converts, rather than trusting that a
    MessageEvent is enough by construction.
    """
    from agentrt.sdk.event import MessageEvent
    from agentrt.sdk.event.base import LLMConvertibleEvent

    llm = RecordingLLM()
    conv = _make_conversation(llm, tmp_path)
    await _interrupt_between_turns(conv, llm)

    notices: list[LLMConvertibleEvent] = [
        event
        for event in conv.state.events
        if isinstance(event, MessageEvent) and event.source == "environment"
    ]
    assert len(notices) == 1

    messages = LLMConvertibleEvent.events_to_messages(notices)
    assert len(messages) == 1
    assert messages[0].role == "user"
    assert "interrupted" in " ".join(content_to_str(messages[0].content)).lower()


@pytest.mark.asyncio
async def test_interrupt_notice_is_not_mistaken_for_an_acp_prompt(tmp_path):
    """The notice must not be read as an ACP prompt cursor.

    ``_is_acp_prompt_message`` accepts environment/user MessageEvents whose
    text starts with the stop-hook prefix. The notice shares its shape, so a
    prefix collision would corrupt prompt tracking.
    """
    from agentrt.sdk.conversation.impl.local_conversation import (
        _is_acp_prompt_message,
    )

    llm = RecordingLLM()
    conv = _make_conversation(llm, tmp_path)
    await _interrupt_between_turns(conv, llm)

    from agentrt.sdk.event import MessageEvent

    notices = [
        event
        for event in conv.state.events
        if isinstance(event, MessageEvent) and event.source == "environment"
    ]
    assert notices and not any(_is_acp_prompt_message(e) for e in notices)


@pytest.mark.asyncio
async def test_no_notice_when_a_new_message_superseded_the_prompt(tmp_path):
    """A superseding user message is the context; a notice would be noise.

    This is the path the agent-server takes when send_message arrives during
    an in-flight ACP prompt (event_service.interrupt(internal_acp_rerun=True)).
    It reaches the same CancelledError handler, so the gate has to be here.
    """
    from agentrt.sdk.conversation.impl.local_conversation import (
        ACP_SUPERSEDE_INFLIGHT_PROMPT,
    )

    llm = RecordingLLM()
    conv = _make_conversation(llm, tmp_path)

    task = asyncio.create_task(conv.arun())
    await asyncio.sleep(0.05)
    assert len(llm._calls) == 1

    # What event_service sets before interrupting the superseded prompt.
    with conv._state:
        conv._state.agent_state = {
            **conv._state.agent_state,
            ACP_SUPERSEDE_INFLIGHT_PROMPT: True,
        }
    conv.interrupt()
    await asyncio.wait_for(task, timeout=5.0)

    assert conv.state.execution_status == ConversationExecutionStatus.PAUSED
    assert _environment_notice_texts(conv) == []


@pytest.mark.asyncio
async def test_no_notice_when_the_run_had_already_finished(tmp_path):
    """An interrupt landing after FINISHED cut nothing short.

    ``completed_cancelled_prompt`` is the race where cancellation arrives as
    the run completes; claiming the run was interrupted before it finished
    would be false.
    """
    llm = RecordingLLM()
    conv = _make_conversation(llm, tmp_path)

    task = asyncio.create_task(conv.arun())
    await asyncio.sleep(0.05)
    assert len(llm._calls) == 1

    conv._state.execution_status = ConversationExecutionStatus.FINISHED
    conv.interrupt()
    await asyncio.wait_for(task, timeout=5.0)

    assert _environment_notice_texts(conv) == []


@pytest.mark.asyncio
async def test_notice_does_not_claim_the_user_interrupted(tmp_path):
    """Teardown reaches the same handler, so the wording must not name a cause.

    ``EventService.close()`` pauses and then cancels the run task -- on server
    shutdown and on conversation delete -- which lands in this handler with
    both gates false. Naming the user there writes a false account into the
    history of exactly the resumable sessions this notice exists to help.
    """
    llm = RecordingLLM()
    conv = _make_conversation(llm, tmp_path)

    task = asyncio.create_task(conv.arun())
    await asyncio.sleep(0.05)
    assert len(llm._calls) == 1

    # What EventService.close() does to drain an in-flight run.
    conv.pause()
    task.cancel()
    await asyncio.wait_for(task, timeout=5.0)

    notices = _environment_notice_texts(conv)
    assert len(notices) == 1, "teardown should still record that it happened"
    assert "the user" not in notices[0].lower(), (
        f"notice attributes the interrupt to a user that did not ask: {notices[0]!r}"
    )
    assert "interrupted" in notices[0].lower()


@pytest.mark.asyncio
async def test_no_notice_when_a_tool_call_was_left_unanswered(tmp_path):
    """The orphan backfill is already LLM-visible; a notice would double up.

    Drives the real handler: the orphan is planted while the completion call
    is in flight, so the interrupt finds exactly what a tool call in flight
    looks like to it.
    """
    from agentrt.sdk.event import ActionEvent

    llm = RecordingLLM()
    conv = _make_conversation(llm, tmp_path)

    task = asyncio.create_task(conv.arun())
    await asyncio.sleep(0.05)
    assert len(llm._calls) == 1

    with conv._state:
        conv._on_event(
            ActionEvent(
                source="agent",
                thought=[TextContent(text="calling a tool")],
                action=_InterruptProbeAction(command="test"),
                tool_call=MessageToolCall(
                    id="tc-orphan",
                    name="my_tool",
                    arguments='{"command": "test"}',
                    origin="completion",
                ),
                tool_name="my_tool",
                tool_call_id="tc-orphan",
                llm_response_id="resp-1",
            )
        )

    conv.interrupt()
    await asyncio.wait_for(task, timeout=5.0)

    assert conv.state.execution_status == ConversationExecutionStatus.PAUSED
    # Backfilled rather than annotated...
    assert any(isinstance(e, AgentErrorEvent) for e in conv.state.events)
    # ...and the AgentErrorEvent is the only thing telling the agent.
    assert _environment_notice_texts(conv) == []


def test_orphaned_action_errors_reports_whether_it_backfilled(tmp_path):
    """The gate input: True only when an orphan was actually backfilled.

    With nothing in flight the method used to return nothing at all, which is
    why the interrupt went unrecorded. It now reports what it did so the
    caller can tell the two cases apart.
    """
    from agentrt.sdk.event import ActionEvent

    llm = RecordingLLM()
    conv = _make_conversation(llm, tmp_path)

    with conv._state:
        # No orphan yet.
        assert conv._emit_orphaned_action_errors() is False

        # An executable ActionEvent (get_unmatched_actions ignores events
        # with action=None) with no matching observation.
        conv._on_event(
            ActionEvent(
                source="agent",
                thought=[TextContent(text="calling a tool")],
                action=_InterruptProbeAction(command="test"),
                tool_call=MessageToolCall(
                    id="tc-orphan",
                    name="my_tool",
                    arguments='{"command": "test"}',
                    origin="completion",
                ),
                tool_name="my_tool",
                tool_call_id="tc-orphan",
                llm_response_id="resp-1",
            )
        )
        assert conv._emit_orphaned_action_errors() is True

    # The backfill is itself LLM-visible, which is why no notice is added on
    # top of it in the real handler.
    assert any(isinstance(e, AgentErrorEvent) for e in conv.state.events)


@pytest.mark.asyncio
async def test_cancelling_during_agent_init_settles_like_a_mid_step_cancel(
    tmp_path,
):
    """The window above the run loop is now handled, not propagated.

    Lazy init awaits before the loop's try block, so a cancel landing there used
    to escape arun() entirely: status left unset, no InterruptEvent, nothing in
    the agent's own history, and the run task never cleared. The window is not
    short -- init loads plugins and, for ACP, resolves credentials through a
    blocking lookup.
    """
    llm = RecordingLLM(sleep_first=0.0)
    conv = _make_conversation(llm, tmp_path)

    started = asyncio.Event()

    def slow_ready() -> None:
        import time as _time

        started.set()
        _time.sleep(5.0)

    conv._ensure_agent_ready = slow_ready  # type: ignore[method-assign]

    task = asyncio.create_task(conv.arun())
    while not started.is_set():
        await asyncio.sleep(0.01)
    task.cancel()
    await asyncio.wait_for(task, timeout=10.0)

    assert conv.state.execution_status == ConversationExecutionStatus.PAUSED
    assert conv._arun_task is None, "the run task must still be cleared"
    events = list(conv.state.events)
    assert sum(isinstance(e, InterruptEvent) for e in events) == 1
    notices = _environment_notice_texts(conv)
    assert len(notices) == 1, notices
    assert "interrupted" in notices[0].lower()
