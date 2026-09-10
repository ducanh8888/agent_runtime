"""Marker-file attacks against the readonly git read surfaces.

A workspace repository can set ``diff.external``, ``diff.<driver>.textconv`` and
``core.fsmonitor`` in its own ``.git/config`` / ``.gitattributes``. If a read
command inherits that config, git executes an arbitrary program. Each helper
script below writes a marker file when it runs, so a test can assert the program
never ran.

The first test is a positive control: it runs raw git with ambient config and
proves the repository is genuinely hostile, so the hardening tests are not
passing vacuously.
"""

import subprocess
from pathlib import Path

import pytest

from agentrt.sdk.git.exceptions import GitCommandError
from agentrt.sdk.git.git_changes import get_changes_in_repo
from agentrt.sdk.git.git_commits import get_commit_file_diff, get_git_commits
from agentrt.sdk.git.git_diff import get_git_diff
from agentrt.sdk.git.utils import run_readonly_git_command


def _run(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=cwd, capture_output=True, text=True, check=False)


def _write_helper(path: Path, marker: Path) -> None:
    path.write_text(f"#!/bin/sh\necho ran > {marker}\nexit 0\n")
    path.chmod(0o755)


@pytest.fixture
def hostile_repo(tmp_path: Path) -> tuple[Path, dict[str, Path]]:
    """A committed repo whose local config would execute programs on reads."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-b", "main"], repo)
    _run(["git", "config", "user.email", "t@example.com"], repo)
    _run(["git", "config", "user.name", "T"], repo)
    (repo / "tracked.txt").write_text("base\n")
    _run(["git", "add", "-A"], repo)
    _run(["git", "commit", "-m", "base"], repo)
    (repo / "tracked.txt").write_text("changed\n")

    helpers = tmp_path / "helpers"
    helpers.mkdir()
    markers = {
        "external_driver": tmp_path / "MARKER-external",
        "driver_command": tmp_path / "MARKER-driver-command",
        "textconv": tmp_path / "MARKER-textconv",
        "fsmonitor": tmp_path / "MARKER-fsmonitor",
    }
    for name, marker in markers.items():
        _write_helper(helpers / f"{name}.sh", marker)

    _run(["git", "config", "diff.external", str(helpers / "external_driver.sh")], repo)
    _run(["git", "config", "core.fsmonitor", str(helpers / "fsmonitor.sh")], repo)
    _run(["git", "config", "diff.hostile.textconv", str(helpers / "textconv.sh")], repo)
    # A per-driver external command selected from ``.gitattributes``; unlike
    # ``diff.external`` it is only suppressed by ``--no-ext-diff``.
    _run(
        ["git", "config", "diff.hostile.command", str(helpers / "driver_command.sh")],
        repo,
    )
    (repo / ".gitattributes").write_text("*.txt diff=hostile\n")
    return repo, markers


def _clear(markers: dict[str, Path]) -> None:
    for marker in markers.values():
        marker.unlink(missing_ok=True)


def _ran(markers: dict[str, Path]) -> set[str]:
    return {name for name, marker in markers.items() if marker.exists()}


def test_raw_git_in_hostile_repo_executes_helpers(hostile_repo):
    """Positive control: without hardening, the hostile config does run."""
    repo, markers = hostile_repo
    _clear(markers)

    _run(["git", "--no-pager", "diff"], repo)
    _run(["git", "--no-pager", "status", "--porcelain"], repo)
    _run(["git", "--no-pager", "show", "HEAD"], repo)

    assert _ran(markers), "fixture is not hostile; marker scripts never executed"


def test_get_changes_in_repo_does_not_execute_hostile_config(hostile_repo):
    repo, markers = hostile_repo
    _clear(markers)

    changes = get_changes_in_repo(repo, ref="HEAD")

    assert any(change.path == Path("tracked.txt") for change in changes)
    assert _ran(markers) == set()


def test_get_git_commits_does_not_execute_hostile_config(hostile_repo):
    repo, markers = hostile_repo
    _clear(markers)

    page = get_git_commits(repo)

    assert page.commits
    assert _ran(markers) == set()


def test_get_git_diff_does_not_execute_hostile_config(
    hostile_repo, monkeypatch: pytest.MonkeyPatch
):
    repo, markers = hostile_repo
    _clear(markers)
    monkeypatch.chdir(repo)

    diff = get_git_diff("tracked.txt", ref="HEAD")

    assert diff.modified == "changed"
    assert diff.original == "base"
    assert _ran(markers) == set()


def test_readonly_command_ignores_git_external_diff_env(
    hostile_repo, monkeypatch: pytest.MonkeyPatch
):
    repo, markers = hostile_repo
    _clear(markers)
    monkeypatch.setenv(
        "GIT_EXTERNAL_DIFF", str(markers["external_driver"].with_name("unused.sh"))
    )

    output = run_readonly_git_command(
        ["git", "--no-pager", "diff", "--name-status", "HEAD"], repo
    )

    assert "tracked.txt" in output
    assert _ran(markers) == set()


def test_readonly_command_refuses_mutating_subcommand(hostile_repo):
    repo, _ = hostile_repo
    with pytest.raises(GitCommandError, match="non-readonly"):
        run_readonly_git_command(["git", "checkout", "HEAD"], repo)


def test_readonly_command_refuses_option_injection(hostile_repo):
    repo, _ = hostile_repo
    with pytest.raises(GitCommandError, match="unexpected option"):
        run_readonly_git_command(
            ["git", "--no-pager", "rev-parse", "--verify", "--output=/tmp/evil"],
            repo,
        )


def test_readonly_commit_file_diff_does_not_execute_textconv(
    hostile_repo, monkeypatch: pytest.MonkeyPatch
):
    repo, markers = hostile_repo
    _clear(markers)
    commit = _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    monkeypatch.chdir(repo)

    diff = get_commit_file_diff("tracked.txt", commit)

    assert diff.original == ""
    assert diff.modified == "base"
    assert _ran(markers) == set()
