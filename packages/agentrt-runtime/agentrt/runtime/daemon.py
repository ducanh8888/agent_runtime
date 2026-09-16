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

import httpx

from agentrt.runtime import config


#: Explicit deployment-only LLM policy for any server this runtime starts.
#: Mirrors the direct/high contract bootstrap writes into the saved profile, so
#: every new conversation is direct DeepSeek Chat Completions, thinking enabled
#: and effort high, and a weaker explicit agent/agent_settings payload or a
#: switch_llm bypass is refused by the server. Kept literal rather than derived
#: from the operator's router config: a deployment that points elsewhere should
#: fail visibly here instead of silently running a weaker model.
DEPLOYMENT_LLM_POLICY: dict[str, str] = {
    "model": "deepseek-flash",
    "base_url": "https://api.deepseek.com",
    "api_mode": "chat",
    "thinking_mode": "enabled",
    "reasoning_effort": "high",
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
            ["taskkill", "/PID", str(pid), "/T", "/F"],
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
    # Explicit deployment-only LLM policy. The server enforces it on new
    # conversations; an agent-server started any other way stays generic.
    env["AGENTRT_DEPLOYMENT_LLM_POLICY"] = json.dumps(DEPLOYMENT_LLM_POLICY)
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
                        "Timed out waiting for daemon lock at " + str(lock_path)
                    ) from None
                try:
                    if time.time() - lock_path.stat().st_mtime > 120.0:
                        lock_path.unlink()
                        continue
                except OSError:
                    pass
                time.sleep(0.1)

        info = read_info()
        if info is not None and is_alive(info):
            return info

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
    """Stop the daemon process and clear its state file."""
    info = read_info()
    if info is None or not is_alive(info):
        _remove_daemon_file()
        return False

    try:
        _terminate(info.pid)
    except OSError:
        pass

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_alive(info, timeout=0.3):
            break
        time.sleep(0.1)

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
