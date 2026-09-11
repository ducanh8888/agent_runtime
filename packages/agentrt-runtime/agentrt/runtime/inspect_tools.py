"""The `inspect` tool: schema-validated read operations for a confined session.

This is the tool the `inspect` permission preset adds between `readonly` and
`workspace`. It is deliberately not a shell and not a string allowlist over
shell text: a rule over command text is defeated by a single `-c`. Instead the
model selects one of four commands, each with its own closed parameter set, and
this module builds the resulting argv itself. `search` is implemented in Python
(no external process at all); `git` and `version` resolve a known executable and
run it with a list argv and a sanitized environment.

The threat model, stated so the code below is not mistaken for more than it is:

- Confinement is by resolved path. Every candidate and every returned path is
  checked with `permissions.check_path`, so a symlink that leaves the workspace
  and a hard link to the runtime's own credential are both refused. A *copy* of
  a secret at a new inode is indistinguishable from any other file by path, and
  no path guard can see it; that needs a sandbox.
- Git is run with the repository's own configuration ignored where it could
  execute code. External diff, textconv, pager, fsmonitor, hooks and credential
  helpers are disabled or bypassed, global and system config are not read, the
  optional index lock is off, and the inherited environment is filtered so the
  provider credential cannot reach the child. Git still reads repository-local
  config and `.gitattributes`; the flags make those inert for the four read
  commands rather than trusting them.
- The output is Git's own bytes. If a tracked file contains a secret, the repo's
  owner put it there; this tool does not widen that beyond what `file_editor`
  view already allows inside the workspace.

Registered on import, like the vendored tools, so importing
`agentrt.runtime.guarded_tools` inside the daemon (which is how the runtime
installs its guards) also registers this tool.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import regex as timeout_regex
from pydantic import BaseModel, Field, model_validator

from agentrt.runtime import permissions
from agentrt.sdk.llm import ImageContent, TextContent
from agentrt.sdk.tool import (
    Action,
    Observation,
    ToolAnnotations,
    ToolDefinition,
    ToolExecutor,
    register_tool,
)


if TYPE_CHECKING:
    from agentrt.sdk.conversation.state import ConversationState


INSPECT_PERMISSION = "inspect"

#: Bounds that keep a single call from becoming an unbounded read. The pattern
#: cap and per-line cap exist because this uses Python's backtracking regex:
#: without them a pathological pattern against a long line is a denial of
#: service that no path guard would catch.
MAX_PATTERN_CHARS = 512
MAX_FILE_BYTES = 1_000_000
MAX_LINE_CHARS = 10_000
MAX_CONTEXT_LINES = 5
MAX_RESULTS = 500
MAX_OUTPUT_CHARS = 200_000
REGEX_TIMEOUT_SECONDS = 0.05
SEARCH_MATCH_BUDGET = MAX_OUTPUT_CHARS - 20_000
MAX_SEARCH_MATCHES_SCANNED = 10_000

#: A revision may not begin with `-` (option injection) and is kept to the
#: characters git itself uses in a ref, so `a..b` and `HEAD~2` work while
#: `--upload-pack=...` and `:/etc/passwd` do not.
_REVISION_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._/@^~-]*$")

#: Directories never descended into by `search`. `.git` is excluded because it
#: holds configuration that may embed a credential in a remote URL, and object
#: files are not source. Heavy dependency trees are left in on purpose: skipping
#: them silently is how a search misses the file the caller asked about.
_SEARCH_SKIP_DIRS = frozenset({".git"})

_GIT_DIRECTORY_FLAGS = ("--no-ext-diff", "--no-textconv", "--no-color")

#: Environment variable names that look like they carry a credential, or that
#: name the runtime/provider directly. Values are dropped before any child
#: process or environment report sees them. Over-matching is the safe direction.
_SECRET_NAME_RE = re.compile(
    r"KEY|SECRET|TOKEN|PASSWORD|PASSWD|CREDENTIAL|AUTH|PRIVATE|SESSION|COOKIE",
    re.IGNORECASE,
)
_PROVIDER_NAME_RE = re.compile(
    r"^(AGENTRT|OPENAI|ANTHROPIC|DEEPSEEK|OPENROUTER|AZURE|AWS|GOOGLE|GEMINI"
    r"|MISTRAL|COHERE|GROQ|XAI|9ROUTER)",
    re.IGNORECASE,
)

#: Non-secret environment values that are safe and useful to report verbatim.
_ENV_VALUE_ALLOWLIST = frozenset(
    {"SHELL", "TERM", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "OSTYPE", "HOSTTYPE"}
)

#: The executables whose version may be queried. A closed set, with the argv
#: fixed here, so no caller-supplied text becomes an argument.
_VERSION_ARGV: dict[str, tuple[str, ...]] = {
    "git": ("--version",),
    "python": ("--version",),
    "python3": ("--version",),
    "node": ("--version",),
    "npm": ("--version",),
    "uv": ("--version",),
    "ruff": ("--version",),
    "pytest": ("--version",),
    "go": ("version",),
    "cargo": ("--version",),
    "rustc": ("--version",),
    "java": ("-version",),
    "javac": ("-version",),
}


def _is_secret_name(name: str) -> bool:
    return bool(_SECRET_NAME_RE.search(name) or _PROVIDER_NAME_RE.match(name))


@lru_cache(maxsize=1)
def _sandbox_home() -> str:
    """A private empty directory used as HOME/hooks path for child processes.

    `mkdtemp` rather than a fixed name under the shared temp root: a predictable
    world-writable path is somewhere a hook could be planted. Mode 0700 and one
    per process close that.
    """
    path = tempfile.mkdtemp(prefix="agentrt-inspect-")
    os.chmod(path, 0o700)
    return path


def sanitized_environment() -> dict[str, str]:
    """The daemon's environment with credential-shaped entries removed.

    Every `GIT_*` entry is dropped as well, including `GIT_DIR`, `GIT_WORK_TREE`
    and `GIT_CONFIG_PARAMETERS`: any of those can redirect git at another
    repository or inject configuration, and none of them is needed for the four
    read commands this module runs.
    """
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("GIT_") and not _is_secret_name(name)
    }
    home = _sandbox_home()
    env.update(
        {
            "HOME": home,
            "XDG_CONFIG_HOME": home,
            "LC_ALL": "C",
            "LANG": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_CONFIG_SYSTEM": os.devnull,
            "GIT_ATTR_NOSYSTEM": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_PAGER": "cat",
            "PAGER": "cat",
            "GIT_EXTERNAL_DIFF": "",
        }
    )
    return env


def environment_names() -> list[str]:
    """Names only, from the sanitized environment. No value ever leaves here."""
    return sorted(sanitized_environment())


def _resolve_executable(name: str, root: Path) -> str | None:
    """Absolute path to `name`, or None if missing or inside the workspace.

    Resolution uses the daemon's own PATH, then the result is re-resolved and
    refused if it lands in the workspace: a workspace that already contains a
    `git` binary must not be able to supply the one this tool runs.
    """
    found = shutil.which(name)
    if not found:
        return None
    resolved = Path(found).resolve()
    if permissions.contains(root, resolved):
        return None
    return str(resolved)


def _clip(text: str, limit: int = MAX_OUTPUT_CHARS) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit] + "\n...<clipped>", True


def _fit_search_page(matches: list[SearchMatch]) -> tuple[list[SearchMatch], bool]:
    """Fit a match page under the structured observation's output budget."""
    page: list[SearchMatch] = []
    used = 0
    clipped = False
    for match in matches:
        size = len(json.dumps(match.model_dump(mode="json"), ensure_ascii=False))
        if page and used + size > SEARCH_MATCH_BUDGET:
            clipped = True
            break
        if not page and size > SEARCH_MATCH_BUDGET:
            match = match.model_copy(
                update={
                    "text": match.text[:1024],
                    "context_before": [value[:1024] for value in match.context_before],
                    "context_after": [value[:1024] for value in match.context_after],
                }
            )
            size = len(json.dumps(match.model_dump(mode="json"), ensure_ascii=False))
            clipped = True
        page.append(match)
        used += size
    return page, clipped


def _source_lines(text: str) -> list[str]:
    """Split only the newline sequences used by file-editor line ranges."""
    if not text:
        return []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    if lines[-1] == "":
        lines.pop()
    return lines


class InspectAction(Action):
    """The closed parameter set for one `inspect` command.

    There is no `args` or `command_text` field. Every value a caller can set is
    either a bounded scalar or a pattern/ref this class re-validates, and the
    executor turns them into argv itself.
    """

    command: Literal["search", "git", "version", "env"] = Field(
        description=(
            "Which read operation to run: `search` (structured file search), "
            "`git` (status/diff/log/show), `version` (one executable's "
            "--version), or `env` (runtime and environment metadata)."
        )
    )

    # search
    pattern: str | None = Field(
        default=None, description="`search`: the regular expression to find."
    )
    path: str | None = Field(
        default=None,
        description=(
            "Optional path to start from, for `search` and `git`. Relative "
            "paths are resolved against the workspace; anything that resolves "
            "outside it, or to the runtime credential, is refused."
        ),
    )
    include: str | None = Field(
        default=None,
        description='`search`: filename glob to filter by, e.g. "*.py".',
    )
    context_lines: int = Field(
        default=0,
        ge=0,
        le=MAX_CONTEXT_LINES,
        description="`search`: lines of context before and after each match.",
    )
    max_results: int = Field(
        default=100,
        ge=1,
        le=MAX_RESULTS,
        description="`search`: maximum matches to return in one page.",
    )
    offset: int = Field(
        default=0,
        ge=0,
        le=MAX_SEARCH_MATCHES_SCANNED,
        description=(
            "`search`: how many matches to skip. Pass a previous call's "
            "`next_offset` to continue."
        ),
    )

    # git
    git_command: Literal["status", "diff", "log", "show"] | None = Field(
        default=None, description="`git`: the subcommand to run."
    )
    git_ref: str | None = Field(
        default=None,
        description=(
            "`git`: commit or ref for `log`/`show`. Defaults to HEAD for "
            "`show`. Validated as a revision, never a free-form argument."
        ),
    )
    git_path: str | None = Field(
        default=None,
        description=(
            "`git`: restrict `status`/`diff`/`show` to one path. Guarded the "
            "same way as a file read."
        ),
    )
    git_cached: bool = Field(
        default=False, description="`git diff`: diff the index (--cached)."
    )
    git_stat: bool = Field(
        default=False, description="`git diff`/`show`: summarise only (--stat)."
    )
    git_max_count: int = Field(
        default=20,
        ge=1,
        le=200,
        description="`git log`: maximum commits to return.",
    )
    git_context: int = Field(
        default=3,
        ge=0,
        le=20,
        description="`git diff`/`show`: unified context lines.",
    )

    # version
    executable: str | None = Field(
        default=None,
        description="`version`: which executable to query, from a fixed set.",
    )

    @model_validator(mode="after")
    def _validate_per_command(self) -> InspectAction:
        if self.command == "search":
            if not self.pattern:
                raise ValueError("search requires a non-empty pattern")
            if len(self.pattern) > MAX_PATTERN_CHARS:
                raise ValueError(
                    f"pattern is longer than {MAX_PATTERN_CHARS} characters"
                )
            try:
                timeout_regex.compile(self.pattern)
            except timeout_regex.error as exc:
                raise ValueError(f"invalid regular expression: {exc}") from exc
        elif self.command == "git":
            if self.git_command is None:
                raise ValueError("git requires git_command")
            if self.git_ref is not None and not _REVISION_RE.match(self.git_ref):
                raise ValueError(
                    f"git_ref {self.git_ref!r} is not a plain revision name"
                )
        elif self.command == "version":
            if self.executable not in _VERSION_ARGV:
                raise ValueError(
                    f"unsupported executable {self.executable!r}; expected one "
                    "of: " + ", ".join(sorted(_VERSION_ARGV))
                )
        return self


class SearchMatch(BaseModel):
    path: str = Field(description="Workspace-relative path of the matching file.")
    line: int = Field(description="1-based line number of the match.")
    text: str = Field(description="The matching line, clipped if very long.")
    context_before: list[str] = Field(default_factory=list)
    context_after: list[str] = Field(default_factory=list)


class InspectObservation(Observation):
    """Structured result plus a text rendering, so the model sees both."""

    command: Literal["search", "git", "version", "env"]

    pattern: str | None = None
    search_root: str | None = None
    include: str | None = None
    matches: list[SearchMatch] = Field(default_factory=list)
    match_count: int = 0
    match_count_exact: bool = True
    files_scanned: int = 0
    offset: int = 0
    next_offset: int | None = None
    truncated: bool = False

    git_command: str | None = None
    argv: list[str] = Field(default_factory=list)
    stdout: str | None = None
    stderr: str | None = None
    exit_code: int | None = None

    executable: str | None = None
    version: str | None = None

    metadata: dict[str, str] = Field(default_factory=dict)


def _text(text: str) -> list[TextContent | ImageContent]:
    return [TextContent(text=text)]


class InspectExecutor(ToolExecutor):
    """Run one validated read action, confined to the workspace."""

    def __init__(self, root: str, permission: str) -> None:
        if permissions.normalise(permission) != INSPECT_PERMISSION:
            raise permissions.PermissionDenied(
                "the inspect tool is only granted by the 'inspect' permission "
                f"preset, not {permission!r}"
            )
        self._root = Path(root).resolve()
        self._permission = permission

    # -- shared guards -------------------------------------------------

    def _approve(self, raw: str | os.PathLike[str]) -> Path:
        """Resolve and approve one path, raising `PermissionDenied` if refused."""
        return permissions.check_path(
            str(raw),
            root=self._root,
            permission=INSPECT_PERMISSION,
            writing=False,
        )

    def _approve_optional(self, raw: str | os.PathLike[str]) -> Path | None:
        try:
            return self._approve(raw)
        except permissions.PermissionDenied:
            return None

    def _relative(self, approved: Path) -> str:
        return os.path.relpath(str(approved), str(self._root))

    # -- dispatch ------------------------------------------------------

    def __call__(
        self, action: InspectAction, _conversation: Any = None
    ) -> InspectObservation:
        try:
            if action.command == "search":
                return self._search(action)
            if action.command == "git":
                return self._git(action)
            if action.command == "version":
                return self._version(action)
            return self._env(action)
        except permissions.PermissionDenied as denied:
            return InspectObservation.from_text(
                text=f"Refused: {denied}",
                is_error=True,
                command=action.command,
            )
        except TimeoutError:
            return InspectObservation.from_text(
                text=(
                    "Search refused: the regular expression exceeded the "
                    "per-match execution deadline."
                ),
                is_error=True,
                command=action.command,
                pattern=action.pattern,
            )

    # -- search --------------------------------------------------------

    def _search(self, action: InspectAction) -> InspectObservation:
        assert action.pattern is not None
        start = self._approve(action.path) if action.path else self._root
        if not start.is_dir():
            return InspectObservation.from_text(
                text=f"Not a directory: {self._relative(start)}",
                is_error=True,
                command="search",
                search_root=self._relative(start),
            )

        regex = timeout_regex.compile(action.pattern)
        matches: list[SearchMatch] = []
        total = 0
        scan_limit_hit = False
        files_scanned = 0
        clipped = False

        for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
            # Symlinked directories are pruned rather than followed: os.walk
            # would not descend them with followlinks=False, and a link that
            # pointed elsewhere inside the workspace would otherwise make the
            # walk's semantics depend on a detail of os.walk.
            dirnames[:] = sorted(
                name
                for name in dirnames
                if name not in _SEARCH_SKIP_DIRS
                and not (Path(dirpath) / name).is_symlink()
                and self._approve_optional(Path(dirpath) / name) is not None
            )
            for name in sorted(filenames):
                if action.include and not fnmatch.fnmatch(name, action.include):
                    continue
                candidate = self._approve_optional(Path(dirpath) / name)
                if candidate is None or not candidate.is_file():
                    continue
                if total >= MAX_SEARCH_MATCHES_SCANNED:
                    scan_limit_hit = True
                    break
                found, count, was_clipped, file_limit_hit = self._scan_file(
                    candidate,
                    regex,
                    action,
                    skip=max(0, action.offset - total),
                    take=max(0, action.max_results - len(matches)),
                    scan_limit=MAX_SEARCH_MATCHES_SCANNED - total,
                )
                files_scanned += 1
                clipped = clipped or was_clipped
                matches.extend(found)
                total += count
                if file_limit_hit:
                    scan_limit_hit = True
                    break
            if scan_limit_hit:
                break

        source_clipped = clipped
        page, budget_clipped = _fit_search_page(matches)
        clipped = clipped or budget_clipped
        returned_end = action.offset + len(page)
        has_more_in_scanned_window = returned_end < total
        has_more_before_scan_cap = (
            scan_limit_hit and returned_end < MAX_SEARCH_MATCHES_SCANNED
        )
        next_offset = (
            returned_end
            if page and (has_more_in_scanned_window or has_more_before_scan_cap)
            else None
        )
        count_text = f"at least {total}" if scan_limit_hit else str(total)
        text = (
            f"{count_text} match(es) for {action.pattern!r} under "
            f"{self._relative(start)}; returning {len(page)} from offset "
            f"{action.offset}."
        )
        if clipped:
            text += " Some lines or files were clipped."
        return InspectObservation(
            content=_text(text),
            command="search",
            pattern=action.pattern,
            search_root=self._relative(start),
            include=action.include,
            matches=page,
            match_count=total,
            match_count_exact=not scan_limit_hit and not source_clipped,
            files_scanned=files_scanned,
            offset=action.offset,
            next_offset=next_offset,
            truncated=next_offset is not None or clipped or scan_limit_hit,
        )

    def _scan_file(
        self,
        path: Path,
        regex: Any,
        action: InspectAction,
        *,
        skip: int,
        take: int,
        scan_limit: int,
    ) -> tuple[list[SearchMatch], int, bool, bool]:
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                return [], 0, True, False
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return [], 0, False, False

        lines = _source_lines(text)
        found: list[SearchMatch] = []
        count = 0
        clipped = False
        limit_hit = False
        for index, line in enumerate(lines):
            if len(line) > MAX_LINE_CHARS:
                line = line[:MAX_LINE_CHARS]
                clipped = True
            if not regex.search(line, timeout=REGEX_TIMEOUT_SECONDS):
                continue
            if count >= scan_limit:
                limit_hit = True
                break
            include_match = count >= skip and len(found) < take
            count += 1
            if not include_match:
                continue
            before = [
                value[:MAX_LINE_CHARS]
                for value in lines[max(0, index - action.context_lines) : index]
            ]
            after = [
                value[:MAX_LINE_CHARS]
                for value in lines[index + 1 : index + 1 + action.context_lines]
            ]
            found.append(
                SearchMatch(
                    path=self._relative(path),
                    line=index + 1,
                    text=line,
                    context_before=before,
                    context_after=after,
                )
            )
        return found, count, clipped, limit_hit

    # -- git -----------------------------------------------------------

    def _git_executable(self) -> str:
        resolved = _resolve_executable("git", self._root)
        if resolved is None:
            raise FileNotFoundError("git is not available outside the workspace")
        return resolved

    def _git_env(self, git_executable: str) -> dict[str, str]:
        env = sanitized_environment()
        env["PATH"] = os.pathsep.join([os.path.dirname(git_executable), os.defpath])
        return env

    def _git_config_overrides(self) -> list[str]:
        """Config forced off on the command line, ahead of repository config.

        A repository may set any of these to a command it controls. `git -c`
        wins over every file, so the flags below are what actually decide.
        """
        return [
            "-c",
            "core.pager=cat",
            "-c",
            "core.fsmonitor=false",
            "-c",
            f"core.hooksPath={_sandbox_home()}",
            "-c",
            "log.showSignature=false",
            "-c",
            "credential.helper=",
            "-c",
            "diff.external=",
            "-c",
            "color.ui=false",
            "-c",
            "core.quotepath=false",
        ]

    def _protected_git_pathspecs(self, toplevel: Path) -> list[str]:
        """Top-relative exclusions for files aliasing runtime-owned state."""
        protected = permissions.runtime_secret_identities()
        if not protected:
            return []
        pathspecs: list[str] = []
        for dirpath, dirnames, filenames in os.walk(toplevel, followlinks=False):
            dirnames[:] = [
                name
                for name in dirnames
                if name != ".git" and not (Path(dirpath) / name).is_symlink()
            ]
            for name in filenames:
                candidate = Path(dirpath) / name
                try:
                    info = candidate.stat()
                except OSError:
                    continue
                if info.st_ino and (info.st_dev, info.st_ino) in protected:
                    relative = candidate.relative_to(toplevel).as_posix()
                    pathspecs.append(f":(top,exclude,literal){relative}")
        return sorted(pathspecs)

    def _run_git(
        self, tail: list[str], cwd: Path, timeout: int = 20
    ) -> tuple[subprocess.CompletedProcess[str], list[str]]:
        git = self._git_executable()
        argv = [
            git,
            "--no-pager",
            "--no-optional-locks",
            *self._git_config_overrides(),
            *tail,
        ]
        completed = subprocess.run(
            argv,
            cwd=cwd,
            env=self._git_env(git),
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=timeout,
        )
        return completed, argv

    def _git(self, action: InspectAction) -> InspectObservation:
        cwd = self._approve(action.path) if action.path else self._root
        if not cwd.is_dir():
            return InspectObservation.from_text(
                text=f"Not a directory: {self._relative(cwd)}",
                is_error=True,
                command="git",
                git_command=action.git_command,
            )

        top_proc, _ = self._run_git(
            [
                "rev-parse",
                "--path-format=absolute",
                "--show-toplevel",
                "--absolute-git-dir",
                "--git-common-dir",
            ],
            cwd,
        )
        if top_proc.returncode != 0:
            return InspectObservation.from_text(
                text=(
                    "Not a git work tree, or git refused to inspect it. "
                    f"{top_proc.stderr.strip()}"
                ),
                is_error=True,
                command="git",
                git_command=action.git_command,
            )
        top_lines = top_proc.stdout.splitlines()
        if len(top_lines) < 3:
            return InspectObservation.from_text(
                text="git did not report a work tree root",
                is_error=True,
                command="git",
                git_command=action.git_command,
            )
        # The work tree root must be inside the workspace. `core.worktree` in a
        # hostile repository config is one way this command could otherwise be
        # pointed at a directory the session was never given.
        try:
            toplevel = self._approve(top_lines[0].strip())
            self._approve(top_lines[1].strip())
            self._approve(top_lines[2].strip())
        except permissions.PermissionDenied as denied:
            return InspectObservation.from_text(
                text=(
                    "Refused: git work tree or metadata directory is outside "
                    f"the workspace ({denied})"
                ),
                is_error=True,
                command="git",
                git_command=action.git_command,
            )

        tail = [action.git_command or ""]
        if action.git_command == "status":
            tail += ["--porcelain=v1", "--untracked-files=all", "--no-renames"]
        elif action.git_command == "diff":
            tail += [*_GIT_DIRECTORY_FLAGS, f"-U{action.git_context}"]
            if action.git_cached:
                tail.append("--cached")
            if action.git_stat:
                tail.append("--stat")
        elif action.git_command == "log":
            tail += [
                *_GIT_DIRECTORY_FLAGS,
                f"-n{action.git_max_count}",
                "--date=short",
                "--format=%h%x09%ad%x09%s",
            ]
        elif action.git_command == "show":
            tail += [
                *_GIT_DIRECTORY_FLAGS,
                f"-U{action.git_context}",
                "--date=short",
                "--format=fuller",
            ]
            if action.git_stat:
                tail.append("--stat")

        if action.git_command in ("log", "show"):
            tail.append(action.git_ref or "HEAD")
        if action.git_path is not None:
            try:
                approved = self._approve(
                    action.git_path
                    if Path(action.git_path).is_absolute()
                    else toplevel / action.git_path
                )
            except permissions.PermissionDenied as denied:
                return InspectObservation.from_text(
                    text=f"Refused: {denied}",
                    is_error=True,
                    command="git",
                    git_command=action.git_command,
                )
            tail += ["--", os.path.relpath(str(approved), str(cwd))]

        if action.git_command in ("status", "diff", "show"):
            protected_pathspecs = self._protected_git_pathspecs(toplevel)
            if protected_pathspecs:
                if "--" not in tail:
                    tail.append("--")
                tail.extend(protected_pathspecs)

        completed, argv = self._run_git(tail, cwd)
        stdout, clipped = _clip(completed.stdout.rstrip("\n"))
        stderr, stderr_clipped = _clip(completed.stderr.rstrip("\n"))
        text = stdout or "(no output)"
        if stderr:
            text += f"\n[stderr]\n{stderr}"
        return InspectObservation(
            content=_text(text),
            command="git",
            git_command=action.git_command,
            argv=argv,
            stdout=stdout,
            stderr=stderr,
            exit_code=completed.returncode,
            truncated=clipped or stderr_clipped,
            is_error=completed.returncode != 0,
        )

    # -- version and environment --------------------------------------

    def _version(self, action: InspectAction) -> InspectObservation:
        assert action.executable is not None
        resolved = _resolve_executable(action.executable, self._root)
        if resolved is None:
            return InspectObservation.from_text(
                text=(f"{action.executable!r} is not available outside the workspace"),
                is_error=True,
                command="version",
                executable=action.executable,
            )
        env = sanitized_environment()
        env["PATH"] = os.pathsep.join([os.path.dirname(resolved), os.defpath])
        completed = subprocess.run(
            [resolved, *_VERSION_ARGV[action.executable]],
            cwd=self._root,
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            check=False,
            timeout=10,
        )
        raw = (completed.stdout + completed.stderr).strip()
        version, clipped = _clip(raw, limit=4096)
        return InspectObservation(
            content=_text(version or "(no version output)"),
            command="version",
            executable=resolved,
            version=version,
            exit_code=completed.returncode,
            truncated=clipped,
            is_error=completed.returncode != 0,
        )

    def _env(self, _action: InspectAction) -> InspectObservation:
        env = sanitized_environment()
        names = sorted(env)
        reported = {
            name: env[name] for name in sorted(_ENV_VALUE_ALLOWLIST) if name in env
        }
        metadata = {
            "platform": sys.platform,
            "python_version": sys.version.split()[0],
            "python_executable": sys.executable,
            "workspace": str(self._root),
            "environment_names": ", ".join(names),
        }
        metadata.update({f"env.{key}": value for key, value in reported.items()})
        text = (
            "Runtime metadata:\n"
            + "\n".join(f"{key}: {value}" for key, value in metadata.items())
            + "\nValues of environment variables are never returned; names are, "
            "and credential-shaped names are removed before this report."
        )
        return InspectObservation(
            content=_text(text),
            command="env",
            metadata=metadata,
        )


TOOL_DESCRIPTION = """Read-only inspection tool.
* `search`: find a regular expression in files under the workspace, returning path, line and optional context. Paged with `offset`/`next_offset`.
* `git`: narrow read commands under the workspace -- `status`, `diff`, `log`, `show` -- built from validated parameters, with external diff, textconv, pager, fsmonitor, hooks and global config disabled.
* `version`: run `--version` for one executable from a fixed list.
* `env`: report runtime and environment metadata. Environment variable names are listed; their values are never returned and credential-shaped names are removed.

Every path is resolved and confined to the workspace. A symlink that leaves the workspace, or a hard link to the runtime's own credential, is refused. This tool cannot write files and is not a shell: there is no field that carries a command line.
"""  # noqa: E501


class InspectTool(ToolDefinition[InspectAction, InspectObservation]):
    """Registers as `inspect`; the `inspect` preset's only command surface."""

    @classmethod
    def create(
        cls,
        conv_state: ConversationState,
        permission: str | None = None,
    ) -> Sequence[InspectTool]:
        preset = permissions.normalise(permission)
        if preset != INSPECT_PERMISSION:
            raise permissions.PermissionDenied(
                "the inspect tool is only available under the 'inspect' preset; "
                f"this profile is {preset!r}"
            )
        root = conv_state.workspace.working_dir
        note = (
            f"\n\nYour workspace is {root}. This session runs under the "
            f"'{preset}' permission preset: " + permissions.DESCRIPTIONS[preset]
        )
        return [
            cls(
                action_type=InspectAction,
                observation_type=InspectObservation,
                description=TOOL_DESCRIPTION + note,
                annotations=ToolAnnotations(
                    title="inspect",
                    readOnlyHint=True,
                    destructiveHint=False,
                    idempotentHint=True,
                    openWorldHint=False,
                ),
                executor=InspectExecutor(root=root, permission=preset),
            )
        ]


register_tool(InspectTool.name, InspectTool)
