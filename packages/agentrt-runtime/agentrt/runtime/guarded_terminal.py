from __future__ import annotations

import os
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from agentrt.runtime import config
from agentrt.sdk.llm import TextContent
from agentrt.sdk.tool import ToolExecutor, register_tool
from agentrt.tools.terminal import TerminalTool
from agentrt.tools.terminal.definition import TerminalAction, TerminalObservation


if TYPE_CHECKING:
    from agentrt.sdk.conversation.state import ConversationState


_VALUE_LINE = re.compile(r"^[^#=\s][^=]*=(.*)$")
_DEFAULT_MAX_SECONDS = 1800


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
        injected_timeout = (
            action.timeout is None
            and limit > 0
            and not action.is_input
            and bool(action.command.strip())
        )
        if injected_timeout:
            action = action.model_copy(update={"timeout": limit})

        observation = self._inner(action, conversation)
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

    def close(self) -> None:
        close = getattr(self._inner, "close", None)
        if callable(close):
            close()


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
