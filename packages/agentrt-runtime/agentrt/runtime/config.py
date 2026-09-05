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


def repo_env_file() -> Path:
    """Locate the repository `.env` by walking up from this module.

    The `.env` lives with the repository, not with state, so operator
    configuration is not duplicated when state is relocated.
    """
    start = Path(__file__).resolve().parent
    for directory in [start, *start.parents]:
        candidate = directory / ".env"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(
        f"No .env file found in {start} or any of its parent directories"
    )


def load_router_config(env_path: Path | None = None) -> RouterConfig:
    """Build RouterConfig from a dotenv file.

    Missing or blank required keys are reported together to make configuration
    problems actionable in a single run.
    """
    if env_path is None:
        env_path = repo_env_file()

    env = read_dotenv(env_path)

    required = (
        "AGENTRT_9ROUTER_API_KEY",
        "AGENTRT_9ROUTER_BASE_URL",
        "AGENTRT_DEFAULT_MODEL",
    )
    missing = [key for key in required if not env.get(key, "").strip()]
    if missing:
        raise ValueError(
            "Missing required keys in dotenv file: " + ", ".join(missing)
        )

    return RouterConfig(
        api_key=env["AGENTRT_9ROUTER_API_KEY"],
        base_url=env["AGENTRT_9ROUTER_BASE_URL"].rstrip("/"),
        model=env["AGENTRT_DEFAULT_MODEL"],
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
