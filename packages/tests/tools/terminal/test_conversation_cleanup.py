"""
Tests for proper cleanup of tool executors in conversations.

This test suite verifies that tool executors are properly cleaned up
when conversations are closed or destroyed.
"""

import tempfile
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, ClassVar, Literal
from unittest.mock import Mock

import pytest

from agentrt.sdk import Agent, Conversation
from agentrt.sdk.conversation.state import ConversationExecutionStatus
from agentrt.sdk.tool import Tool, ToolExecutor, register_tool
from agentrt.tools.terminal import TerminalExecutor, TerminalTool


if TYPE_CHECKING:
    from agentrt.sdk.conversation.state import ConversationState


class _InjectedExecutorTerminalTool(TerminalTool):
    """TerminalTool variant that injects a test-provided executor at resolve time.

    ``TerminalTool.create`` needs ``conv_state`` (for the working dir), so this
    subclass builds the real tool from ``conv_state`` and then swaps in the
    executor captured on the class attribute. Each test sets ``injected_executor``
    before registering this subclass.
    """

    injected_executor: ClassVar[ToolExecutor | None] = None

    @classmethod
    def create(
        cls,
        conv_state: "ConversationState",
        username: str | None = None,
        no_change_timeout_seconds: int | None = None,
        terminal_type: Literal["tmux", "subprocess", "powershell"] | None = None,
        shell_path: str | None = None,
        executor: ToolExecutor | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> Sequence[TerminalTool]:
        tools = TerminalTool.create(
            conv_state,
            username=username,
            no_change_timeout_seconds=no_change_timeout_seconds,
            terminal_type=terminal_type,
            shell_path=shell_path,
            executor=executor,
            env=env,
        )
        return [
            tool.model_copy(update={"executor": cls.injected_executor})
            for tool in tools
        ]


def test_conversation_close_calls_executor_close(mock_llm):
    """Test that Conversation.close() calls close() on all tool executors."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create a TerminalExecutor with subprocess terminal to avoid tmux issues
        terminal_executor = TerminalExecutor(
            working_dir=temp_dir, terminal_type="subprocess"
        )
        terminal_executor.close = Mock()

        _InjectedExecutorTerminalTool.injected_executor = terminal_executor
        register_tool("test_terminal", _InjectedExecutorTerminalTool)

        # Create agent and conversation
        agent = Agent(
            llm=mock_llm,
            tools=[Tool(name="test_terminal")],
        )
        conversation = Conversation(
            agent=agent, workspace=temp_dir, delete_on_close=True
        )

        # Trigger lazy agent initialization to create tools
        conversation._ensure_agent_ready()

        # Close the conversation
        conversation.close()

        # Verify that the executor's close method was called
        terminal_executor.close.assert_called_once()


def test_conversation_close_calls_executor_close_without_delete(mock_llm):
    """Executors are closed even when delete_on_close=False."""
    with tempfile.TemporaryDirectory() as temp_dir:
        terminal_executor = TerminalExecutor(
            working_dir=temp_dir, terminal_type="subprocess"
        )
        terminal_executor.close = Mock()

        _InjectedExecutorTerminalTool.injected_executor = terminal_executor
        register_tool("test_terminal", _InjectedExecutorTerminalTool)

        agent = Agent(
            llm=mock_llm,
            tools=[Tool(name="test_terminal")],
        )
        conversation = Conversation(
            agent=agent, workspace=temp_dir, delete_on_close=False
        )
        conversation._ensure_agent_ready()
        conversation.close()

        terminal_executor.close.assert_called_once()


def test_conversation_del_calls_close(mock_llm):
    """Test that Conversation.__del__() calls close()."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create a TerminalExecutor with subprocess terminal to avoid tmux issues
        terminal_executor = TerminalExecutor(
            working_dir=temp_dir, terminal_type="subprocess"
        )
        terminal_executor.close = Mock()

        _InjectedExecutorTerminalTool.injected_executor = terminal_executor
        register_tool("test_terminal", _InjectedExecutorTerminalTool)

        # Create agent and conversation
        agent = Agent(
            llm=mock_llm,
            tools=[Tool(name="test_terminal")],
        )
        conversation = Conversation(
            agent=agent, workspace=temp_dir, delete_on_close=True
        )

        # Trigger lazy agent initialization to create tools
        conversation._ensure_agent_ready()

        # Manually call __del__ to simulate garbage collection
        conversation.__del__()

        # Verify that the executor's close method was called
        terminal_executor.close.assert_called_once()


def test_conversation_close_handles_executor_exceptions(mock_llm):
    """Test that Conversation.close() handles exceptions from executor.close()."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create a mock LLM to avoid actual API calls

        # Create a TerminalExecutor with subprocess terminal and make its close method
        # raise an exception
        terminal_executor = TerminalExecutor(
            working_dir=temp_dir, terminal_type="subprocess"
        )
        terminal_executor.close = Mock(side_effect=Exception("Test exception"))

        _InjectedExecutorTerminalTool.injected_executor = terminal_executor
        register_tool("test_terminal", _InjectedExecutorTerminalTool)

        # Create agent and conversation
        agent = Agent(
            llm=mock_llm,
            tools=[Tool(name="test_terminal")],
        )
        conversation = Conversation(agent=agent, workspace=temp_dir)

        # Close should not raise an exception even if executor.close() fails
        # We can see from the captured stderr that the warning is logged correctly
        conversation.close()  # This should not raise an exception


def test_conversation_close_skips_none_executors(mock_llm):
    """Test that Conversation.close() skips tools with None executors."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create a mock LLM to avoid actual API calls

        # Create a tool with no executor
        _InjectedExecutorTerminalTool.injected_executor = None
        register_tool("test_terminal", _InjectedExecutorTerminalTool)

        # Create agent and conversation
        agent = Agent(
            llm=mock_llm,
            tools=[Tool(name="test_terminal")],
        )
        conversation = Conversation(agent=agent, workspace=temp_dir)

        # This should not raise an exception
        conversation.close()


def test_terminal_executor_close_calls_session_close():
    """Test that TerminalExecutor.close() calls session.close()."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create a TerminalExecutor with subprocess terminal
        terminal_executor = TerminalExecutor(
            working_dir=temp_dir, terminal_type="subprocess"
        )

        # Mock the session's close method
        terminal_executor.session.close = Mock()

        # Call close on the executor
        terminal_executor.close()

        # Verify that session.close() was called
        terminal_executor.session.close.assert_called_once()


def test_terminal_executor_close_handles_missing_session():
    """Test that TerminalExecutor.close() handles missing session attribute."""
    with tempfile.TemporaryDirectory() as temp_dir:
        # Create a TerminalExecutor with subprocess terminal
        terminal_executor = TerminalExecutor(
            working_dir=temp_dir, terminal_type="subprocess"
        )

        # Clear the session to simulate a missing/uninitialized state
        terminal_executor._session = None

        # This should not raise an exception
        terminal_executor.close()


# --------------------------------------------------------------------------
# #17: a run's end releases the shell, and the next command gets a new one.
# --------------------------------------------------------------------------


def test_reap_closes_the_shell_and_the_next_command_builds_a_new_one():
    """reap() closes the process without disabling the executor."""
    from agentrt.tools.terminal import TerminalAction

    with tempfile.TemporaryDirectory() as temp_dir:
        executor = TerminalExecutor(working_dir=temp_dir, terminal_type="subprocess")
        first_terminal = executor.session.terminal
        first_process = first_terminal.process

        executor.reap()

        assert first_process is not None
        assert first_process.poll() is not None, "the shell survived reap()"

        observation = executor(TerminalAction(command="echo alive"))

        assert observation.exit_code == 0, observation.text
        assert executor.session.terminal is not first_terminal, "no fresh shell"
        assert "terminal was reset" in observation.text


def _scripted_agent(tools: list[Tool]):
    """An LLM-free agent: one scripted reply per step, then the run finishes."""
    from pydantic import PrivateAttr, SecretStr

    from agentrt.sdk.agent.base import AgentBase
    from agentrt.sdk.conversation.types import (
        ConversationCallbackType,
        ConversationTokenCallbackType,
    )
    from agentrt.sdk.event.llm_convertible import MessageEvent, SystemPromptEvent
    from agentrt.sdk.llm import LLM, Message, TextContent

    class _ScriptedAgent(AgentBase):
        _replies: list[str] = PrivateAttr(default_factory=list)
        _calls: int = PrivateAttr(default=0)

        def __init__(self, replies: list[str]) -> None:
            super().__init__(
                llm=LLM(
                    model="gpt-4o-mini",
                    api_key=SecretStr("test-key"),
                    usage_id="test-llm",
                ),
                tools=tools,
            )
            self._replies = replies

        def init_state(self, state, on_event: ConversationCallbackType) -> None:  # noqa: ARG002
            on_event(
                SystemPromptEvent(
                    source="agent", system_prompt=TextContent(text="dummy"), tools=[]
                )
            )

        def step(
            self,
            conversation,
            on_event: ConversationCallbackType,
            on_token: ConversationTokenCallbackType | None = None,
        ) -> None:
            reply = self._replies[min(self._calls, len(self._replies) - 1)]
            self._calls += 1
            on_event(
                MessageEvent(
                    source="agent",
                    llm_message=Message(
                        role="assistant", content=[TextContent(text=reply)]
                    ),
                )
            )
            conversation.state.execution_status = ConversationExecutionStatus.FINISHED

    return _ScriptedAgent(["answer one"])


@pytest.mark.asyncio
async def test_a_finished_run_closes_the_sessions_shell(monkeypatch):
    """The run's end reaps the terminal, and the next command still works.

    Before #17 the shell outlived the run until the whole conversation was
    closed, so a fleet running sessions for hours kept one shell per session
    that had run -- on Windows, a PowerShell process each.
    """
    monkeypatch.delenv("AGENTRT_TERMINAL_REAP_AFTER_RUN", raising=False)
    # Force the single-shell backend: pool mode has no one session to watch.
    monkeypatch.setattr("agentrt.tools.terminal.impl._is_tmux_available", lambda: False)
    monkeypatch.setattr(
        "agentrt.tools.terminal.terminal.factory._is_tmux_available", lambda: False
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        conversation = Conversation(
            agent=_scripted_agent([Tool(name=TerminalTool.name)]),
            workspace=temp_dir,
        )
        conversation._ensure_agent_ready()
        tool = conversation.agent.tools_map[TerminalTool.name]
        executor = tool.as_executable().executor
        first_process = executor.session.terminal.process

        conversation.send_message("question one")
        await conversation.arun()

        assert first_process is not None
        assert first_process.poll() is not None, (
            "the run ended but its shell is still alive"
        )

        from agentrt.tools.terminal import TerminalAction

        observation = executor(TerminalAction(command="echo still works"))
        assert observation.exit_code == 0, observation.text


@pytest.mark.asyncio
async def test_reap_after_run_can_be_turned_off(monkeypatch):
    """AGENTRT_TERMINAL_REAP_AFTER_RUN=0 keeps the shell across runs."""
    monkeypatch.setenv("AGENTRT_TERMINAL_REAP_AFTER_RUN", "0")
    monkeypatch.setattr("agentrt.tools.terminal.impl._is_tmux_available", lambda: False)
    monkeypatch.setattr(
        "agentrt.tools.terminal.terminal.factory._is_tmux_available", lambda: False
    )

    with tempfile.TemporaryDirectory() as temp_dir:
        conversation = Conversation(
            agent=_scripted_agent([Tool(name=TerminalTool.name)]),
            workspace=temp_dir,
        )
        conversation._ensure_agent_ready()
        tool = conversation.agent.tools_map[TerminalTool.name]
        executor = tool.as_executable().executor
        first_process = executor.session.terminal.process

        conversation.send_message("question one")
        await conversation.arun()

        assert first_process is not None
        assert first_process.poll() is None, "the shell was reaped despite the flag"
        executor.close()


@pytest.mark.asyncio
async def test_a_reap_that_blocks_does_not_hold_the_event_loop(monkeypatch):
    """#22: a close() that never returns froze the daemon's event loop."""
    import asyncio
    import threading

    monkeypatch.delenv("AGENTRT_TERMINAL_REAP_AFTER_RUN", raising=False)
    monkeypatch.setattr(
        "agentrt.sdk.conversation.impl.local_conversation._REAP_DEADLINE_SECONDS",
        0.5,
    )
    release = threading.Event()

    with tempfile.TemporaryDirectory() as temp_dir:
        conversation = Conversation(
            agent=_scripted_agent([Tool(name=TerminalTool.name)]),
            workspace=temp_dir,
        )
        conversation._ensure_agent_ready()
        executor = conversation.agent.tools_map[TerminalTool.name].as_executable()
        monkeypatch.setattr(
            executor.executor, "reap", lambda: release.wait(10), raising=False
        )

        conversation.send_message("question one")
        try:
            await asyncio.wait_for(conversation.arun(), timeout=5)
        finally:
            release.set()
            executor.executor.close()
