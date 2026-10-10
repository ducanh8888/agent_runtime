"""Tests for the require-commit stop-hook checker."""

from __future__ import annotations

import subprocess

from agentrt.runtime.commit_hook import REASON, main


def test_checker_blocks_until_head_advances(monkeypatch, tmp_path, capsys) -> None:
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(tmp_path),
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@t",
            "commit",
            "--allow-empty",
            "-m",
            "base",
        ],
        check=True,
        capture_output=True,
    )
    start = subprocess.run(
        ["git", "-C", str(tmp_path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["commit_hook", start])

    assert main() == 2
    assert REASON in capsys.readouterr().err

    subprocess.run(
        [
            "git",
            "-c",
            "user.name=T",
            "-c",
            "user.email=t@t",
            "commit",
            "--allow-empty",
            "-m",
            "next",
        ],
        check=True,
        capture_output=True,
    )
    assert main() == 0


def _git(repo, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=T", "-c", "user.email=t@t", *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo_with_rewritten_start(tmp_path):
    """work = base + S (the dispatch HEAD); up = base + U. Rebasing work onto up
    rewrites S, so the dispatch HEAD stops being an ancestor of HEAD (#6)."""
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "base")
    _git(tmp_path, "branch", "up")
    _git(tmp_path, "checkout", "-q", "-b", "work")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "S")
    start = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "checkout", "-q", "up")
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "U")
    _git(tmp_path, "checkout", "-q", "work")
    return start


def test_checker_accepts_commits_that_survive_a_rebase(
    monkeypatch, tmp_path, capsys
) -> None:
    start = _repo_with_rewritten_start(tmp_path)
    _git(tmp_path, "commit", "-q", "--allow-empty", "-m", "W")
    _git(tmp_path, "rebase", "-q", "up")
    assert (
        subprocess.run(
            ["git", "-C", str(tmp_path), "merge-base", "--is-ancestor", start, "HEAD"]
        ).returncode
        == 1
    )  # the premise: the old check would have blocked this
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["commit_hook", start])

    assert main() == 0


def test_checker_still_blocks_a_move_that_made_no_commit(
    monkeypatch, tmp_path, capsys
) -> None:
    start = _repo_with_rewritten_start(tmp_path)
    _git(tmp_path, "checkout", "-q", "--detach", "up")  # HEAD moved, nothing committed
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["commit_hook", start])

    assert main() == 2
    assert REASON in capsys.readouterr().err
