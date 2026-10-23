"""The readonly runner honours the user's own ``safe.directory`` (AgentRT #36).

It drops global and system config, which also dropped ``safe.directory``. On
Windows a repository created from an elevated shell is owned by
``BUILTIN\\Administrators``, so git refused it as "dubious ownership" even though
the user had trusted it, and snapshot/isolated_worktree dispatch reported "Not a
git repository". ``GIT_TEST_ASSUME_DIFFERENT_OWNER`` makes git treat any repo as
owned by someone else, on every platform.
"""

import subprocess
from pathlib import Path

import pytest

from agentrt.sdk.git import utils
from agentrt.sdk.git.exceptions import GitRepositoryError


@pytest.fixture
def foreign_repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    global_config = tmp_path / "global.gitconfig"
    global_config.write_text("")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(global_config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    hardened_env = utils._readonly_git_env
    monkeypatch.setattr(
        utils,
        "_readonly_git_env",
        lambda: {**hardened_env(), "GIT_TEST_ASSUME_DIFFERENT_OWNER": "1"},
    )
    monkeypatch.setattr(utils, "_safe_directories_cache", None, raising=False)
    return repo, global_config


def _trust(global_config: Path, value: str) -> None:
    subprocess.run(
        [
            "git",
            "config",
            "--file",
            str(global_config),
            "--add",
            "safe.directory",
            value,
        ],
        check=True,
    )


def test_a_repo_the_user_trusted_is_accepted(foreign_repo):
    repo, global_config = foreign_repo
    _trust(global_config, repo.resolve().as_posix())
    assert utils.validate_git_repository(repo) == repo.resolve()


def test_an_untrusted_foreign_repo_is_still_refused(foreign_repo):
    repo, global_config = foreign_repo
    _trust(global_config, (repo.parent / "elsewhere").resolve().as_posix())
    with pytest.raises(GitRepositoryError):
        utils.validate_git_repository(repo)


def test_only_safe_directory_is_carried_over(foreign_repo):
    repo, global_config = foreign_repo
    _trust(global_config, repo.resolve().as_posix())
    subprocess.run(
        ["git", "config", "--file", str(global_config), "core.pager", "evil"],
        check=True,
    )
    argv = utils._build_readonly_argv(["git", "status"])
    assert ["-c", f"safe.directory={repo.resolve().as_posix()}"] == argv[
        argv.index(f"safe.directory={repo.resolve().as_posix()}") - 1 :
    ][:2]
    assert not any("evil" in arg for arg in argv)
