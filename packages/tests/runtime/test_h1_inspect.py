"""H1: the `inspect` preset, its schema-validated reads and its guards.

The adversarial half of this file is deliberate: the preset's value is that it
refuses things, so the tests try to make it do them rather than checking that
the happy path returns text. Every refusal is asserted against the filesystem or
the returned observation, not against a prompt.
"""

from __future__ import annotations

import shutil
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from agentrt.runtime import inspect_tools, permissions
from agentrt.runtime.guarded_tools import GuardedFileEditorExecutor
from agentrt.tools.file_editor.definition import FileEditorAction
from agentrt.tools.file_editor.impl import FileEditorExecutor


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A private state directory, so secret identity is ours to control."""
    state = tmp_path / "state"
    (state / "profiles").mkdir(parents=True)
    monkeypatch.setenv("AGENTRT_STATE_DIR", str(state))
    return state


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "a.py").write_text("import os\nprint('hello world')\n", encoding="utf-8")
    (ws / "b.txt").write_text("hello world\n", encoding="utf-8")
    return ws


def _executor(workspace: Path) -> inspect_tools.InspectExecutor:
    return inspect_tools.InspectExecutor(root=str(workspace), permission="inspect")


# -- preset shape ------------------------------------------------------


def test_inspect_sits_between_readonly_and_workspace() -> None:
    assert permissions.PRESETS == ("readonly", "inspect", "workspace", "broad")
    assert permissions.PRESETS.index("readonly") < permissions.PRESETS.index("inspect")
    assert permissions.PRESETS.index("inspect") < permissions.PRESETS.index("workspace")
    assert "inspect" in permissions.DESCRIPTIONS
    assert permissions.DEFAULT_PERMISSION == "workspace"
    assert permissions.normalise("inspect") == "inspect"


def test_existing_presets_are_preserved() -> None:
    assert permissions.tools_for("readonly") == ["file_editor", "task_tracker"]
    assert permissions.tools_for("workspace") == [
        "terminal",
        "file_editor",
        "task_tracker",
    ]
    assert permissions.tools_for("broad") == [
        "terminal",
        "file_editor",
        "task_tracker",
    ]


def test_inspect_grants_no_terminal_and_one_read_tool() -> None:
    tools = permissions.tools_for("inspect")
    assert tools == ["file_editor", "inspect", "task_tracker"]
    assert "terminal" not in tools


def test_check_path_refuses_writes_for_both_read_only_presets(
    state_dir: Path, workspace: Path
) -> None:
    for preset in ("readonly", "inspect"):
        with pytest.raises(permissions.PermissionDenied):
            permissions.check_path(
                "a.py", root=workspace, permission=preset, writing=True
            )
    # Viewing is still allowed under inspect.
    approved = permissions.check_path(
        "a.py", root=workspace, permission="inspect", writing=False
    )
    assert approved == (workspace / "a.py").resolve()


def test_executor_refuses_any_permission_but_inspect(workspace: Path) -> None:
    with pytest.raises(permissions.PermissionDenied):
        inspect_tools.InspectExecutor(root=str(workspace), permission="readonly")


# -- schema validation -------------------------------------------------


def test_action_rejects_unknown_fields() -> None:
    with pytest.raises(ValueError):
        inspect_tools.InspectAction.model_validate(
            {"command": "search", "pattern": "x", "shell": "rm -rf /"}
        )


def test_action_rejects_option_like_revision_and_unknown_executable() -> None:
    with pytest.raises(ValueError):
        inspect_tools.InspectAction(
            command="git", git_command="log", git_ref="--upload-pack=evil"
        )
    with pytest.raises(ValueError):
        inspect_tools.InspectAction(command="version", executable="rm")
    with pytest.raises(ValueError):
        inspect_tools.InspectAction(command="git", git_command="log", git_ref="a:b")


def test_action_enforces_bounds() -> None:
    with pytest.raises(ValueError):
        inspect_tools.InspectAction(command="search", pattern="x", max_results=10_000)
    with pytest.raises(ValueError):
        inspect_tools.InspectAction(
            command="search", pattern="x" * (inspect_tools.MAX_PATTERN_CHARS + 1)
        )


# -- search guards -----------------------------------------------------


def test_search_finds_a_match_with_context(state_dir: Path, workspace: Path) -> None:
    obs = _executor(workspace)(
        inspect_tools.InspectAction(
            command="search", pattern="hello", include="*.py", context_lines=1
        )
    )
    assert obs.is_error is False
    assert obs.match_count == 1
    match = obs.matches[0]
    assert match.path == "a.py"
    assert match.line == 2
    assert match.context_before == ["import os"]


def test_search_refuses_a_symlink_that_leaves_the_workspace(
    state_dir: Path, workspace: Path, tmp_path: Path
) -> None:
    outside = tmp_path / "outside.txt"
    outside.write_text("OUTSIDE-ONLY-CONTENT", encoding="utf-8")
    (workspace / "link.txt").symlink_to(outside)

    obs = _executor(workspace)(
        inspect_tools.InspectAction(command="search", pattern="OUTSIDE-ONLY")
    )

    assert obs.match_count == 0
    # The two legitimate files were scanned; the refused link was not counted.
    assert obs.files_scanned == 2


def test_search_refuses_a_hard_link_to_the_runtime_credential(
    state_dir: Path, workspace: Path
) -> None:
    credential = state_dir / "profiles" / "default.json"
    credential.write_text('{"api_key": "SK-DO-NOT-LEAK-H1"}', encoding="utf-8")
    hard_link = workspace / "leak.json"
    hard_link.hardlink_to(credential)

    # The guard must refuse the name, and search must therefore not see it.
    with pytest.raises(permissions.PermissionDenied):
        permissions.check_path(
            "leak.json", root=workspace, permission="inspect", writing=False
        )
    obs = _executor(workspace)(
        inspect_tools.InspectAction(command="search", pattern="DO-NOT-LEAK")
    )
    assert obs.match_count == 0
    assert obs.files_scanned == 2


@pytest.mark.parametrize(
    "relative",
    [
        "settings.json",
        "secrets.json",
        "provider-connections/provider_connections.json",
        "agent-profiles/inspect.json",
        "conversations/session-a/meta.json",
        "conversations/session-a/base_state.json",
    ],
)
def test_inspect_refuses_every_runtime_state_alias(
    state_dir: Path, workspace: Path, relative: str
) -> None:
    protected = state_dir / relative
    protected.parent.mkdir(parents=True, exist_ok=True)
    protected.write_text('{"secret": "DO-NOT-LEAK"}', encoding="utf-8")
    alias = workspace / "alias.json"
    alias.hardlink_to(protected)

    with pytest.raises(permissions.PermissionDenied):
        permissions.check_path(
            str(alias), root=workspace, permission="inspect", writing=False
        )


@pytest.mark.parametrize(
    "environment_name", ["AGENTRT_PERSISTENCE_DIR", "AGENTRT_CONVERSATIONS_PATH"]
)
def test_inspect_refuses_aliases_from_explicit_persistence_locations(
    state_dir: Path,
    workspace: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    environment_name: str,
) -> None:
    external = tmp_path / environment_name.lower()
    if environment_name == "AGENTRT_CONVERSATIONS_PATH":
        protected = external / "session-a" / "base_state.json"
    else:
        protected = external / "secrets.json"
    protected.parent.mkdir(parents=True)
    protected.write_text('{"secret": "DO-NOT-LEAK"}', encoding="utf-8")
    monkeypatch.setenv(environment_name, str(external))
    alias = workspace / "explicit-location-alias.json"
    alias.hardlink_to(protected)

    with pytest.raises(permissions.PermissionDenied):
        permissions.check_path(
            str(alias), root=workspace, permission="inspect", writing=False
        )


def test_inspect_allows_unrelated_hard_link(
    state_dir: Path, workspace: Path, tmp_path: Path
) -> None:
    public = tmp_path / "public.txt"
    public.write_text("safe", encoding="utf-8")
    alias = workspace / "public-link.txt"
    alias.hardlink_to(public)

    assert (
        permissions.check_path(
            str(alias), root=workspace, permission="inspect", writing=False
        )
        == alias.resolve()
    )


def test_search_does_not_descend_git_metadata(state_dir: Path, workspace: Path) -> None:
    # A remote URL in .git/config can embed a credential; search must not read it.
    git_dir = workspace / ".git"
    git_dir.mkdir()
    (git_dir / "config").write_text(
        '[remote "origin"]\n\turl = https://user:TOKEN@example.invalid/repo\n',
        encoding="utf-8",
    )

    obs = _executor(workspace)(
        inspect_tools.InspectAction(command="search", pattern="TOKEN")
    )

    assert obs.match_count == 0


def test_search_cursor_advances_and_reports_continuation(
    state_dir: Path, workspace: Path
) -> None:
    (workspace / "many.txt").write_text(
        "\n".join(f"needle {index}" for index in range(5)) + "\n", encoding="utf-8"
    )
    first = _executor(workspace)(
        inspect_tools.InspectAction(command="search", pattern="needle", max_results=2)
    )
    assert first.match_count == 5
    assert len(first.matches) == 2
    assert first.next_offset == 2
    second = _executor(workspace)(
        inspect_tools.InspectAction(
            command="search",
            pattern="needle",
            max_results=2,
            offset=first.next_offset,
        )
    )
    assert [m.line for m in second.matches] == [3, 4]


def test_pathological_regex_cannot_stall_the_runtime(
    state_dir: Path, workspace: Path
) -> None:
    (workspace / "pathological.txt").write_text(
        "a" * (inspect_tools.MAX_LINE_CHARS - 1) + "X\n", encoding="utf-8"
    )
    started = time.monotonic()

    obs = _executor(workspace)(
        inspect_tools.InspectAction(command="search", pattern=r"^(a+)+$")
    )

    assert time.monotonic() - started < 1.0
    assert obs.is_error is True
    assert "deadline" in obs.text.lower()


def test_search_structured_output_obeys_the_budget(
    state_dir: Path, workspace: Path
) -> None:
    large = "x" * (inspect_tools.MAX_LINE_CHARS - 1000)
    (workspace / "large.txt").write_text(
        "\n".join([large] * 100) + "\n", encoding="utf-8"
    )

    obs = _executor(workspace)(
        inspect_tools.InspectAction(
            command="search",
            pattern="x",
            context_lines=5,
            max_results=100,
        )
    )

    assert len(obs.model_dump_json()) <= inspect_tools.MAX_OUTPUT_CHARS
    assert obs.truncated is True
    assert obs.next_offset is not None


def test_oversized_file_marks_search_incomplete(
    state_dir: Path, workspace: Path
) -> None:
    (workspace / "oversized.txt").write_bytes(
        b"needle" + b"x" * inspect_tools.MAX_FILE_BYTES
    )

    obs = _executor(workspace)(
        inspect_tools.InspectAction(command="search", pattern="needle")
    )

    assert obs.match_count == 0
    assert obs.truncated is True


def test_search_caps_total_matches_before_materializing_them(
    state_dir: Path, workspace: Path
) -> None:
    (workspace / "dense.txt").write_text(
        "a\n" * (inspect_tools.MAX_SEARCH_MATCHES_SCANNED + 100), encoding="utf-8"
    )

    obs = _executor(workspace)(
        inspect_tools.InspectAction(command="search", pattern="a", max_results=10)
    )

    assert len(obs.matches) == 10
    assert obs.match_count == inspect_tools.MAX_SEARCH_MATCHES_SCANNED
    assert obs.match_count_exact is False
    assert obs.truncated is True
    assert obs.next_offset == 10


# -- mutation attempts -------------------------------------------------


def test_guarded_editor_refuses_writes_under_inspect(
    state_dir: Path, workspace: Path
) -> None:
    guarded = GuardedFileEditorExecutor(
        FileEditorExecutor(workspace_root=str(workspace)),
        str(workspace),
        "inspect",
    )

    view = guarded(FileEditorAction(command="view", path="a.py"))
    assert view.is_error is False

    for command in ("create", "str_replace", "insert", "undo_edit"):
        action = FileEditorAction(command=command, path="a.py", file_text="x")
        obs = guarded(action)
        assert obs.is_error is True, command

    assert not (workspace / "new.txt").exists()
    assert (workspace / "a.py").read_text(encoding="utf-8").startswith("import os")


# -- version and environment -------------------------------------------


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_version_reports_a_resolved_executable(
    state_dir: Path, workspace: Path
) -> None:
    obs = _executor(workspace)(
        inspect_tools.InspectAction(command="version", executable="git")
    )
    assert obs.is_error is False
    assert obs.executable is not None
    assert Path(obs.executable).is_absolute()
    assert "git version" in (obs.version or "")


def test_environment_report_never_names_or_values_credentials(
    state_dir: Path, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENTRT_API_KEY", "sk-secret-value")
    monkeypatch.setenv("MY_SESSION_TOKEN", "tok-secret-value")

    obs = _executor(workspace)(inspect_tools.InspectAction(command="env"))

    names = obs.metadata["environment_names"]
    assert "AGENTRT_API_KEY" not in names
    assert "MY_SESSION_TOKEN" not in names
    assert "sk-secret-value" not in obs.text
    assert "tok-secret-value" not in obs.text


def test_sanitized_environment_drops_git_redirection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_DIR", "/somewhere/else")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.pager=evil'")

    env = inspect_tools.sanitized_environment()

    assert "GIT_DIR" not in env
    assert "GIT_CONFIG_PARAMETERS" not in env
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_OPTIONAL_LOCKS"] == "0"


# -- tool creation -----------------------------------------------------


def test_inspect_tool_create_requires_the_inspect_preset(
    state_dir: Path, workspace: Path
) -> None:
    # `create` only reads `conv_state.workspace.working_dir`; a full
    # ConversationState would drag the whole SDK in for one attribute.
    conv_state = cast(
        Any, SimpleNamespace(workspace=SimpleNamespace(working_dir=str(workspace)))
    )
    built = inspect_tools.InspectTool.create(conv_state, permission="inspect")
    assert len(built) == 1
    assert built[0].name == "inspect"
    assert built[0].annotations is not None
    assert built[0].annotations.readOnlyHint is True

    with pytest.raises(permissions.PermissionDenied):
        inspect_tools.InspectTool.create(conv_state, permission="workspace")
