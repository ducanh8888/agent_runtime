"""H10 item 3: a path refusal is deterministic -- retrying the exact same
path can never succeed. Both guarded executors nudge on the second identical
refusal in a row, ahead of and independent of the SDK's generic stuck
detector (which does not even apply here: this refusal is an
ObservationEvent, not the AgentErrorEvent its action-error scenario
matches).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from agentrt.runtime import guarded_tools, inspect_tools
from agentrt.tools.file_editor.definition import FileEditorAction


class _RecordingInner:
    def __call__(self, action: FileEditorAction, conversation: Any = None):
        return action


def test_file_editor_nudges_only_on_the_second_identical_refusal(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = guarded_tools.GuardedFileEditorExecutor(
        _RecordingInner(), str(workspace), "readonly", None
    )
    outside = str(tmp_path / "elsewhere" / "secret.txt")
    action = FileEditorAction(command="view", path=outside)

    first = executor(action)
    assert "retrying this exact path" not in first.text

    second = executor(action)
    assert "retrying this exact path" in second.text

    third = executor(action)
    assert "retrying this exact path" in third.text


def test_file_editor_nudge_resets_on_a_different_refusal(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = guarded_tools.GuardedFileEditorExecutor(
        _RecordingInner(), str(workspace), "readonly", None
    )
    first_path = str(tmp_path / "one.txt")
    second_path = str(tmp_path / "two.txt")

    executor(FileEditorAction(command="view", path=first_path))
    result = executor(FileEditorAction(command="view", path=second_path))
    assert "retrying this exact path" not in result.text


def test_inspect_nudges_only_on_the_second_identical_refusal(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    executor = inspect_tools.InspectExecutor(str(workspace), "inspect")
    outside = str(tmp_path / "elsewhere")
    action = inspect_tools.InspectAction(command="search", pattern="x", path=outside)

    first = executor(action)
    assert "retrying this exact path" not in first.text

    second = executor(action)
    assert "retrying this exact path" in second.text
