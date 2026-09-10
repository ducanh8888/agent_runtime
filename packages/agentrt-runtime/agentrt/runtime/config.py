from __future__ import annotations

import importlib.metadata
import os
from dataclasses import dataclass
from pathlib import Path


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


DEFAULT_MAX_RUNNING_SESSIONS = 5


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
