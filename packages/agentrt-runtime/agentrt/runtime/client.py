"""HTTP client for the agentrt daemon.

Every front end (CLI and MCP server) should use this class as its only
window into the daemon REST API.  Keeping the wire format here prevents
callers from silently depending on different response shapes.
"""

from __future__ import annotations

import os
import uuid
from datetime import datetime, timezone
from urllib.parse import quote

import httpx

from agentrt.runtime import bootstrap, config, daemon, permissions


#: Directories skipped when reporting what a session wrote.
#:
#: These are dependency trees and tool caches: never authored, frequently
#: enormous. Pruning them is a correctness measure before it is a speed one. A
#: session that runs ``uv sync`` or ``npm install`` gives thirty thousand files
#: a modification time inside its own run, and without this the one file it
#: wrote is buried in them. It is also 35x faster -- 1.43s to 0.04s over this
#: repository -- which matters because this runs inside a tool call.
#:
#: Build outputs (``dist``, ``build``, ``target``) are deliberately absent: a
#: session can legitimately be asked to produce one, and the modification-time
#: filter already excludes a stale one.
PRUNED_DIRS = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        "__pycache__",
        ".venv",
        "venv",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
    }
)


def _started_at(status: dict) -> float | None:
    """When the session began, as a POSIX timestamp, or None if unknown.

    The daemon reports ``created_at`` as ISO 8601 with an explicit ``Z``, and
    this machine runs at UTC+7. Comparing that text to a local clock -- or
    parsing it as naive -- puts every file on the wrong side of the boundary by
    seven hours, so the parse is to an aware datetime and the comparison is
    epoch to epoch.

    No grace period, which was tried and removed. FAT records modification times
    to a two-second resolution, so on such a volume a file written just after
    dispatch can report a stamp just before it and be missed. Two seconds of
    slack covers that and costs more than it saves: seeding a workspace and then
    dispatching into it happens microseconds apart -- `tools/adversarial.py`
    does exactly that -- so the slack attributed the orchestrator's own seed
    file to the session on every such run. Being wrong about the common case to
    insure the rare one is the wrong trade, and the rare one is documented
    instead.
    """
    raw = status.get("created_at")
    if not isinstance(raw, str) or not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


class ClientError(Exception):
    """Base class for all errors raised by :class:`Client`."""


class SessionNotFound(ClientError):
    """A session prefix did not match any known session."""


class AmbiguousSession(ClientError):
    """A session prefix matched more than one known session."""


def short_id(full: str) -> str:
    """Return the short, human-friendly form of a session id."""
    return str(full)[:8]

def _status_of(data: object) -> str | None:
    """Read a session's lifecycle state.

    The server calls the field ``execution_status``; ``status`` is accepted as a
    fallback so a future rename does not silently start returning None, which is
    indistinguishable from "still starting" at the call site.
    """
    if not isinstance(data, dict):
        return None
    return data.get("execution_status") or data.get("status")

def _join_text(blocks: object) -> str:
    """Join the ``"text"`` fields of content blocks, tolerating non-lists."""
    if not isinstance(blocks, list):
        return ""
    return "".join(
        b.get("text", "")
        for b in blocks
        if isinstance(b, dict) and isinstance(b.get("text"), str)
    )


def _text_from_response(data: object) -> str | None:
    """Extract the final agent text from a JSON response.

    The daemon's exact response field has changed in the past; accept the
    common variants so the CLI and MCP server do not need to know which one
    a particular daemon version returns.
    """
    if isinstance(data, str):
        return data
    if not isinstance(data, dict):
        return None

    for key in ("content", "text", "final_response", "result", "response"):
        value = data.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            parts: list[str] = []
            for item in value:
                if isinstance(item, dict):
                    parts.append(str(item.get("text") or item.get("content") or ""))
                else:
                    parts.append(str(item))
            return "\n".join(parts)
        return str(value)
    return None


def _join_blocks(blocks: object) -> str:
    """Join the text of content blocks shaped ``{"type": "text", "text": ...}``."""
    if isinstance(blocks, str):
        return blocks
    if not isinstance(blocks, list):
        return ""
    parts: list[str] = []
    for block in blocks:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict):
            text = block.get("text")
            if text is not None:
                parts.append(str(text))
    return "\n".join(parts)


def _capped(text: str, limit: int) -> str:
    """Truncate to ``limit`` characters, marking the cut."""
    if len(text) <= limit:
        return text
    return text[:limit] + " ... [truncated]"


class Client:
    """Talking to the daemon over its HTTP API.

    Construction is intentionally side-effect free.  The daemon process and
    bootstrapped profiles are started lazily on the first operation that
    needs them, so callers can create a client without paying startup cost.
    """

    def __init__(self, *, timeout: float = 120.0) -> None:
        self._timeout = timeout
        self._daemon_info = None
        self._profile_ref: object | None = None
        self._http: httpx.Client | None = None

    @property
    def info(self):
        """Return the :class:`DaemonInfo`, starting the daemon if needed.

        Only daemon startup is required for this property; profile bootstrap
        is deferred until a request actually needs a profile id.
        """
        if self._daemon_info is None:
            self._daemon_info = daemon.ensure_running()
        return self._daemon_info

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _ensure_ready(self) -> None:
        """Make sure the daemon is running and profiles are bootstrapped."""
        _ = self.info

        if self._profile_ref is None:
            profiles = bootstrap.ensure_profiles()
            if not isinstance(profiles, dict) or not profiles:
                raise ClientError("bootstrap did not produce agent profiles")
            self._profile_ref = profiles

        if self._http is None:
            self._http = httpx.Client(timeout=self._timeout, follow_redirects=True)

    def _base_url(self) -> str:
        info = self._daemon_info
        for attr in ("base_url", "url", "api_url"):
            value = getattr(info, attr, None)
            if value:
                return str(value).rstrip("/")

        port = getattr(info, "port", None)
        if port is not None:
            host = getattr(info, "host", "127.0.0.1")
            return f"http://{host}:{port}"

        raise ClientError("daemon info did not expose a base URL")

    def _token(self) -> str:
        """Return the daemon token without ever logging or returning it."""
        info = self._daemon_info
        for attr in ("token", "api_key", "session_api_key", "api_token"):
            if info is not None and hasattr(info, attr):
                value = getattr(info, attr)
                if callable(value):
                    value = value()
                if value is not None:
                    return str(value)

        # Some deployments keep the shared secret in config rather than on
        # DaemonInfo.  Probe common names defensively.
        for attr in ("session_api_key", "daemon_token", "api_key", "token"):
            if hasattr(config, attr):
                value = getattr(config, attr)
                if callable(value):
                    value = value()
                if value:
                    return str(value)

            getter = getattr(config, "get", None)
            if callable(getter):
                try:
                    value = getter(attr)
                except Exception:
                    value = None
                if value:
                    return str(value)

        raise ClientError("daemon token is unavailable")

    def _send(
        self,
        method: str,
        path: str,
        *,
        tolerate_404: bool = False,
        **kwargs: object,
    ) -> httpx.Response:
        """Perform an authenticated HTTP call and unify error reporting."""
        self._ensure_ready()
        assert self._http is not None

        extra = kwargs.pop("headers", None) or {}

        def attempt() -> httpx.Response:
            assert self._http is not None
            headers = {"X-Session-API-Key": self._token()}
            headers.update(extra)  # type: ignore[arg-type]
            return self._http.request(
                method, f"{self._base_url()}{path}", headers=headers, **kwargs
            )

        try:
            response = attempt()
        except httpx.TransportError as exc:
            # The cached port and token come from daemon.json as it read at
            # first use. A daemon that restarts picks a new ephemeral port, so
            # a long-lived client -- the MCP server lives as long as the
            # orchestrator does -- would go on addressing a port nobody is
            # listening on, and every tool would fail until the orchestrator
            # itself was restarted. Re-read and try once more.
            self._daemon_info = None
            self._profile_ref = None
            if self._http is not None:
                self._http.close()
                self._http = None
            try:
                self._ensure_ready()
                response = attempt()
            except httpx.HTTPError as retry_exc:
                raise ClientError(str(retry_exc)) from retry_exc
        except httpx.HTTPError as exc:
            raise ClientError(str(exc)) from exc

        if response.status_code >= 400:
            if not (tolerate_404 and response.status_code == 404):
                raise ClientError(response.text)

        return response

    def _resolve_session(self, session: str) -> str:
        """Turn a full UUID or an unambiguous prefix into a full UUID."""
        try:
            uuid.UUID(session)
        except (ValueError, AttributeError, TypeError):
            pass
        else:
            return session

        prefix = str(session).casefold()
        matches: list[str] = []
        for item in self.list_sessions():
            full = item.get("id")
            if full is None:
                continue
            if str(full).casefold().startswith(prefix):
                matches.append(str(full))

        if not matches:
            raise SessionNotFound(f"no session matches {session!r}")

        if len(matches) > 1:
            listed = ", ".join(short_id(full) for full in matches)
            raise AmbiguousSession(
                f"session prefix {session!r} is ambiguous: {listed}"
            )

        return matches[0]

    def _profile_id(self, permission: str | None = None) -> str:
        """Resolve a preset name to the agent profile the daemon should use."""
        if self._profile_ref is None:
            self._ensure_ready()
        preset = permissions.normalise(permission)
        profiles = self._profile_ref
        assert isinstance(profiles, dict)
        if preset not in profiles:
            raise ClientError(
                f"no agent profile for permission {preset!r}; "
                "run `agentrt config` to see what exists"
            )
        return str(profiles[preset])

    def profiles(self) -> dict:
        """List the permission presets and what each one grants."""
        self._ensure_ready()
        return {
            "default": permissions.DEFAULT_PERMISSION,
            "presets": [
                {
                    "name": preset,
                    "description": permissions.DESCRIPTIONS[preset],
                    "tools": permissions.tools_for(preset),
                }
                for preset in permissions.PRESETS
            ],
        }

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def dispatch(
        self,
        task: str,
        workspace: str,
        *,
        title: str | None = None,
        permission: str | None = None,
    ) -> dict:
        """Start a new conversation in a workspace for a text task."""
        workspace = os.path.abspath(os.path.expanduser(workspace))
        os.makedirs(workspace, exist_ok=True)
        preset = permissions.normalise(permission)

        cap = config.max_running_sessions()
        if cap > 0:
            running = [s for s in self.list_sessions(limit=100)
                       if (s.get("status") or "").lower() == "running"]
            if len(running) >= cap:
                raise ClientError(
                    f"{len(running)} sessions are already running and the limit "
                    f"is {cap}. Wait for one to finish, stop one, or raise "
                    "AGENTRT_MAX_SESSIONS on the daemon's environment."
                )

        body: dict = {
            "workspace": {"working_dir": workspace},
            "agent_profile_id": self._profile_id(preset),
            # The daemon imports these modules "to trigger tool auto
            # registration". Naming the guard module is what installs the
            # permission-aware file editor inside the daemon process, which is
            # a different process from this one.
            "tool_module_qualnames": {"file_editor": bootstrap.GUARD_MODULE},
            "initial_message": {
                "role": "user",
                "content": [{"type": "text", "text": task}],
            },
        }
        if title is not None:
            body["title"] = title

        data = self._send("POST", "/api/conversations", json=body).json()
        full_id = data.get("id")
        return {
            "id": full_id,
            "short_id": short_id(full_id) if full_id else None,
            "status": _status_of(data),
            "workspace": workspace,
            "permission": preset,
        }

    def list_sessions(self, limit: int = 50) -> list[dict]:
        """Return the most recent sessions from the daemon."""
        response = self._send(
            "GET",
            "/api/conversations/search",
            params={"limit": limit},
        )
        data = response.json()

        if isinstance(data, list):
            sessions = data
        elif isinstance(data, dict):
            sessions = data.get("sessions")
            if not isinstance(sessions, list):
                sessions = data.get("items")
            if not isinstance(sessions, list):
                sessions = data.get("conversations")
            if not isinstance(sessions, list):
                sessions = []
        else:
            sessions = []

        result: list[dict] = []
        for session in sessions:
            full_id = session.get("id") if isinstance(session, dict) else None
            result.append(
                {
                    "id": full_id,
                    "short_id": short_id(full_id) if full_id else None,
                    "title": session.get("title") if isinstance(session, dict) else None,
                    "status": _status_of(session),
                    "created_at": session.get("created_at") if isinstance(session, dict) else None,
                    "updated_at": session.get("updated_at") if isinstance(session, dict) else None,
                }
            )
        return result

    def status(self, session: str) -> dict:
        """Return the status of one session by full id or unambiguous prefix."""
        resolved = self._resolve_session(session)
        data = self._send(
            "GET",
            f"/api/conversations/{quote(resolved, safe='')}",
        ).json()

        workspace_obj = data.get("workspace")
        if isinstance(workspace_obj, dict):
            workspace = workspace_obj.get("working_dir")
        else:
            workspace = None

        result = {
            "id": data.get("id", resolved),
            "short_id": short_id(data.get("id", resolved)),
            "title": data.get("title"),
            "status": _status_of(data),
            "created_at": data.get("created_at"),
            "updated_at": data.get("updated_at"),
            "workspace": workspace,
        }
        if data.get("error") is not None:
            result["error"] = data.get("error")
        return result

    def result(self, session: str) -> dict:
        """Return the agent's final response, or ``None`` if there is none yet."""
        resolved = self._resolve_session(session)
        response = self._send(
            "GET",
            f"/api/conversations/{quote(resolved, safe='')}/agent_final_response",
            tolerate_404=True,
        )

        if response.status_code == 404:
            info = self.status(session)
            return {
                "id": info.get("id"),
                "short_id": info.get("short_id"),
                "status": _status_of(info),
                "result": None,
            }

        data = response.json()
        # The final-response payload describes the answer, not the session, so
        # it carries no id. Use the id we resolved to make the request.
        full_id = resolved
        status = _status_of(data)
        if status is None:
            status = self.status(session).get("status")

        return {
            "id": full_id,
            "short_id": short_id(full_id) if full_id else None,
            "status": status,
            "result": _text_from_response(data),
        }

    def transcript(
        self, session: str, *, limit: int = 30, cursor: str | None = None
    ) -> dict:
        """Return conversation events oldest first; ``next_cursor`` pages
        backwards into older events."""
        resolved = self._resolve_session(session)

        params: dict[str, object] = {
            "sort_order": "TIMESTAMP_DESC",
            "limit": min(limit, 100),
        }
        if cursor:
            params["page_id"] = cursor

        data = self._send(
            "GET",
            f"/api/conversations/{quote(resolved, safe='')}/events/search",
            params=params,
        ).json()

        events: list[dict] = []
        items = data.get("items") if isinstance(data, dict) else None
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            kind = item.get("kind")
            if kind == "MessageEvent":
                message = item.get("llm_message") or {}
                events.append(
                    {
                        "type": "message",
                        "role": message.get("role"),
                        "text": _join_text(message.get("content")),
                    }
                )
            elif kind == "ActionEvent":
                events.append(
                    {
                        "type": "action",
                        "tool": item.get("tool_name"),
                        "thought": _capped(_join_text(item.get("thought")), 400),
                    }
                )
            elif kind == "ObservationEvent":
                observation = item.get("observation") or {}
                events.append(
                    {
                        "type": "observation",
                        "tool": item.get("tool_name"),
                        "output": _capped(
                            _join_text(observation.get("content")), 600
                        ),
                    }
                )

        events.reverse()
        return {
            "id": resolved,
            "short_id": short_id(resolved),
            "status": self.status(resolved).get("status"),
            "events": events,
            "next_cursor": data.get("next_page_id") if isinstance(data, dict) else None,
        }

    def send(self, session: str, message: str) -> dict:
        """Post a user message and start a run (``run=True`` is required)."""
        resolved = self._resolve_session(session)
        self._send(
            "POST",
            f"/api/conversations/{quote(resolved, safe='')}/events",
            json={
                "role": "user",
                "content": [{"type": "text", "text": message}],
                "run": True,
            },
        )
        return {
            "id": resolved,
            "short_id": short_id(resolved),
            "status": self.status(resolved).get("status"),
        }

    def interrupt(self, session: str) -> dict:
        """Cancel work in flight; the session becomes "paused", still resumable."""
        resolved = self._resolve_session(session)
        self._send(
            "POST", f"/api/conversations/{quote(resolved, safe='')}/interrupt"
        )
        return {
            "id": resolved,
            "short_id": short_id(resolved),
            "status": self.status(resolved).get("status"),
        }

    def stop(self, session: str) -> dict:
        """Pause a session. Maps to /pause, not /goal/stop -- /goal/stop
        belongs to a separate objective subsystem and does nothing to an
        ordinary conversation."""
        resolved = self._resolve_session(session)
        self._send("POST", f"/api/conversations/{quote(resolved, safe='')}/pause")
        return {
            "id": resolved,
            "short_id": short_id(resolved),
            "status": self.status(resolved).get("status"),
        }

    def resume(self, session: str) -> dict:
        """Resume a paused session. Maps to /run, not /goal/resume, which
        returns HTTP 400 "no_resumable_goal" on an ordinary conversation."""
        resolved = self._resolve_session(session)
        self._send("POST", f"/api/conversations/{quote(resolved, safe='')}/run")
        return {
            "id": resolved,
            "short_id": short_id(resolved),
            "status": self.status(resolved).get("status"),
        }

    def delete(self, session: str) -> dict:
        """Delete a session."""
        resolved = self._resolve_session(session)
        self._send("DELETE", f"/api/conversations/{quote(resolved, safe='')}")
        return {"id": resolved, "short_id": short_id(resolved), "deleted": True}

    def artifacts(self, session: str, path: str | None = None) -> dict:
        """What this session wrote, or the contents of one file it wrote.

        The daemon has no directory-listing endpoint, so listing is done
        locally, which is correct because the daemon runs on this machine.

        The question an orchestrator asks here is "what did this session
        produce", and an earlier version answered a different one -- every file
        in the workspace, sorted by path, first two hundred. That is the same
        answer for an empty scratch directory, which is why it survived: every
        workspace dogfooded during development was one. Measured against a real
        repository it is not merely noisy but useless. With a `.venv` at the
        root, all two hundred slots are dependency files and nothing the session
        wrote appears at all, because `.` sorts before every letter.
        """
        resolved = self._resolve_session(session)
        status = self.status(resolved)
        workspace = status.get("workspace")
        if not workspace:
            raise ClientError(f"no workspace known for session {short_id(resolved)}")

        if path is not None:
            if os.path.isabs(path):
                raise ClientError(f"absolute paths are not allowed: {path!r}")
            root = permissions._real(workspace)
            # `permissions.contains` rather than a `startswith`: this is the same
            # containment question the guard answers, and answering it a second
            # way here meant a junction inside the workspace was approved,
            # since the old check normalised the path without resolving it.
            if not permissions.contains(root, permissions._real(path, base=root)):
                raise ClientError(f"path escapes the workspace: {path!r}")
            response = self._send(
                "GET",
                f"/api/conversations/{quote(resolved, safe='')}/workspace/{quote(path, safe='/')}",
            )
            return {
                "id": resolved,
                "short_id": short_id(resolved),
                "path": path,
                "content": response.text,
            }

        since = _started_at(status)
        found: list[tuple[float, dict]] = []
        total = 0
        for dirpath, dirnames, filenames in os.walk(workspace):
            dirnames[:] = sorted(d for d in dirnames if d not in PRUNED_DIRS)
            for name in filenames:
                full = os.path.join(dirpath, name)
                try:
                    entry_stat = os.stat(full)
                except OSError:
                    continue
                total += 1
                if since is not None and entry_stat.st_mtime < since:
                    continue
                found.append(
                    (
                        entry_stat.st_mtime,
                        {
                            "path": os.path.relpath(full, workspace).replace(
                                os.sep, "/"
                            ),
                            "size": entry_stat.st_size,
                            "modified": datetime.fromtimestamp(
                                entry_stat.st_mtime, tz=timezone.utc
                            ).isoformat(),
                        },
                    )
                )
        # Newest first, so the file the session finished with is the one read
        # first. Path order buries it behind whatever the repository is called.
        found.sort(key=lambda entry: entry[0], reverse=True)
        files = [entry[1] for entry in found]
        return {
            "id": resolved,
            "short_id": short_id(resolved),
            "workspace": workspace,
            "since": (
                datetime.fromtimestamp(since, tz=timezone.utc).isoformat()
                if since is not None
                else None
            ),
            "files": files[:200],
            "truncated": len(files) > 200,
            # Files seen outside the pruned directories, which is not the
            # size of the workspace and should not be read as one: a repository
            # with a 400-file virtualenv reports 4. Its job is to distinguish
            # "walked the workspace and the session wrote nothing" from "the
            # walk found nothing at all". The first is a real and common answer
            # -- a session can run an hour and produce no file -- and it should
            # not look like a broken call.
            "total_scanned": total,
            "pruned": sorted(PRUNED_DIRS),
        }

