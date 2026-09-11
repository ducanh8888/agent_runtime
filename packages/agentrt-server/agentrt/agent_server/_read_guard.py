"""Guard the file/workspace read endpoints against serving server credentials.

A served workspace can contain a symlink or a same-inode hard link pointing at
one of the agent-server's own credential-bearing state files (``settings.json``,
``secrets.json``, ``provider-connections/*.json``, ``profiles/*.json``,
``agent-profiles/*.json`` and conversation ``meta.json`` / ``base_state.json``).
Containment checks alone do not catch the hard-link case: the link's path is
inside the workspace and resolves there, yet its inode is the credential file's.

This module identifies those exact server-owned files and answers whether a
candidate read path is one of them (or aliases one by inode). Only the
server-owned files are protected; a *copy* of a secret elsewhere has a different
inode and is deliberately out of scope -- this module makes no claim to detect
copied secrets.
"""

from __future__ import annotations

import stat
from pathlib import Path

from fastapi import HTTPException, status

from agentrt.agent_server.config import Config, get_default_config
from agentrt.sdk.utils.path import get_user_persistence_dir


# Credential-bearing files under the user persistence dir (settings/secrets) and
# its credential subdirectories. The provider-connections store keeps its keys
# in one JSON file per directory, hence the glob.
_PERSISTENCE_CREDENTIAL_FILES = (
    ".env",
    "daemon.json",
    "settings.json",
    "secrets.json",
)
_PERSISTENCE_CREDENTIAL_DIRS = ("provider-connections", "profiles", "agent-profiles")

# Per-conversation state files that can carry agent/LLM state.
_CONVERSATION_STATE_FILES = ("meta.json", "base_state.json")

Inode = tuple[int, int]


def _resolve_config(config: Config | None) -> Config:
    return config if config is not None else get_default_config()


def protected_state_paths(config: Config | None = None) -> list[Path]:
    """Existing server-owned credential files, resolved.

    Best-effort: unreadable or missing paths are skipped so a scan never turns a
    read request into a 500. The hot read path compares inode identities instead
    of resolving every protected path.
    """
    resolved: list[Path] = []
    for path in _candidate_state_paths(config):
        try:
            if path.is_file():
                resolved.append(path.resolve())
        except OSError:
            continue
    return resolved


def _candidate_state_paths(config: Config | None) -> list[Path]:
    """Unresolved server-owned credential file paths (no stat, no resolve)."""
    paths: list[Path] = []
    persistence = get_user_persistence_dir()
    paths.extend(persistence / name for name in _PERSISTENCE_CREDENTIAL_FILES)
    for subdir in _PERSISTENCE_CREDENTIAL_DIRS:
        try:
            paths.extend((persistence / subdir).glob("*.json"))
        except OSError:
            continue

    conversations_path = _resolve_config(config).conversations_path
    try:
        conversation_dirs = list(conversations_path.iterdir())
    except OSError:
        conversation_dirs = []
    for child in conversation_dirs:
        try:
            if not child.is_dir():
                continue
        except OSError:
            continue
        paths.extend(child / name for name in _CONVERSATION_STATE_FILES)
    return paths


def _inode(path: Path) -> Inode | None:
    try:
        stat_result = path.stat()
    except OSError:
        return None
    if not stat.S_ISREG(stat_result.st_mode):
        return None
    return (stat_result.st_dev, stat_result.st_ino)


def _is_protected_state_location(candidate: Path, config: Config | None) -> bool:
    """Whether a resolved path names a protected state location.

    Unlike inode matching, this also protects a credential file before it has
    been created. That matters to write routes: uploading directly to a missing
    ``settings.json`` must not be the operation that creates it.
    """
    try:
        resolved = candidate.resolve(strict=False)
        persistence = get_user_persistence_dir().resolve(strict=False)
        conversations = _resolve_config(config).conversations_path.resolve(strict=False)
    except OSError:
        return False

    if (
        resolved.parent == persistence
        and resolved.name in _PERSISTENCE_CREDENTIAL_FILES
    ):
        return True
    if resolved.suffix == ".json" and resolved.parent.parent == persistence:
        if resolved.parent.name in _PERSISTENCE_CREDENTIAL_DIRS:
            return True
    if (
        resolved.name in _CONVERSATION_STATE_FILES
        and resolved.parent.parent == conversations
    ):
        return True
    return False


def protected_state_inodes(config: Config | None = None) -> set[Inode]:
    """Inodes of the existing server-owned credential files.

    Recomputed for every read. Correctness is more important than caching here:
    a newly written credential or conversation state file must be protected on
    its first read attempt, not after a time window expires. A future cache may
    only be introduced with write-side invalidation owned by every persistence
    writer.
    """
    return _scan_protected_state_inodes(config)


def _scan_protected_state_inodes(config: Config | None) -> set[Inode]:
    inodes: set[Inode] = set()
    for path in _candidate_state_paths(config):
        inode = _inode(path)
        if inode is not None:
            inodes.add(inode)
    return inodes


def is_protected_state_path(candidate: Path, config: Config | None = None) -> bool:
    """True when ``candidate`` is, or aliases by inode, a server-owned file.

    Comparing inodes (rather than resolved paths) also catches symlinks and hard
    links to the protected files without resolving every protected path.
    """
    if _is_protected_state_location(candidate, config):
        return True
    try:
        candidate_stat = candidate.stat()
    except OSError:
        return False
    if not stat.S_ISREG(candidate_stat.st_mode):
        return False
    if candidate_stat.st_nlink < 2:
        return False
    candidate_inode = (candidate_stat.st_dev, candidate_stat.st_ino)
    return candidate_inode in protected_state_inodes(config)


def reject_protected_state_path(candidate: Path, config: Config | None = None) -> None:
    """Raise 404 when ``candidate`` aliases a server-owned credential file.

    404 (rather than 403) keeps the response from confirming that a particular
    server state file exists at that path.
    """
    if is_protected_state_path(candidate, config):
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found",
        )
