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

PRESETS: tuple[str, ...] = ("readonly", "workspace", "broad")
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
            f"unknown permission {permission!r}; expected one of: "
            + ", ".join(PRESETS)
        )
    return value  # type: ignore[return-value]


def _real(path: str | os.PathLike[str]) -> Path:
    """Resolve a path the way the filesystem will.

    ``resolve()`` rather than ``normpath`` because it follows symlinks and
    Windows junctions; a check against the unresolved name would approve a link
    whose target is elsewhere. ``strict=False`` because a path being created
    does not exist yet, and refusing to check it would leave `create` unguarded.
    """
    return Path(path).resolve()


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
        return _real(raw_path)

    text = str(raw_path)
    # Extended-length and UNC prefixes bypass the normalisation everything else
    # here relies on, and nothing legitimate in a workspace needs them.
    if text.startswith("\\\\?\\") or text.startswith("\\\\.\\") or text.startswith("\\\\"):
        raise PermissionDenied(
            f"refusing an extended-length or UNC path: {text!r}"
        )

    if writing and permission == "readonly":
        raise PermissionDenied(
            "this session is readonly; it may view files but not change them"
        )

    resolved = _real(text)
    workspace = _real(root)
    if not contains(workspace, resolved):
        raise PermissionDenied(
            f"path is outside the workspace: {resolved} is not inside {workspace}"
        )
    return resolved


def tools_for(permission: Permission) -> list[str]:
    """Which tool names a preset grants.

    `readonly` drops the terminal entirely rather than trying to filter shell
    commands. A rule over command text is defeated by `python -c`, so the only
    honest read-only shell is no shell.
    """
    if permission == "readonly":
        return ["file_editor", "task_tracker"]
    return ["terminal", "file_editor", "task_tracker"]
