"""H1: hostile Git configuration cannot turn an inspect read into execution.

`inspect` runs a fixed set of Git read commands. Those commands would, with a
repository's own configuration in charge, be able to run a program the
repository names: a pager, an external diff, a textconv filter, an fsmonitor
hook, a credential helper. The tests below make Git's configuration try exactly
that and then check the filesystem for the marker those programs would write.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from agentrt.runtime import inspect_tools


pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
        },
    )


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    (state / "profiles").mkdir(parents=True)
    monkeypatch.setenv("AGENTRT_STATE_DIR", str(state))
    return state


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    ws = tmp_path / "repo"
    ws.mkdir()
    _git("init", "-q", cwd=ws)
    (ws / "a.txt").write_text("hello\n", encoding="utf-8")
    _git("add", "-A", cwd=ws)
    _git("commit", "-qm", "init", cwd=ws)
    return ws


def _executor(repo: Path) -> inspect_tools.InspectExecutor:
    return inspect_tools.InspectExecutor(root=str(repo), permission="inspect")


def _run(repo: Path, **kwargs) -> inspect_tools.InspectObservation:
    action = inspect_tools.InspectAction(command="git", **kwargs)
    return _executor(repo)(action)


def _hostile_script(repo: Path, marker: Path) -> Path:
    script = repo / "evil.sh"
    script.write_text(
        f"#!/bin/sh\necho ran >> {marker}\nexit 0\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    return script


def test_status_and_diff_and_log_and_show_all_work(repo: Path, state_dir: Path) -> None:
    (repo / "a.txt").write_text("hello changed\n", encoding="utf-8")
    (repo / "new.txt").write_text("untracked\n", encoding="utf-8")

    status = _run(repo, git_command="status")
    assert status.exit_code == 0
    assert "a.txt" in (status.stdout or "")
    assert "new.txt" in (status.stdout or "")

    diff = _run(repo, git_command="diff", git_path="a.txt")
    assert diff.exit_code == 0
    assert "hello changed" in (diff.stdout or "")

    log = _run(repo, git_command="log")
    assert log.exit_code == 0
    assert "init" in (log.stdout or "")

    show = _run(repo, git_command="show")
    assert show.exit_code == 0
    assert "hello" in (show.stdout or "")


def test_git_argv_disables_the_execution_surfaces(repo: Path, state_dir: Path) -> None:
    diff = _run(repo, git_command="diff")
    argv = diff.argv
    assert "--no-pager" in argv
    assert "--no-optional-locks" in argv
    assert "--no-ext-diff" in argv
    assert "--no-textconv" in argv
    assert "core.fsmonitor=false" in argv
    assert "core.pager=cat" in argv
    assert "diff.external=" in argv
    assert "credential.helper=" in argv
    assert any(arg.startswith("core.hooksPath=") for arg in argv)


@pytest.mark.skipif(os.name == "nt", reason="POSIX helper scripts")
def test_hostile_repository_config_cannot_execute(repo: Path, state_dir: Path) -> None:
    marker = repo / "PWNED"
    script = _hostile_script(repo, marker)
    (repo / ".git" / "config").write_text(
        (repo / ".git" / "config").read_text(encoding="utf-8")
        + "\n[core]\n"
        + f"\tfsmonitor = {script}\n"
        + f"\tpager = {script}\n"
        + "[diff]\n"
        + f"\texternal = {script}\n"
        + '[diff "evil"]\n'
        + f"\ttextconv = {script}\n"
        + f"\tcommand = {script}\n"
        + "[credential]\n"
        + f"\thelper = {script}\n",
        encoding="utf-8",
    )
    (repo / ".gitattributes").write_text("a.txt diff=evil\n", encoding="utf-8")
    (repo / "a.txt").write_text("hello changed\n", encoding="utf-8")

    for command in ("status", "diff", "log", "show"):
        obs = _run(repo, git_command=command)
        assert obs.exit_code == 0, command
    assert not marker.exists(), "a repository-configured helper was executed"


def test_git_path_escape_is_refused(repo: Path, state_dir: Path) -> None:
    obs = _run(repo, git_command="diff", git_path="../../etc/passwd")
    assert obs.is_error is True
    assert "outside the workspace" in obs.text


def test_git_refuses_a_work_tree_above_the_workspace(
    repo: Path, state_dir: Path
) -> None:
    subdir = repo / "nested"
    subdir.mkdir()
    (subdir / "c.txt").write_text("c\n", encoding="utf-8")

    # The repository root is `repo`, above the session's workspace `subdir`.
    obs = _executor(subdir)(
        inspect_tools.InspectAction(command="git", git_command="status")
    )

    assert obs.is_error is True
    assert "outside the workspace" in obs.text


def test_sanitized_environment_overrides_a_hostile_global_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/tmp/hostile-global-config")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/tmp/hostile-system-config")

    env = inspect_tools.sanitized_environment()

    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_CONFIG_SYSTEM"] == os.devnull
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["HOME"] == env["XDG_CONFIG_HOME"]
