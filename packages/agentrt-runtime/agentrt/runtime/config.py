from __future__ import annotations

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


REQUIRED_KEYS = (
    "AGENTRT_9ROUTER_API_KEY",
    "AGENTRT_9ROUTER_BASE_URL",
    "AGENTRT_DEFAULT_MODEL",
)


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


def resolve_settings() -> tuple[dict[str, str], str]:
    """Collect configuration from the first source that supplies it.

    Order is process environment, then the state directory, then a development
    checkout. The environment comes first because it is the only source an
    orchestrator can set without touching the filesystem, and the checkout
    comes last because relying on it is exactly what stops agentrt working
    outside this repository.

    Returns the values together with a description of where they came from, so
    a configuration mistake can be reported against a real path instead of a
    guess.
    """
    for key in REQUIRED_KEYS:
        if not os.environ.get(key, "").strip():
            break
    else:
        return {key: os.environ[key] for key in REQUIRED_KEYS}, "process environment"

    values: dict[str, str] = {}
    sources: list[str] = []
    for path in (repo_env_file(), config_file()):
        if path is not None and path.is_file():
            values.update(read_dotenv(path))
            sources.append(str(path))

    # The environment still wins over any file for keys it does define, so a
    # single variable can override one setting without restating the rest.
    for key in REQUIRED_KEYS:
        if os.environ.get(key, "").strip():
            values[key] = os.environ[key]
            if "process environment" not in sources:
                sources.append("process environment")

    return values, ", ".join(sources) if sources else "no configuration source"


def load_router_config(env_path: Path | None = None) -> RouterConfig:
    """Build RouterConfig from the resolved configuration.

    Missing or blank required keys are reported together, with the source that
    was consulted, so one run tells the operator both what is absent and where
    to put it.
    """
    if env_path is not None:
        values, source = read_dotenv(env_path), str(env_path)
    else:
        values, source = resolve_settings()

    missing = [key for key in REQUIRED_KEYS if not values.get(key, "").strip()]
    if missing:
        raise ValueError(
            f"Missing required settings: {', '.join(missing)}. "
            f"Checked {source}. Set them in the environment or write them to "
            f"{config_file()}."
        )

    return RouterConfig(
        api_key=values["AGENTRT_9ROUTER_API_KEY"],
        base_url=values["AGENTRT_9ROUTER_BASE_URL"].rstrip("/"),
        model=values["AGENTRT_DEFAULT_MODEL"],
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

        if (
            len(value) >= 2
            and value[0] == value[-1]
            and value[0] in ("'", '"')
        ):
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
