"""H8 item 7: a readonly/inspect session's write channel outside its
workspace, added in ``guarded_tools.py`` and read back through
``client.artifacts`` (covered separately in ``test_h2_client_contract.py``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from agentrt.runtime import guarded_tools, permissions
from agentrt.tools.file_editor.definition import FileEditorAction


class _RecordingInner:
    """Stands in for the vendored `FileEditorExecutor`: records the action it
    was handed and returns it unchanged, so a test can see exactly what path
    reached "the real editor" after `_approve` ran."""

    def __init__(self) -> None:
        self.received: FileEditorAction | None = None

    def __call__(self, action: FileEditorAction, conversation: Any = None):
        self.received = action
        return action


def _executor(
    tmp_path: Path, *, permission: permissions.Permission, with_reports: bool
) -> tuple[guarded_tools.GuardedFileEditorExecutor, _RecordingInner, Path, Path | None]:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    reports = None
    reports_root = None
    if with_reports:
        reports = tmp_path / "reports"
        reports.mkdir()
        reports_root = str(reports)
    inner = _RecordingInner()
    executor = guarded_tools.GuardedFileEditorExecutor(
        inner, str(workspace), permission, reports_root
    )
    return executor, inner, workspace, reports


def test_readonly_session_can_write_inside_its_reports_root(tmp_path: Path) -> None:
    """A model writes a report using the absolute path its tool description
    handed it (`reports_root`), not a relative one."""
    executor, inner, _workspace, reports = _executor(
        tmp_path, permission="readonly", with_reports=True
    )
    assert reports is not None
    action = FileEditorAction(
        command="create", path=str(reports / "findings.md"), file_text="hi"
    )

    result = executor(action)

    assert inner.received is not None
    assert Path(inner.received.path) == reports / "findings.md"
    assert not getattr(result, "is_error", False)


def test_readonly_session_relative_write_is_still_refused_not_redirected(
    tmp_path: Path,
) -> None:
    """A relative path always resolves inside whatever root it is checked
    against, so it must not be tried against the reports root -- that would
    silently redirect an ordinary refused write instead of refusing it."""
    executor, inner, _workspace, _reports = _executor(
        tmp_path, permission="readonly", with_reports=True
    )
    action = FileEditorAction(command="create", path="findings.md", file_text="hi")

    result = executor(action)

    assert inner.received is None
    assert result.is_error


def test_readonly_session_absolute_write_outside_both_roots_is_refused(
    tmp_path: Path,
) -> None:
    """An absolute path is eligible for the reports-root fallback, but only
    when it actually resolves inside it -- an absolute path elsewhere is
    still refused."""
    executor, inner, _workspace, _reports = _executor(
        tmp_path, permission="readonly", with_reports=True
    )
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    action = FileEditorAction(
        command="create", path=str(elsewhere / "not_a_report.py"), file_text="x"
    )

    result = executor(action)

    assert inner.received is None
    assert result.is_error


def test_readonly_session_view_still_reaches_its_workspace(tmp_path: Path) -> None:
    """`view` is not a write, so it is unaffected by the reports root and
    still resolves against the workspace as before."""
    executor, inner, workspace, _reports = _executor(
        tmp_path, permission="readonly", with_reports=True
    )
    (workspace / "existing.py").write_text("print(1)\n")
    action = FileEditorAction(command="view", path="existing.py")

    executor(action)

    assert inner.received is not None
    assert Path(inner.received.path) == workspace / "existing.py"


def test_no_reports_root_behaves_exactly_as_before(tmp_path: Path) -> None:
    executor, inner, _workspace, _reports = _executor(
        tmp_path, permission="readonly", with_reports=False
    )
    action = FileEditorAction(command="create", path="anything.md", file_text="x")

    result = executor(action)

    assert inner.received is None
    assert result.is_error


def test_workspace_permission_session_writes_its_own_workspace_unaffected(
    tmp_path: Path,
) -> None:
    """`workspace`/`broad` sessions never get a reports root (see
    `GuardedFileEditorTool.create`); this checks the executor's own logic
    still writes straight to the workspace when `reports_root` is `None`."""
    executor, inner, workspace, _reports = _executor(
        tmp_path, permission="workspace", with_reports=False
    )
    action = FileEditorAction(command="create", path="out.py", file_text="x=1")

    executor(action)

    assert inner.received is not None
    assert Path(inner.received.path) == workspace / "out.py"


class _FakeWorkspace:
    def __init__(self, working_dir: str) -> None:
        self.working_dir = working_dir


class _FakeLLM:
    def vision_is_active(self) -> bool:
        return False


class _FakeAgent:
    def __init__(self) -> None:
        self.llm = _FakeLLM()


class _FakeConvState:
    """The subset of `ConversationState` `GuardedFileEditorTool.create`
    (via `FileEditorTool.create`) reads: `workspace.working_dir`,
    `agent.llm.vision_is_active()` and `persistence_dir`."""

    def __init__(self, working_dir: str, persistence_dir: str | None) -> None:
        self.workspace = _FakeWorkspace(working_dir)
        self.agent = _FakeAgent()
        self.persistence_dir = persistence_dir


@pytest.mark.parametrize("preset", ["readonly", "inspect"])
def test_create_provisions_reports_root_for_readonly_presets(
    tmp_path: Path, preset: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    persistence = tmp_path / "persist" / "conv-hex"
    conv_state = _FakeConvState(str(workspace), str(persistence))

    tools = guarded_tools.GuardedFileEditorTool.create(conv_state, permission=preset)

    assert len(tools) == 1
    executor = tools[0].executor
    assert isinstance(executor, guarded_tools.GuardedFileEditorExecutor)
    assert executor._reports_root == str(persistence / "reports")
    assert (persistence / "reports").is_dir()
    assert str(persistence / "reports") in (tools[0].description or "")


@pytest.mark.parametrize("preset", ["workspace", "broad"])
def test_create_gives_writable_presets_no_reports_root(
    tmp_path: Path, preset: str
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    persistence = tmp_path / "persist" / "conv-hex"
    conv_state = _FakeConvState(str(workspace), str(persistence))

    tools = guarded_tools.GuardedFileEditorTool.create(conv_state, permission=preset)

    executor = tools[0].executor
    assert isinstance(executor, guarded_tools.GuardedFileEditorExecutor)
    assert executor._reports_root is None
    assert not (persistence / "reports").exists()


def test_create_without_persistence_dir_gives_no_reports_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    conv_state = _FakeConvState(str(workspace), None)

    tools = guarded_tools.GuardedFileEditorTool.create(
        conv_state, permission="readonly"
    )

    executor = tools[0].executor
    assert isinstance(executor, guarded_tools.GuardedFileEditorExecutor)
    assert executor._reports_root is None
