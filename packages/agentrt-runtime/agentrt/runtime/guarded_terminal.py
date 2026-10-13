from __future__ import annotations

import os
import re
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

from agentrt.runtime import config
from agentrt.sdk.llm import TextContent
from agentrt.sdk.logger import get_logger
from agentrt.sdk.tool import ToolExecutor, register_tool
from agentrt.tools.terminal import TerminalTool
from agentrt.tools.terminal.definition import TerminalAction, TerminalObservation


if TYPE_CHECKING:
    from agentrt.sdk.conversation.state import ConversationState


logger = get_logger(__name__)

_VALUE_LINE = re.compile(r"^[^#=\s][^=]*=(.*)$")
_DEFAULT_MAX_SECONDS = 1800
#: Slack on top of the command limit before the call is abandoned. A command
#: that is at its limit needs a moment to unwind; one that is stuck never will.
_DEADLINE_GRACE_SECONDS = 15.0
#: How long an interrupted call is given to return before it is abandoned.
_INTERRUPT_GRACE_SECONDS = 10.0
_ABANDONED_TEXT = (
    "[Stopped by AgentRT: the terminal command did not return within "
    "{limit:g} seconds, so the call was abandoned and the terminal was reset. "
    "The command's result is unknown; do not repeat it blindly.]"
)


def _values_from_file(path: Path) -> set[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return set()

    values = set()
    has_assignments = False
    for line in text.splitlines():
        match = _VALUE_LINE.match(line.strip())
        if match:
            has_assignments = True
            value = match.group(1).strip().strip("'\"")
            if len(value) >= 8:
                values.add(value)
    if (
        not has_assignments
        and "\n" not in text.rstrip("\r\n")
        and len(text.strip()) >= 8
    ):
        values.add(text.strip())
    return values


def _workspace_secret_files(root: Path) -> set[Path]:
    files = set(root.glob(".env")) | set(root.glob(".env.*"))
    for path in root.iterdir():
        if path.is_file() and any(
            marker in path.name.lower() for marker in ("secret", "passphrase")
        ):
            files.add(path)
    return {
        path for path in files if path.is_file() and path.name.lower() != ".env.example"
    }


def _secret_values(workspace_root: str) -> set[str]:
    values = _values_from_file(config.config_file())
    root = Path(workspace_root)
    try:
        paths = _workspace_secret_files(root)
    except OSError:
        paths = set()
    for path in paths:
        values.update(_values_from_file(path))
    return values


def _max_seconds() -> float:
    raw = os.environ.get("AGENTRT_TERMINAL_MAX_SECONDS")
    if raw is None:
        return _DEFAULT_MAX_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return _DEFAULT_MAX_SECONDS
    return max(0.0, value)


def _redact(text: str, values: set[str]) -> str:
    for value in sorted(values, key=len, reverse=True):
        text = text.replace(value, "<redacted>")
    return text


class GuardedTerminalExecutor(ToolExecutor):
    def __init__(self, inner: ToolExecutor, workspace_root: str) -> None:
        self._inner = inner
        self._workspace_root = workspace_root

    def __call__(
        self,
        action: TerminalAction,
        conversation: Any = None,
    ) -> TerminalObservation:
        limit = _max_seconds()
        # The limit is a maximum (#15): it also caps a timeout the model chose.
        # A model asking for a longer one got no limit at all, and a command
        # that never returns (an unclosed quote leaves the shell waiting for
        # input) then held the session for as long as it asked, with the
        # no-output check switched off, which a timeout does.
        injected_timeout = (
            (action.timeout is None or action.timeout > limit)
            and limit > 0
            and not action.is_input
            and bool(action.command.strip())
        )
        if injected_timeout:
            action = action.model_copy(update={"timeout": limit})

        observation = self._bounded_call(action, conversation, limit)
        secret_values = _secret_values(self._workspace_root)
        text = _redact(observation.text, secret_values)
        metadata = observation.metadata.model_copy(
            update={
                field: _redact(getattr(observation.metadata, field), secret_values)
                for field in ("prefix", "suffix", "working_dir", "py_interpreter_path")
                if getattr(observation.metadata, field) is not None
            }
        )
        if text != observation.text or metadata != observation.metadata:
            observation = observation.model_copy(
                update={"content": [TextContent(text=text)], "metadata": metadata}
            )

        timeout_text = f"timed out after {limit:g} seconds"
        timed_out = (
            observation.timeout
            or timeout_text in text
            or timeout_text in metadata.suffix
        )
        if injected_timeout and timed_out:
            note = f"[Stopped by AgentRT's {limit:g}-second terminal limit.]"
            observation = observation.model_copy(
                update={
                    "content": [
                        *observation.content,
                        TextContent(text=f"\n{note}"),
                    ]
                }
            )
        return observation

    def _bounded_call(
        self,
        action: TerminalAction,
        conversation: Any,
        limit: float,
    ) -> TerminalObservation:
        """Run the command, and never hold the run past its deadline.

        The wall-clock limit lives *inside* the session loop, and that loop is
        what is stuck when a shell exits without leaving its prompt marker (a
        command ending in `exit $rc`) or when a backend read never returns. The
        operator's sessions then sat `running` with no events for hours while
        `interrupt` and `delete` timed out at 120 s (#15, #10). Waiting out
        here means the deadline holds wherever the backend blocks.
        """
        if limit <= 0 or action.is_input:
            return self._inner(action, conversation)

        outcome: dict[str, Any] = {}
        finished = threading.Event()

        def _run() -> None:
            try:
                outcome["observation"] = self._inner(action, conversation)
            except BaseException as exc:  # raised again on the caller's thread
                outcome["error"] = exc
            finally:
                finished.set()

        threading.Thread(target=_run, name="agentrt-terminal-call", daemon=True).start()

        if finished.wait(limit + _DEADLINE_GRACE_SECONDS):
            error = outcome.get("error")
            if error is not None:
                raise error
            return cast(TerminalObservation, outcome["observation"])

        logger.warning(
            "Terminal command did not return within %g s; interrupting it",
            limit,
        )
        self.interrupt()
        if finished.wait(_INTERRUPT_GRACE_SECONDS) and "observation" in outcome:
            return cast(TerminalObservation, outcome["observation"])

        # Closing the shell is what unblocks a read that will never return.
        self.reap()
        return TerminalObservation.from_text(
            text=_ABANDONED_TEXT.format(limit=limit),
            is_error=True,
            command=action.command or "[timed out]",
            exit_code=-1,
        )

    def interrupt(self) -> None:
        """Delegate: an interrupt that stops at the guard never stops the shell.

        ``ParallelToolExecutor`` calls ``tool.executor.interrupt()`` when a tool
        call is cancelled, and on the daemon path the tool's executor is this
        guard -- so without the delegation the terminal command kept running
        and the conversation could not be cancelled or deleted (#15).
        """
        interrupt = getattr(self._inner, "interrupt", None)
        if callable(interrupt):
            interrupt()

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()

    def reap(self) -> None:
        """Delegate the between-runs reap to the wrapped executor (#17)."""
        reap = getattr(self._inner, "reap", None)
        if callable(reap):
            reap()


class GuardedTerminalTool(TerminalTool):
    @classmethod
    def create(
        cls,
        conv_state: ConversationState,
        username: str | None = None,
        no_change_timeout_seconds: int | None = None,
        terminal_type: Literal["tmux", "subprocess", "powershell"] | None = None,
        shell_path: str | None = None,
        executor: ToolExecutor | None = None,
        *,
        env: Mapping[str, str] | None = None,
    ) -> Sequence[GuardedTerminalTool]:
        tools = super().create(
            conv_state,
            username=username,
            no_change_timeout_seconds=no_change_timeout_seconds,
            terminal_type=terminal_type,
            shell_path=shell_path,
            executor=executor,
            env=env,
        )
        guarded = []
        for tool in tools:
            if tool.executor is None:
                raise RuntimeError("terminal was created without an executor")
            description = tool.description or ""
            limit = _max_seconds()
            if limit > 0:
                description += (
                    f"\n\nAgentRT stops commands without an explicit timeout after "
                    f"{limit:g} seconds. Set AGENTRT_TERMINAL_MAX_SECONDS=0 to "
                    "disable this hard limit."
                )
            guarded.append(
                tool.model_copy(
                    update={
                        "executor": GuardedTerminalExecutor(
                            tool.executor, conv_state.workspace.working_dir
                        ),
                        "description": description,
                    }
                )
            )
        return guarded


GuardedTerminalTool.name = TerminalTool.name


def install() -> None:
    register_tool(TerminalTool.name, GuardedTerminalTool)


install()
