"""Permission presets, and the path guard that makes `readonly` mean something.

Hand-written rather than delegated, for the reason `bootstrap.py` is: a guard
that is subtly wrong is worse than no guard, because it is trusted. The specific
ways this goes wrong on Windows are `startswith` instead of `commonpath`
(`C:\\ws` would contain `C:\\ws2`), `normpath` instead of `resolve` (a junction
points elsewhere and normpath never notices), and forgetting `normcase` (`C:\\Foo`
and `c:\\foo` are the same directory).

What the presets are honest about:

- `readonly` — no terminal at all, and the file editor may only view, only
  inside the workspace. This is the one preset that keeps a session away from
  the credential in the state directory.
- `workspace` — the file editor is confined to the workspace, and a terminal is
  present. **A terminal defeats path confinement**: `python -c` opens any file
  the user can. The confinement constrains the model's ordinary behaviour, not a
  determined one.
- `broad` — no confinement.

Plan §5 says this plainly and it bears repeating here, next to the code that
would otherwise look like it were enforcing more than it does.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal


Permission = Literal["readonly", "workspace", "broad"]

PRESETS: tuple[Permission, ...] = ("readonly", "workspace", "broad")
DEFAULT_PERMISSION: Permission = "workspace"

DESCRIPTIONS: dict[str, str] = {
    "readonly": (
        "Read-only. No terminal. The file editor can view files inside the "
        "workspace and nothing else. The only preset that keeps a session away "
        "from the provider credential."
    ),
    "workspace": (
        "Read and write inside the workspace, with a terminal. The file editor "
        "is confined to the workspace; the terminal is not, because a shell can "
        "open any file this user can."
    ),
    "broad": (
        "No filesystem confinement. Everything the user running the daemon can "
        "do. Use only for work you would run yourself."
    ),
}


class PermissionDenied(Exception):
    """A tool call was refused by the profile it is running under."""


def normalise(permission: str | None) -> Permission:
    """Return a known preset name, defaulting rather than guessing.

    An unknown name is rejected instead of silently falling back to the
    default: a caller who asks for `read-only` and gets `workspace` has been
    given more authority than they asked for, which is the wrong direction to
    fail in.
    """
    if permission is None:
        return DEFAULT_PERMISSION
    value = str(permission).strip().lower()
    if value not in PRESETS:
        raise PermissionDenied(
            f"unknown permission {permission!r}; expected one of: " + ", ".join(PRESETS)
        )
    return value  # type: ignore[return-value]


def _real(
    path: str | os.PathLike[str], *, base: str | os.PathLike[str] | None = None
) -> Path:
    """Resolve a path the way the filesystem will.

    ``resolve()`` rather than ``normpath`` because it follows symlinks and
    Windows junctions; a check against the unresolved name would approve a link
    whose target is elsewhere. ``strict=False`` because a path being created
    does not exist yet, and refusing to check it would leave `create` unguarded.

    A relative path is joined onto ``base`` rather than left to ``resolve()``,
    which would anchor it to the daemon's own working directory. That directory
    is wherever the daemon happened to be started, so an agent asking for
    ``OK.txt`` in its workspace was told the file was outside the workspace --
    true of the path that had been constructed, and nothing to do with what it
    asked for.
    """
    candidate = Path(path)
    if base is not None and not candidate.is_absolute():
        candidate = Path(base) / candidate
    return candidate.resolve()


def contains(root: Path, target: Path) -> bool:
    """Is ``target`` inside ``root``?

    ``commonpath`` rather than ``startswith``: as strings, ``C:\\ws2`` starts
    with ``C:\\ws``. ``normcase`` because Windows paths are case-insensitive and
    ``C:\\Foo`` must match ``c:\\foo``. A ``ValueError`` means the two share no
    common prefix at all -- different drives, or a UNC path against a local one
    -- which is emphatically outside.
    """
    a = os.path.normcase(str(root))
    b = os.path.normcase(str(target))
    try:
        return os.path.commonpath([a, b]) == a
    except ValueError:
        return False


def check_path(
    raw_path: str,
    *,
    root: str | os.PathLike[str],
    permission: Permission,
    writing: bool,
) -> Path:
    """Approve one filesystem access, or raise ``PermissionDenied``.

    Returns the resolved path so a caller can act on the same value that was
    checked, rather than re-deriving it and possibly getting a different one.
    """
    if permission == "broad":
        return _real(raw_path, base=root)

    text = str(raw_path)
    # Extended-length and UNC prefixes bypass the normalisation everything else
    # here relies on, and nothing legitimate in a workspace needs them.
    if (
        text.startswith("\\\\?\\")
        or text.startswith("\\\\.\\")
        or text.startswith("\\\\")
    ):
        raise PermissionDenied(f"refusing an extended-length or UNC path: {text!r}")

    if writing and permission == "readonly":
        raise PermissionDenied(
            "this session is readonly; it may view files but not change them"
        )

    # Windows strips trailing dots and spaces from a path component when it
    # opens it, but Python's normalisation does not, so `.. ` survives as a
    # literal name here and could be read as `..` further down. Measured, the
    # open then fails rather than escaping -- but the guard should not be
    # relying on that, since it is a detail of one API on one OS.
    for part in Path(text).parts:
        if part in (".", ".."):
            # resolve() handles these correctly; it is the near-misses that
            # survive normalisation as literal names.
            continue
        if part and set(part) <= {".", " "}:
            raise PermissionDenied(
                f"refusing a path component made only of dots and spaces: {part!r}"
            )

    workspace = _real(root)
    resolved = _real(text, base=workspace)
    if not contains(workspace, resolved):
        raise PermissionDenied(
            f"path is outside the workspace: {resolved} is not inside {workspace}"
        )

    if _is_runtime_secret(resolved):
        raise PermissionDenied(
            f"refusing {resolved}: it is the same file as one of the runtime's "
            "own credential files, reached under a different name"
        )
    return resolved


def _secret_identities() -> set[tuple[int, int]]:
    """(device, inode) of every file in the state directory holding a secret.

    Identity rather than path, because that is the thing a second name cannot
    disguise. Recomputed per call: these files are few, and caching them would
    mean a credential rewritten after startup stopped being recognised.

    `agent-profiles/` is deliberately absent -- those name a permission preset
    and a tool list, and hold no secret. Only `profiles/` carries the LLM
    profile, and with it the key.

    On a filesystem that reports no inode -- FAT, and some network shares --
    `st_ino` is 0 and this returns nothing. The hard-link protection is then
    simply off rather than weakened, and a workspace on such a volume gets the
    behaviour that existed before it: path confinement, blind to aliases.
    """
    from agentrt.runtime import config

    state = config.state_dir()
    candidates = [
        state / ".env",
        state / "daemon.json",
        *(state / "profiles").glob("*.json"),
    ]
    out: set[tuple[int, int]] = set()
    for path in candidates:
        try:
            info = path.stat()
        except OSError:
            continue
        if info.st_ino:  # 0 means the filesystem does not report one
            out.add((info.st_dev, info.st_ino))
    return out


def _is_runtime_secret(resolved: Path) -> bool:
    """Is this path another name for one of the runtime's credential files?

    A hard link is a second name for one file, and no path resolution reveals
    it: ``resolve()`` returns the name it was given, that name is inside the
    workspace, and the bytes belong to a file that is not. A workspace holding a
    link to the credential therefore reads it through an approved path --
    demonstrated against a real ``readonly`` session, which cannot create such a
    link but does not need to when the directory it was pointed at already has
    one.

    Matching on identity catches that. An earlier attempt refused any file whose
    ``st_nlink`` exceeded one, which is the general form of the problem and
    unusable in practice: ``uv`` hard-links packages from its global cache, so
    30,656 of the 31,402 files in this project's own virtualenv have more than
    one name. That guard would have refused to read almost any library source.

    Stated plainly, this protects the runtime's own secrets and nothing else. A
    hard link to some other file outside the workspace stays invisible, because
    confinement by path cannot see aliasing it is not told about. Closing that
    properly needs a sandbox, not a cleverer check.
    """
    try:
        info = resolved.stat()
    except OSError:
        return False
    if not info.st_ino:
        return False
    return (info.st_dev, info.st_ino) in _secret_identities()


def tools_for(permission: Permission) -> list[str]:
    """Which tool names a preset grants.

    `readonly` drops the terminal entirely rather than trying to filter shell
    commands. A rule over command text is defeated by `python -c`, so the only
    honest read-only shell is no shell.
    """
    if permission == "readonly":
        return ["file_editor", "task_tracker"]
    return ["terminal", "file_editor", "task_tracker"]
