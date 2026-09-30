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
