Write the file `agentrt/runtime/config.py`.

It is the single place that answers "where does runtime state live" and "what
did the operator configure". Nothing else in the runtime may hardcode a path.

Imports allowed: standard library only.

Provide exactly this public surface:

1. `def state_dir() -> Path`
   - Honours the env var `AGENTRT_STATE_DIR` if set (absolute path).
   - Otherwise on Windows returns `Path(os.environ["LOCALAPPDATA"]) / "agentrt"`,
     falling back to `Path.home() / ".agentrt"` if LOCALAPPDATA is unset.
   - Otherwise returns `Path.home() / ".agentrt"`.
   - Creates the directory (parents=True, exist_ok=True) before returning.

2. Module-level path helpers, each a function taking no arguments and returning
   a Path under state_dir(): `daemon_file()` -> "daemon.json",
   `log_file()` -> "daemon.log", `repo_env_file()` is different, see below.

3. `@dataclass(frozen=True) class RouterConfig` with fields
   `api_key: str`, `base_url: str`, `model: str`.

4. `def load_router_config(env_path: Path | None = None) -> RouterConfig`
   - Reads a dotenv-style file. Default location: the repository `.env`, found by
     walking up from this file's directory until a directory containing a `.env`
     is found; raise `FileNotFoundError` with a clear message if none is found.
   - Parse rules: ignore blank lines and lines whose first non-space character is
     `#`; split on the FIRST `=`; strip whitespace from key and value; strip one
     layer of matching single or double quotes from the value.
   - Required keys: `AGENTRT_9ROUTER_API_KEY`, `AGENTRT_9ROUTER_BASE_URL`,
     `AGENTRT_DEFAULT_MODEL`. If any is missing or empty, raise `ValueError`
     naming every missing key at once, not just the first.
   - `base_url` must have any trailing "/" stripped.
   - The returned object must never be logged. Add `__repr__` to RouterConfig
     that shows base_url and model but replaces api_key with the literal
     `<redacted>`, so an accidental print or traceback cannot leak it.

5. `def read_dotenv(path: Path) -> dict[str, str]` — the parser from point 4,
   exposed separately because the daemon reuses it.

Order the file: imports, dataclass, then functions. Keep it under 120 lines.
