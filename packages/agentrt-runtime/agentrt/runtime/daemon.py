"""Own the lifecycle of a local, long-lived agent server process.

A session must survive the client that started it, which is only possible if
the agent process is fully detached from the client's console, stdio, and
process group.  This module is the only owner of that lifecycle.
"""

from __future__ import annotations

import json
import os
import secrets
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

from agentrt.runtime import config


def _deployment_llm_policy() -> dict[str, str] | None:
    """The deployment-only LLM policy for any server this runtime starts.

    Router-neutral since the OmniRoute migration (see
    docs/plans/omniroute-migration.md): derived from the operator's own
    resolved router configuration (``AGENTRT_API_KEY``/``AGENTRT_BASE_URL``/
    ``AGENTRT_DEFAULT_MODEL`` and their legacy ``AGENTRT_9ROUTER_*``
    aliases) rather than a literal, so the one contract this policy pins is
    always exactly the endpoint and model this deployment is actually
    configured to call -- whether that is a direct provider or one virtual
    model behind a router. No ``thinking_mode``/``reasoning_effort`` keys are
    set: a router's virtual model resolves to a different real backend per
    request, so this runtime cannot honestly assert a fixed reasoning
    contract for it (see ``bootstrap._build_llm``, which the server-side
    enforcement in ``agentrt.agent_server.deployment_policy`` must agree
    with -- both read the same resolved router config).

    Returns ``None`` when the router config cannot be resolved yet (e.g. a
    bare ``agentrt daemon start`` before ``.env`` is written) -- matching
    ``DeploymentLLMPolicy``'s own documented opt-in behavior: the server
    simply runs generic until a provider is configured, rather than this
    call failing and blocking a plain daemon start that used to succeed.
    """
    try:
        router = config.load_router_config()
    except ValueError:
        return None
    return {
        "model": router.model,
        "base_url": router.base_url,
        "api_mode": "chat",
    }


@dataclass(frozen=True)
class DaemonInfo:
    """Identity of one running agent server instance."""

    port: int
    token: str
    pid: int

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def __repr__(self) -> str:
        return (
            f"DaemonInfo(port={self.port!r}, token='<redacted>', "
            f"pid={self.pid!r}, base_url={self.base_url!r})"
        )


def read_info() -> DaemonInfo | None:
    """Read daemon state from disk, tolerating an absent or corrupt file."""
    try:
        with config.daemon_file().open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except Exception:
        return None

    if not isinstance(data, dict):
        return None

    port = data.get("port")
    token = data.get("token")
    pid = data.get("pid")
    if (
        not isinstance(port, int)
        or isinstance(port, bool)
        or not isinstance(token, str)
        or not isinstance(pid, int)
        or isinstance(pid, bool)
    ):
        return None

    return DaemonInfo(port=port, token=token, pid=pid)


def is_alive(info: DaemonInfo, timeout: float = 2.0) -> bool:
    """Return True only when the daemon health endpoint answers 200."""
    try:
        response = httpx.get(
            f"{info.base_url}/health",
            headers={"X-Session-API-Key": info.token},
            timeout=timeout,
        )
        status = response.status_code
        response.close()
        return status == 200
    except Exception:
        return False


def _remove_daemon_file() -> None:
    try:
        config.daemon_file().unlink(missing_ok=True)
    except OSError:
        pass


def _atomic_json_write(path, data: dict) -> None:
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
            fh.flush()
            os.fsync(fh.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _history_file():
    return config.state_dir() / "daemon_history.json"


def _read_history() -> dict:
    try:
        data = json.loads(_history_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        data = {}
    if not isinstance(data, dict):
        data = {}
    starts = data.get("starts")
    return {
        "starts": starts[-50:] if isinstance(starts, list) else [],
        "unexpected_exits": data.get("unexpected_exits", 0)
        if isinstance(data.get("unexpected_exits", 0), int)
        else 0,
        "last_unexpected_exit_at": data.get("last_unexpected_exit_at"),
    }


def _now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _record_start() -> None:
    history = _read_history()
    history["starts"].append(_now_iso())
    history["starts"] = history["starts"][-50:]
    _atomic_json_write(_history_file(), history)


def _record_unexpected_exit() -> None:
    history = _read_history()
    history["unexpected_exits"] += 1
    history["last_unexpected_exit_at"] = _now_iso()
    _atomic_json_write(_history_file(), history)


def history() -> dict:
    """Return the daemon start and unexpected-exit history."""
    return _read_history()


def _write_daemon_file(info: DaemonInfo) -> None:
    path = config.daemon_file()
    fd, tmp = tempfile.mkstemp(
        dir=path.parent,
        prefix=".daemon-",
        suffix=".json",
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(
                {"port": info.port, "token": info.token, "pid": info.pid},
                fh,
            )
            fh.flush()
            os.fsync(fh.fileno())
        if os.name != "nt":
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _terminate(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    else:
        os.kill(pid, signal.SIGTERM)


def _log_tail() -> str:
    try:
        lines = (
            config.log_file().read_text(encoding="utf-8", errors="replace").splitlines()
        )
    except OSError:
        return ""
    return "\n".join(lines[-30:])


def _windows_process_alive(pid: int) -> bool:
    """Liveness without side effects. On Windows `os.kill(pid, 0)` is not a
    probe: it calls TerminateProcess and kills the process it asks about."""
    import ctypes
    from ctypes import wintypes

    kernel32 = getattr(ctypes, "WinDLL")("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.GetExitCodeProcess.argtypes = (
        wintypes.HANDLE,
        ctypes.POINTER(wintypes.DWORD),
    )
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    process_query_limited_information = 0x1000
    error_access_denied = 5
    still_active = 259

    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        # Access denied means it exists but belongs to another user.
        return getattr(ctypes, "get_last_error")() == error_access_denied
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)


def _process_alive(pid: int) -> bool:
    if os.name == "nt":
        return _windows_process_alive(pid)
    if sys.platform.startswith("linux"):
        try:
            stat = Path(f"/proc/{pid}/stat").read_text()
            state = stat[stat.rfind(")") + 2 :].split()[0]
            if state == "Z":
                return False
        except (OSError, IndexError):
            return False
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _proc_daemons() -> list[DaemonInfo]:
    if not sys.platform.startswith("linux"):
        return []
    found: list[DaemonInfo] = []
    proc_root = Path("/proc")
    state = str(config.state_dir().resolve())
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return []
    for entry in entries:
        if not entry.name.isdecimal() or not _process_alive(int(entry.name)):
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().split(b"\0")
            environ = (entry / "environ").read_bytes().split(b"\0")
            env = dict(
                item.decode(errors="replace").split("=", 1)
                for item in environ
                if b"=" in item
            )
            if (
                not any(b"agentrt.runtime.server_launch" in arg for arg in cmdline)
                or str(Path(env.get("AGENTRT_PERSISTENCE_DIR", "")).resolve()) != state
            ):
                continue
            args = [arg.decode(errors="replace") for arg in cmdline]
            port_index = args.index("--port") + 1
            found.append(
                DaemonInfo(
                    port=int(args[port_index]),
                    token=env["AGENTRT_SESSION_API_KEYS_0"],
                    pid=int(entry.name),
                )
            )
        except (OSError, ValueError, KeyError, IndexError):
            continue
    return found


def _daemon_env(token: str) -> dict[str, str]:
    """Environment for the spawned agent server, including the H0 policy.

    Factored out so the deployment contract can be asserted without launching a
    process.
    """
    env = os.environ.copy()
    env["AGENTRT_SESSION_API_KEYS_0"] = token
    # The vendored SDK defaults its state directory to ~/.agentrt on every
    # platform, while config.state_dir() follows the Windows convention.
    # Without this the daemon would look for its settings, and therefore
    # its LLM credential, somewhere the bootstrap never wrote.
    env["AGENTRT_PERSISTENCE_DIR"] = str(config.state_dir())
    env["AGENTRT_SUPPRESS_BANNER"] = "1"
    # Conversations default to a path relative to the process working
    # directory, so without this the daemon's session catalog would move
    # whenever it was started from somewhere else -- sessions would appear
    # to vanish rather than fail loudly.
    state = config.state_dir()
    env["AGENTRT_CONVERSATIONS_PATH"] = str(state / "conversations")
    env["AGENTRT_WORKSPACE_PATH"] = str(state / "workspace")
    # Snapshot/isolated worktrees otherwise go to a hard-coded
    # /tmp/conversation-worktrees: `\\tmp` on the current drive on Windows,
    # and a directory tmp cleaners may empty under a live session on Linux.
    env["AGENTRT_CONVERSATION_WORKTREE_ROOT"] = str(state / "worktrees")
    # Without this, the vendored server has no cipher and silently redacts a
    # conversation's persisted secrets -- including an LLM's API key -- to
    # `**********` whenever it saves state to disk. A fresh dispatch never
    # notices (its first real work happens before any save/reload), but a
    # fork always round-trips through exactly that save/reload before it can
    # run, so every fork loses its credential the same way. `setdefault`
    # rather than a hard assignment: an operator who already set this
    # themselves (e.g. to share one key across machines on purpose) is not
    # silently overridden. docs/plans/deepseek-hardening.md H10 item 2,
    # 2026-09-18.
    if "AGENTRT_SECRET_KEY" not in env:
        env["AGENTRT_SECRET_KEY"] = config.secret_key()
    # Explicit deployment-only LLM policy, derived from the same resolved
    # router config bootstrap.py uses to build the saved profile. The server
    # enforces it on new conversations; an agent-server started any other way
    # stays generic. Omitted (not merely empty) when the router config is not
    # yet resolvable, so a bare `agentrt daemon start` before `.env` exists
    # still starts -- the server's own policy field is optional for exactly
    # this reason.
    policy = _deployment_llm_policy()
    if policy is not None:
        env["AGENTRT_DEPLOYMENT_LLM_POLICY"] = json.dumps(policy)
    # OpenHands ceremony this deployment never reaches, off by default here
    # but `setdefault` rather than a hard assignment: an operator who already
    # set either var in their own environment (e.g. to restore vendored
    # defaults, or because a downstream feature starts depending on the
    # builtin sub-agents) is not silently overridden. No AgentRT permission
    # preset grants the `delegate` tool the builtin sub-agents need, and
    # nothing about MCP/CLI-driven dispatch connects an editor to VSCode.
    # docs/plans/deepseek-hardening.md H9 (OpenHands ceremony), 2026-09-17.
    env.setdefault("AGENTRT_REGISTER_BUILTIN_SUBAGENTS", "0")
    env.setdefault("AGENTRT_ENABLE_VSCODE", "0")
    # The public OpenHands skill catalogue (68 skills, ~24k chars re-sent on
    # every LLM call) is irrelevant to AgentRT workers: none, by default. The
    # target repo's own skills are loaded separately and are unaffected.
    env.setdefault("AGENTRT_PUBLIC_SKILLS", "")
    # The vendored server's own run-pool cap defaults to 10 concurrent
    # conversation steps; without this a fan-out beyond that queues on the
    # daemon side even after AGENTRT_MAX_SESSIONS (this client's own,
    # separate cap) is raised. setdefault: an operator who already set their
    # own ceiling here is not silently overridden.
    env.setdefault("AGENTRT_MAX_CONCURRENT_RUNS", config.DEFAULT_MAX_CONCURRENT_RUNS)
    for name, value in {
        "GIT_EDITOR": "true",
        "EDITOR": "true",
        "VISUAL": "true",
        "GIT_PAGER": "cat",
        "PAGER": "cat",
        "GIT_TERMINAL_PROMPT": "0",
    }.items():
        env.setdefault(name, value)
    return env


def ensure_running(startup_timeout: float = 240.0) -> DaemonInfo:
    """Start the daemon if needed, or return the already-running instance."""
    lock_path = config.state_dir() / "daemon.lock"
    lock_fd: int | None = None
    # The startup default is 240s rather than 60s. Measured on a fresh
    # `uv tool install`: the first daemon start took longer than a minute and
    # was killed, so the very first thing a new user does failed, with a message
    # that read like a real failure. The same start warm takes 37 seconds. A
    # generous ceiling costs nothing now that a child which has exited is
    # detected immediately instead of being waited out.
    acquire_deadline = time.monotonic() + 30.0

    try:
        while lock_fd is None:
            try:
                lock_fd = os.open(
                    lock_path,
                    os.O_CREAT | os.O_EXCL | os.O_RDWR,
                )
            except FileExistsError:
                if time.monotonic() >= acquire_deadline:
                    raise RuntimeError(
                        "Another agentrt CLI/MCP process is starting or holding "
                        "the daemon lock. "
                        "Concurrent CLI calls should be serialized or retried. "
                        f"Lock path: {lock_path}"
                    ) from None
                try:
                    if time.time() - lock_path.stat().st_mtime > 120.0:
                        lock_path.unlink()
                        continue
                except OSError:
                    pass
                time.sleep(0.1)

        info = read_info()
        if info is not None:
            if is_alive(info):
                return info
            if not _process_alive(info.pid):
                _record_unexpected_exit()
            elif not sys.platform.startswith("linux"):
                # Linux re-discovers it below through /proc; elsewhere a second
                # daemon would start on the same state directory.
                raise RuntimeError(
                    f"AgentRT daemon pid {info.pid} is running but not answering "
                    "its health check; wait and retry, or run `agentrt daemon stop`"
                )
        candidates = _proc_daemons()
        if len(candidates) > 1:
            details = ", ".join(
                f"pid={item.pid} port={item.port}" for item in candidates
            )
            raise RuntimeError(
                "Multiple AgentRT daemons serve this state directory: " + details
            )
        if candidates:
            adopted = candidates[0]
            _write_daemon_file(adopted)
            _record_start()
            return adopted

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        token = secrets.token_urlsafe(32)
        env = _daemon_env(token)
        command = [
            sys.executable,
            "-m",
            # Our launcher, not the server's own entry point: it registers the
            # workspace kinds this runtime supports before handing over. See
            # server_launch for why a missing import shows up as a validation
            # error with an empty message.
            "agentrt.runtime.server_launch",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ]

        popen_kwargs: dict = {}
        if os.name == "nt":
            popen_kwargs["creationflags"] = (
                subprocess.DETACHED_PROCESS
                | subprocess.CREATE_NO_WINDOW
                | subprocess.CREATE_NEW_PROCESS_GROUP
            )
        else:
            popen_kwargs["start_new_session"] = True

        with config.log_file().open("ab") as log_handle:
            proc = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                env=env,
                **popen_kwargs,
            )

        info = DaemonInfo(port=port, token=token, pid=proc.pid)

        try:
            _write_daemon_file(info)
        except BaseException:
            try:
                _terminate(proc.pid)
            except OSError:
                pass
            raise

        alive_deadline = time.monotonic() + startup_timeout
        while time.monotonic() < alive_deadline:
            if is_alive(info, timeout=0.5):
                _record_start()
                return info
            # A child that has exited is never going to answer, so stop waiting
            # on it. Separating "died" from "slow" is what lets the timeout be
            # generous without making a genuine failure take minutes to report.
            if proc.poll() is not None:
                _remove_daemon_file()
                raise RuntimeError(
                    "agent server daemon exited during startup; log tail:\n"
                    + _log_tail()
                )
            time.sleep(0.3)

        try:
            _terminate(proc.pid)
        except OSError:
            pass
        _remove_daemon_file()
        raise RuntimeError(
            f"agent server daemon did not answer within {startup_timeout:.0f}s "
            "and was stopped. It was still running, so it may simply have "
            "needed longer: a first start after installation imports the whole "
            "model stack cold. Try again, or pass a larger startup_timeout.\n"
            "log tail:\n" + _log_tail()
        )
    finally:
        if lock_fd is not None:
            os.close(lock_fd)
            try:
                lock_path.unlink()
            except OSError:
                pass


def stop(timeout: float = 10.0) -> bool:
    """Stop the daemon, escalating only after allowing graceful shutdown."""
    info = read_info()
    if info is None or not _process_alive(info.pid):
        _remove_daemon_file()
        return False

    try:
        _terminate(info.pid)
    except ProcessLookupError:
        pass
    except OSError as exc:
        raise RuntimeError(f"Could not stop daemon pid {info.pid}: {exc}") from exc

    deadline = time.monotonic() + max(timeout, 60.0)
    while _process_alive(info.pid) and time.monotonic() < deadline:
        time.sleep(0.1)

    if _process_alive(info.pid):
        try:
            os.kill(info.pid, signal.SIGKILL) if os.name != "nt" else subprocess.run(
                ["taskkill", "/PID", str(info.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        except OSError:
            pass
        kill_deadline = time.monotonic() + 5.0
        while _process_alive(info.pid) and time.monotonic() < kill_deadline:
            time.sleep(0.1)

    if _process_alive(info.pid):
        raise RuntimeError(
            f"Could not stop daemon pid {info.pid}; daemon.json was retained."
        )
    _remove_daemon_file()
    return True


def status() -> dict:
    """Return a JSON-serialisable status dict, never exposing the token."""
    info = read_info()
    if info is None or not is_alive(info):
        return {
            "running": False,
            "port": None,
            "pid": None,
            "base_url": None,
            "log": str(config.log_file()),
            "state_dir": str(config.state_dir()),
        }

    return {
        "running": True,
        "port": info.port,
        "pid": info.pid,
        "base_url": info.base_url,
        "log": str(config.log_file()),
        "state_dir": str(config.state_dir()),
    }
