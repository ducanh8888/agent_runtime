from __future__ import annotations

import importlib.metadata
import logging
import os
import re
import secrets
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


logger = logging.getLogger(__name__)

DEFAULT_MAX_ITERATIONS = 100_000
DEFAULT_MAX_OUTPUT_TOKENS = 32_000


def default_max_output_tokens() -> int:
    """Default output-token budget, overrideable by the dispatcher environment."""
    raw = os.environ.get("AGENTRT_MAX_OUTPUT_TOKENS", "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_MAX_OUTPUT_TOKENS
    except ValueError:
        return DEFAULT_MAX_OUTPUT_TOKENS


def default_max_iterations() -> int:
    """Default agent run budget, overrideable by the dispatcher environment."""
    raw = os.environ.get("AGENTRT_DEFAULT_MAX_ITERATIONS", "").strip()
    if not raw:
        return DEFAULT_MAX_ITERATIONS
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_MAX_ITERATIONS


@dataclass(frozen=True)
class RouterConfig:
    """Immutable router settings.

    Keeping api_key a dedicated field allows __repr__ to redact it, so an
    accidental log or traceback cannot leak credentials.
    """

    api_key: str
    base_url: str
    model: str

    def __repr__(self) -> str:
        return (
            f"RouterConfig(api_key=<redacted>, base_url={self.base_url!r}, "
            f"model={self.model!r})"
        )


RUNTIME_DISTRIBUTION = "agentrt-runtime"


def runtime_version() -> str:
    """Return the installed AgentRT runtime version.

    The MCP initialize response must identify AgentRT, not the ``mcp`` library
    that carries the tool surface, and the low-level server otherwise reports
    the library's own version. A source checkout without installed distribution
    metadata reports ``"unknown"`` rather than failing to start.
    """
    try:
        return importlib.metadata.version(RUNTIME_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def state_dir() -> Path:
    """Return the canonical runtime state directory and create it.

    Everything that persists requires a stable, relocatable home. This is the
    only authority for resolving that home.
    """
    configured = os.environ.get("AGENTRT_STATE_DIR")
    if configured:
        path = Path(configured).resolve()
    elif os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        path = Path(local) / "agentrt" if local else Path.home() / ".agentrt"
    else:
        path = Path.home() / ".agentrt"

    path.mkdir(parents=True, exist_ok=True)
    return path


def daemon_file() -> Path:
    """Path to the daemon's JSON state file."""
    return state_dir() / "daemon.json"


def log_file() -> Path:
    """Path to the daemon's rotating log file."""
    return state_dir() / "daemon.log"


def system_prompt_suffix_file() -> Path:
    """Path to the locally saved unnamed dispatch system-prompt suffix."""
    return state_dir() / "system-prompt-suffix.txt"


def system_prompt_suffixes_dir() -> Path:
    """Directory containing named dispatch system-prompt suffixes."""
    return state_dir() / "system-prompt-suffixes"


def active_system_prompt_suffix_file() -> Path:
    """Path storing the active named suffix selection."""
    return state_dir() / "system-prompt-suffix-active.txt"


def _validate_system_prompt_suffix_name(name: str) -> str:
    if not isinstance(name, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", name):
        raise ValueError(
            "suffix name must begin with a lowercase letter or digit and contain "
            "only lowercase letters, digits, '-' or '_' (maximum 64 characters)"
        )
    return name


def named_system_prompt_suffix_file(name: str) -> Path:
    """Path to a named suffix after validating its name."""
    validated_name = _validate_system_prompt_suffix_name(name)
    return system_prompt_suffixes_dir() / f"{validated_name}.txt"


def save_system_prompt_suffix(name: str, text: str) -> None:
    """Save or replace a named suffix."""
    path = named_system_prompt_suffix_file(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def get_system_prompt_suffix(name: str) -> str:
    """Read a named suffix, raising when the name is not saved."""
    path = named_system_prompt_suffix_file(name)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ValueError(f"system prompt suffix {name!r} is not saved") from exc


def list_system_prompt_suffixes() -> list[str]:
    """List saved suffix names in stable sorted order."""
    directory = system_prompt_suffixes_dir()
    if not directory.is_dir():
        return []
    return sorted(path.stem for path in directory.glob("*.txt") if path.is_file())


def active_system_prompt_suffix_name() -> str | None:
    """Return the active named suffix if one is selected."""
    try:
        name = active_system_prompt_suffix_file().read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        return _validate_system_prompt_suffix_name(name) if name else None
    except ValueError:
        return None


def use_system_prompt_suffix(name: str) -> None:
    """Select a saved named suffix as the default for future dispatches."""
    name = _validate_system_prompt_suffix_name(name)
    get_system_prompt_suffix(name)
    active_system_prompt_suffix_file().write_text(name, encoding="utf-8")


def delete_system_prompt_suffix(name: str) -> None:
    """Delete a saved suffix and clear it if selected as active."""
    path = named_system_prompt_suffix_file(name)
    if not path.exists():
        raise ValueError(f"system prompt suffix {name!r} is not saved")
    path.unlink()
    if active_system_prompt_suffix_name() == name:
        active_system_prompt_suffix_file().unlink(missing_ok=True)


def system_prompt_suffix() -> str | None:
    """Return active named suffix, falling back to the legacy unnamed suffix.

    The old single-suffix file and API remain compatible. When a named set is
    active it takes precedence; otherwise the legacy suffix remains the default.
    """
    name = active_system_prompt_suffix_name()
    if name is not None:
        try:
            return get_system_prompt_suffix(name) or None
        except ValueError:
            return None
    try:
        value = system_prompt_suffix_file().read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    return value or None


def set_system_prompt_suffix(value: str) -> None:
    """Save the legacy unnamed dispatch suffix and clear named selection."""
    active_system_prompt_suffix_file().unlink(missing_ok=True)
    system_prompt_suffix_file().write_text(value, encoding="utf-8")


def clear_active_system_prompt_suffix() -> None:
    """Clear the active named suffix while preserving all saved sets."""
    active_system_prompt_suffix_file().unlink(missing_ok=True)


def clear_system_prompt_suffix() -> None:
    """Clear both the legacy suffix and active named default."""
    active_system_prompt_suffix_file().unlink(missing_ok=True)
    system_prompt_suffix_file().unlink(missing_ok=True)


def restrict_to_owner(path: Path) -> None:
    """Make a credential file readable and writable by its owner only.

    POSIX mode 0600. On Windows, ``icacls`` drops inherited entries and grants
    only the current user; a failure there is logged, never raised.
    """
    if os.name != "nt":
        os.chmod(path, 0o600)
        return
    username = os.environ.get("USERNAME")
    if not username:
        logger.warning("Could not restrict ACL for %s: USERNAME is unset", path)
        return
    try:
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{username}:(R,W)"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=30,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Could not restrict ACL for %s: %s", path, exc)
        return
    if result.returncode:
        logger.warning("Could not restrict ACL for %s: %s", path, result.stderr.strip())


def secret_key_file() -> Path:
    """Path to the daemon's persistent secret-encryption key."""
    return state_dir() / "secret.key"


def secret_key() -> str:
    """Return this installation's stable ``AGENTRT_SECRET_KEY``, generating
    and persisting one on first use.

    Without it, the vendored agent server has no cipher and redacts a
    conversation's secrets (an LLM's API key among them) when it persists
    state to disk -- silently, with only a log line, not an error. A fresh
    session dispatched directly is unaffected (it never round-trips through
    a save/reload before its first real work), but a *fork* always does: the
    server writes the fork's agent to its own ``base_state.json`` and reloads
    it from there before running, per ``fork_conversation``'s own comment
    that "the agent is NOT stored in meta.json". Reproduced live: a fork of a
    session that had just run three successful tool calls failed its very
    first step with a provider credential error, at `iterations_used: 0`.

    Unlike the per-start session auth token, this key must be *stable*
    across restarts -- it decrypts what earlier processes encrypted, so
    rotating it on every start would make already-persisted secrets
    unreadable rather than merely absent, which is a worse failure than the
    one this exists to fix. Generated once with the same
    ``secrets.token_urlsafe`` the session token already uses, and written
    with the same tighten-after-write pattern `daemon.py` uses for
    ``daemon.json``, so a key this file did not create is never silently
    overwritten. docs/plans/deepseek-hardening.md H10 item 2, 2026-09-18.
    """
    path = secret_key_file()
    try:
        existing = path.read_text().strip()
    except FileNotFoundError:
        existing = ""
    if existing:
        return existing

    generated = secrets.token_urlsafe(32)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".secret-", suffix=".key")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(generated)
            fh.flush()
            os.fsync(fh.fileno())
        restrict_to_owner(Path(tmp))
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path.read_text().strip()


class ConfigConflictError(ValueError):
    """Two aliases for one setting disagree at the same precedence."""


#: Canonical setting name to the spellings accepted for it, neutral first.
#: Per-setting aliases let the neutral names land without breaking an
#: operator's existing ``AGENTRT_9ROUTER_*`` configuration.
SETTING_ALIASES: dict[str, tuple[str, ...]] = {
    "AGENTRT_API_KEY": ("AGENTRT_API_KEY", "AGENTRT_9ROUTER_API_KEY"),
    "AGENTRT_BASE_URL": ("AGENTRT_BASE_URL", "AGENTRT_9ROUTER_BASE_URL"),
    "AGENTRT_DEFAULT_MODEL": ("AGENTRT_DEFAULT_MODEL",),
}

REQUIRED_KEYS: tuple[str, ...] = tuple(SETTING_ALIASES)

SECRET_SETTINGS: frozenset[str] = frozenset({"AGENTRT_API_KEY"})


@dataclass(frozen=True)
class SettingProvenance:
    """Where one resolved setting's value came from."""

    setting: str
    value: str | None
    source: str
    alias: str | None

    @property
    def present(self) -> bool:
        return self.value is not None


def config_file() -> Path:
    """Path to the operator's credential file inside the state directory.

    This is the home for an installed agentrt. State is the only directory the
    runtime can rely on existing, so it is the only place configuration can
    live without assuming a particular checkout is present on the machine.
    """
    return state_dir() / ".env"


def repo_env_file() -> Path | None:
    """Locate a development `.env` by walking up from this module.

    Present so a checkout keeps working with the file where a developer expects
    it. It returns None rather than raising: an installed copy has no
    repository above it, and that is the normal case, not an error.
    """
    start = Path(__file__).resolve().parent
    for directory in [start, *start.parents]:
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    return None


def _alias_names(setting: str) -> str:
    return " or ".join(SETTING_ALIASES[setting])


def _resolve_layer(entries: dict[str, str]) -> dict[str, tuple[str, str]]:
    """Resolve aliases within one precedence layer.

    Raises :class:`ConfigConflictError` when two spellings for one setting
    carry different non-empty values, because there is no safe way to guess
    which endpoint or credential the operator meant.
    """
    resolved: dict[str, tuple[str, str]] = {}
    for setting, aliases in SETTING_ALIASES.items():
        supplied = [(alias, entries.get(alias, "")) for alias in aliases]
        non_empty = [(alias, value) for alias, value in supplied if value.strip()]
        if not non_empty:
            continue
        if len({value.strip() for _, value in non_empty}) > 1:
            raise ConfigConflictError(
                f"{setting} is set more than once with different values "
                f"({', '.join(alias for alias, _ in non_empty)}); remove one. "
                "Values are not shown."
            )
        alias, value = next(
            (pair for pair in non_empty if pair[0] == setting), non_empty[0]
        )
        resolved[setting] = (value, alias)
    return resolved


def _layers() -> list[tuple[str, dict[str, str]]]:
    """Configuration sources in precedence order: env, state, checkout."""
    layers: list[tuple[str, dict[str, str]]] = [
        ("process environment", dict(os.environ))
    ]
    state = config_file()
    if state.is_file():
        layers.append((str(state), read_dotenv(state)))
    dev = repo_env_file()
    if dev is not None and dev != state:
        layers.append((str(dev), read_dotenv(dev)))
    return layers


def resolve_settings_provenance() -> dict[str, SettingProvenance]:
    """Resolve every setting and record which layer and alias supplied it.

    Aliases are resolved inside each layer first, so the neutral name and its
    legacy spelling at the same precedence are either equal (accepted) or a
    :class:`ConfigConflictError`. Higher-precedence layers win per setting,
    which keeps the environment ahead of the state file while still letting a
    single value be overridden without restating the rest.
    """
    out = {
        setting: SettingProvenance(setting, None, "no configuration source", None)
        for setting in REQUIRED_KEYS
    }
    for source, entries in _layers():
        for setting, (value, alias) in _resolve_layer(entries).items():
            if not out[setting].present:
                out[setting] = SettingProvenance(setting, value, source, alias)
    return out


def resolve_settings() -> tuple[dict[str, str], str]:
    """Collect configuration from the highest source that supplies each key.

    Returns canonical setting names to values, plus a description of the
    sources consulted. Provenance per setting is available from
    :func:`resolve_settings_provenance`.
    """
    provenance = resolve_settings_provenance()
    values: dict[str, str] = {}
    sources: list[str] = []
    for setting in REQUIRED_KEYS:
        entry = provenance[setting]
        if entry.present:
            values[setting] = str(entry.value)
            if entry.source not in sources:
                sources.append(entry.source)
    return values, ", ".join(sources) if sources else "no configuration source"


def describe_settings() -> list[dict]:
    """Report provenance per setting with secret values redacted."""
    provenance = resolve_settings_provenance()
    report: list[dict] = []
    for setting in REQUIRED_KEYS:
        entry = provenance[setting]
        if setting in SECRET_SETTINGS:
            value = "<set>" if entry.present else None
        else:
            value = entry.value
        report.append(
            {
                "setting": setting,
                "aliases": list(SETTING_ALIASES[setting]),
                "source": entry.source if entry.present else None,
                "alias": entry.alias,
                "present": entry.present,
                "value": value,
            }
        )
    return report


def load_router_config(env_path: Path | None = None) -> RouterConfig:
    """Build RouterConfig from the resolved configuration.

    Missing or blank required keys are reported together, naming both the
    neutral and legacy spellings, with the source that was consulted. Aliases
    that disagree inside one layer raise :class:`ConfigConflictError`.
    """
    if env_path is not None:
        resolved = _resolve_layer(read_dotenv(env_path))
        source = str(env_path)
    else:
        provenance = resolve_settings_provenance()
        resolved = {
            setting: (str(entry.value), entry.alias)
            for setting, entry in provenance.items()
            if entry.present
        }
        source = (
            ", ".join(
                dict.fromkeys(
                    entry.source for entry in provenance.values() if entry.present
                )
            )
            or "no configuration source"
        )

    missing = [setting for setting in REQUIRED_KEYS if setting not in resolved]
    if missing:
        raise ValueError(
            "Missing required settings: "
            + ", ".join(_alias_names(setting) for setting in missing)
            + f". Checked {source}. Set them in the environment or write them to "
            f"{config_file()}."
        )

    api_key = resolved["AGENTRT_API_KEY"][0]
    base_url = resolved["AGENTRT_BASE_URL"][0]
    model = resolved["AGENTRT_DEFAULT_MODEL"][0]
    return RouterConfig(
        api_key=api_key,
        base_url=base_url.rstrip("/"),
        model=model,
    )


def read_dotenv(path: Path) -> dict[str, str]:
    """Parse a minimal dotenv file into a dictionary.

    The daemon reuses this parser for non-router environment values, so it is
    kept separate from RouterConfig construction.
    """
    values: dict[str, str] = {}
    text = path.read_text(encoding="utf-8")

    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        key, sep, value = line.partition("=")
        if not sep:
            continue

        key = key.strip()
        value = value.strip()

        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]

        values[key] = value

    return values


# 0 means unlimited, per max_running_sessions()'s own documented semantics.
# This client-side check sits on top of the daemon's own admission pool
# (`capacity`'s `limit`), which already queues gracefully instead of refusing
# when full -- so the safety this constant existed for is handled there, and
# an unrelated Claude/Codex session's real work should not hit an abrupt
# refusal from a second, redundant, stricter cap. Set AGENTRT_MAX_SESSIONS to
# reintroduce a per-dispatcher ceiling if a specific deployment wants one.
DEFAULT_MAX_RUNNING_SESSIONS = 0


def max_running_sessions() -> int:
    """How many sessions may run at once before dispatch refuses.

    Each running session holds a terminal and an open provider stream, so the
    ceiling that matters is the machine's, not the daemon's. Refusing with a
    clear message is better than accepting work that will make every session
    slower, including the ones already running.

    Zero or negative means no limit, for an operator who would rather find the
    real ceiling than guess at one.
    """
    raw = os.environ.get("AGENTRT_MAX_SESSIONS", "").strip()
    if not raw:
        return DEFAULT_MAX_RUNNING_SESSIONS
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_MAX_RUNNING_SESSIONS


def workers() -> int:
    """How many server processes the daemon runs (``AGENTRT_WORKERS``).

    One process is one event loop and in practice one core, which about 50
    long sessions saturate (#22, #24). Above 1, a front process spreads
    sessions over that many workers. Default 1: the single-process daemon.
    """
    raw = os.environ.get("AGENTRT_WORKERS", "").strip()
    if not raw:  # like the other settings: the process env, then <state-dir>/.env
        try:
            raw = read_dotenv(config_file()).get("AGENTRT_WORKERS", "").strip()
        except OSError:
            raw = ""
    try:
        return max(1, int(raw)) if raw else 1
    except ValueError:
        return 1


def _setting(name: str) -> str:
    """A setting from the process env, then ``<state-dir>/.env``; "" if unset."""
    raw = os.environ.get(name, "").strip()
    if raw:
        return raw
    try:
        return read_dotenv(config_file()).get(name, "").strip()
    except OSError:
        return ""


#: The LLM profile a dispatch selects for reasoning-heavy work.
HARD_LLM_PROFILE = "hard"


LLM_PROFILE_NAME_PATTERN = re.compile(r"[a-z0-9-]+\Z")


def llm_profiles() -> dict[str, str]:
    """Named router models from ``AGENTRT_LLM_PROFILES`` plus hard shorthand.

    The setting is a comma-separated list of ``name=model`` pairs. The legacy
    ``AGENTRT_HARD_MODEL`` setting fills in ``hard`` unless explicitly listed.
    """
    profiles: dict[str, str] = {}
    raw = _setting("AGENTRT_LLM_PROFILES")
    for entry in raw.split(",") if raw else ():
        name, separator, model = entry.partition("=")
        name, model = name.strip(), model.strip()
        if not separator or not model:
            raise ValueError(
                f"Invalid LLM profile entry {entry!r}; expected name=model"
            )
        if name == "default" or not LLM_PROFILE_NAME_PATTERN.fullmatch(name):
            raise ValueError(
                f"Invalid LLM profile name {name!r}; names must use lowercase "
                "letters, digits, or hyphens and cannot be 'default'"
            )
        if name in profiles:
            raise ValueError(f"Duplicate LLM profile name {name!r}")
        profiles[name] = model

    hard = _setting("AGENTRT_HARD_MODEL")
    if hard:
        profiles.setdefault(HARD_LLM_PROFILE, hard)
    return profiles


def hard_model() -> str | None:
    """The router model for reasoning-heavy sessions, if configured."""
    return llm_profiles().get(HARD_LLM_PROFILE)


def hard_condenser_max_tokens() -> int | None:
    """History cap for hard-model sessions (``AGENTRT_HARD_CONDENSER_MAX_TOKENS``).

    Unset: the same cap as every other session. A hard session works through
    more material, and its model takes a far larger context than the cap the
    default model was measured at, so an operator may raise it here.
    """
    raw = _setting("AGENTRT_HARD_CONDENSER_MAX_TOKENS")
    try:
        return int(raw) if raw and int(raw) > 0 else None
    except ValueError:
        return None


# The vendored server's own run-pool cap (`agentrt-server`'s Config.
# max_concurrent_runs, env var AGENTRT_MAX_CONCURRENT_RUNS) is a second,
# independent limit from AGENTRT_MAX_SESSIONS above -- one is this client
# refusing to dispatch, the other is the daemon refusing to *run* a step on
# an already-created session. It defaults to 10 there and cannot be set to
# its own "0 means unbounded" value through that field (`ge=1` on the
# pydantic Config -- 0 would fail Config construction and crash the daemon
# at startup, even though the code that reads it already treats <= 0 as
# unbounded for other callers). A very large number is the practical
# equivalent within that constraint: no realistic dispatch volume reaches
# it, and it costs nothing at rest -- the run pool is a ThreadPoolExecutor,
# which does not pre-spawn threads up to max_workers.
DEFAULT_MAX_CONCURRENT_RUNS = "100000"


# Codex bounds recursive spawning at `agent_max_depth` (default 3) and tells the
# agent to do the work itself past that. AgentRT had no equivalent, and the plan
# recorded the reason to hold off: no permission preset grants any tool that can
# call `dispatch_from`, so there was no live nesting to cap. What the cap is
# actually for is the *orchestrator* calling `dispatch_from` in a loop -- a
# chain a caller builds one hop at a time, each hop cheap, with the cost of the
# whole chain landing later. Three matches Codex, and this is a runaway guard,
# not a limit anyone hits while doing real work.
DEFAULT_MAX_FORK_DEPTH = 3


def max_fork_depth() -> int:
    """How many fork generations deep a `dispatch_from` chain may go.

    Depth counts ancestors: a session dispatched directly is depth 0, a fork of
    it is depth 1, and so on. A `dispatch_from` that would create a session past
    this many generations is refused before the fork is requested, so a runaway
    chain costs nothing rather than forking first and reporting later.

    Zero or negative means no limit, matching `AGENTRT_MAX_SESSIONS`.
    """
    raw = os.environ.get("AGENTRT_MAX_FORK_DEPTH", "").strip()
    if not raw:
        return DEFAULT_MAX_FORK_DEPTH
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_MAX_FORK_DEPTH


# A consumer report measured a transport-level idle ceiling of roughly 1800s
# between an orchestrator and a stdio MCP server -- unrelated to and below
# `wait`'s own documented `timeout` parameter, so a large requested timeout
# let that outer layer kill the connection with a generic error before
# `wait`'s own graceful still_running/timed_out answer was ever reached.
# Measured, not assumed, on 2026-09-17 (docs/plans/deepseek-hardening.md H8,
# the wait_*/transport-timeout entry) -- kept well under the reported ~1800s
# rather than at it, since that number is this one deployment's, not a spec.
DEFAULT_WAIT_SAFE_CEILING_SECONDS = 900.0


def wait_safe_ceiling_seconds() -> float:
    """The most `wait` will block in one call, regardless of `timeout`.

    A caller that wants to wait longer gets `still_running`/`timed_out` at
    this boundary and calls again -- which is also the shape `agentrt wait`
    (the CLI) or a backgrounded poll loop should use instead of holding the
    orchestrator's own turn open. Zero or negative disables the cap, for an
    operator confident their own transport has none.
    """
    raw = os.environ.get("AGENTRT_WAIT_SAFE_CEILING_SECONDS", "").strip()
    if not raw:
        return DEFAULT_WAIT_SAFE_CEILING_SECONDS
    try:
        return float(raw)
    except ValueError:
        return DEFAULT_WAIT_SAFE_CEILING_SECONDS
