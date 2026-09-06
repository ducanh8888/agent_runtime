"""HTTP client for the agentrt daemon.

Every front end (CLI and MCP server) should use this class as its only
window into the daemon REST API.  Keeping the wire format here prevents
callers from silently depending on different response shapes.
"""

from __future__ import annotations

import os
import uuid
from urllib.parse import quote

import httpx

from agentrt.runtime import bootstrap, config, daemon


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
            if isinstance(profiles, (list, tuple)):
                if not profiles:
                    raise ClientError("bootstrap returned no agent profiles")
                profile = profiles[0]
            else:
                profile = profiles
            self._profile_ref = getattr(profile, "id", profile)

        if self._profile_ref is None:
            raise ClientError("bootstrap did not produce an agent profile id")

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

        url = f"{self._base_url()}{path}"
        headers = {"X-Session-API-Key": self._token()}
        headers.update(kwargs.pop("headers", None) or {})  # type: ignore[arg-type]

        try:
            response = self._http.request(method, url, headers=headers, **kwargs)
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

    def _profile_id(self) -> str:
        if self._profile_ref is None:
            self._ensure_ready()
        return str(self._profile_ref)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def dispatch(
        self,
        task: str,
        workspace: str,
        *,
        title: str | None = None,
    ) -> dict:
        """Start a new conversation in a workspace for a text task."""
        workspace = os.path.abspath(os.path.expanduser(workspace))
        os.makedirs(workspace, exist_ok=True)

        body: dict = {
            "workspace": {"working_dir": workspace},
            "agent_profile_id": self._profile_id(),
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
