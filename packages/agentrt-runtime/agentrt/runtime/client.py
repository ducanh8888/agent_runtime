"""HTTP client for the agentrt daemon.

Every front end (CLI and MCP server) should use this class as its only
window into the daemon REST API.  Keeping the wire format here prevents
callers from silently depending on different response shapes.
"""

from __future__ import annotations

import base64
import os
import time
import uuid
from datetime import UTC, datetime
from typing import Any
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


def _clean_tags(tags: dict) -> dict[str, str]:
    """Coerce a tag map to what the daemon accepts.

    Values must be strings: a non-string value is a 500 from the daemon, which
    reaches a caller as an opaque server error rather than as "that is not a
    tag". Measured. Coercing here turns `{"attempt": 2}` into something that
    works instead of something that fails at the wire.
    """
    out: dict[str, str] = {}
    for key, value in (tags or {}).items():
        out[str(key)] = "" if value is None else str(value)
    return out


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
        parsed = parsed.replace(tzinfo=UTC)
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


#: Image formats an attachment may carry, and the size above which one is
#: refused. The format is sniffed from the file's own bytes: an extension is
#: the caller's claim, and the MIME type is what the provider acts on.
MAX_ATTACHMENT_BYTES = 5 * 1024 * 1024


def _sniff_image_mime(head: bytes) -> str | None:
    """The allowed image MIME a file's leading bytes claim, or None."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if head.startswith(b"RIFF") and head[8:12] == b"WEBP":
        return "image/webp"
    return None


#: Execution states in which a session is no longer progressing on its own.
SETTLED_STATUSES = frozenset({"finished", "error", "stuck", "paused"})

#: Optional answer fields copied through from the daemon when present. A null is
#: omitted rather than sent as None, so a caller can tell "not reported" from an
#: explicit empty value.
_OPTIONAL_RESPONSE_KEYS = (
    "request_message_id",
    "iterations_used",
    "iterations_remaining",
    "last_completed_tool",
    "last_progress_at",
    "progress_age_seconds",
    "error",
    "summary",
)


def _response_payload(full_id: str, data: dict) -> dict:
    """Project a daemon answer payload for the CLI/MCP/orchestrator."""
    payload: dict = {
        "id": full_id,
        "short_id": short_id(full_id) if full_id else None,
        "state": data.get("state"),
        "result": _text_from_response(data),
    }
    for key in _OPTIONAL_RESPONSE_KEYS:
        value = data.get(key)
        if value is not None:
            payload[key] = value
    return payload


def _is_settled(status: dict) -> bool:
    """Whether a status sample is terminal for waiting purposes.

    Both the admission state and the result state have to agree. Admission
    catches accepted-but-unstarted input. The result state catches the mirror
    case: a session whose execution status already reads terminal while a newer
    input is still unconsumed -- the server reports that as terminal-but-
    admitted, and its answer is `pending`, so it is not an outcome yet. A
    daemon that predates result_state reports nothing, and an absent value is
    treated as no objection.
    """
    if (status.get("status") or "").lower() not in SETTLED_STATUSES:
        return False
    if status.get("admission_status") in ("queued", "preparing"):
        return False
    return status.get("result_state") != "pending"


def _wait_bucket(payload: dict) -> str:
    """Map a settled session's result payload to its wait bucket."""
    status = (payload.get("status") or "").lower()
    if status == "paused":
        return "stopped"
    if status == "finished":
        return "completed"
    if payload.get("state") == "partial":
        return "partial"
    text = payload.get("result")
    if isinstance(text, str) and text.strip():
        return "partial"
    return "failed"


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


def _add_action_location(entry: dict, action: object) -> None:
    """Copy a bounded path/range off a file action onto a transcript entry.

    Only the location travels -- not the content being written or edited, which
    is what keeps a transcript from carrying repository payloads.
    """
    if not isinstance(action, dict):
        return
    path = action.get("path")
    if isinstance(path, str) and path:
        entry["path"] = _capped(path, 300)
    view_range = action.get("view_range")
    if isinstance(view_range, (list, tuple)):
        bounds = [value for value in view_range if isinstance(value, int)][:2]
        if bounds:
            entry["range"] = bounds


#: Tool names whose observations can carry file-read evidence. Matches the
#: ``tool_name`` persisted on an ``ObservationEvent``. The second spelling is
#: the upstream name; the projection accepts both so a differently-named build
#: does not silently project nothing.
FILE_EDITOR_TOOL_NAMES = frozenset({"file_editor", "str_replace_editor"})

#: A read passes through three stages and they are not interchangeable.
#: ``observed`` is what a persisted successful tool result proves. ``delivered``
#: is content serialized into an LLM request, and ``understood`` is content the
#: model acted on. The latter two are not implied by an observation, so they are
#: ``unknown`` unless a source event records them.
READ_STAGES = ("observed", "delivered", "understood")

#: Optional metadata a paging writer may add to a view observation. The
#: projection must work against events persisted before those fields existed, so
#: every lookup is best-effort: a missing key becomes "unknown", never a guessed
#: range, EOF, or hash.
_READ_PATH_KEYS = ("path", "file", "file_path", "filename")
_READ_VERSION_KEYS = (
    "file_hash",
    "file_version",
    "version",
    "content_hash",
    "hash",
    "sha256",
    "digest",
    "revision",
)
_READ_REQUESTED_LINE_KEYS = (
    "requested_lines",
    "requested_line_range",
    "view_range",
    "requested_range",
)
_READ_RETURNED_LINE_KEYS = (
    "returned_range",
    "returned_lines",
    "returned_line_range",
    "line_range",
    "lines",
)
_READ_START_LINE_KEYS = ("start_line", "first_line", "returned_start_line")
_READ_END_LINE_KEYS = ("end_line", "last_line", "returned_end_line")
_READ_REQUESTED_CHAR_KEYS = (
    "requested_chars",
    "requested_char_range",
    "requested_character_range",
)
_READ_RETURNED_CHAR_KEYS = (
    "returned_chars",
    "returned_char_range",
    "returned_character_range",
)
_READ_OFFSET_KEYS = ("offset", "char_offset", "start_offset", "byte_offset")
_READ_LENGTH_KEYS = ("length", "char_count", "byte_count", "size")
_READ_EOF_KEYS = ("eof", "reached_eof", "is_eof", "at_eof", "end_of_file")
_READ_TRUNCATED_KEYS = (
    "truncated",
    "is_truncated",
    "truncation",
    "partial",
    "is_partial",
)
_READ_DELIVERED_KEYS = ("delivered", "is_delivered", "llm_delivered", "in_llm_request")
_READ_UNDERSTOOD_KEYS = ("understood", "is_understood", "model_understood")
_READ_STATUS_KEYS = ("view_status",)
_READ_PARTIAL_LINE_KEYS = ("partial_line",)

_READ_METADATA_KEYS = (
    _READ_PATH_KEYS
    + _READ_VERSION_KEYS
    + _READ_REQUESTED_LINE_KEYS
    + _READ_RETURNED_LINE_KEYS
    + _READ_START_LINE_KEYS
    + _READ_END_LINE_KEYS
    + _READ_REQUESTED_CHAR_KEYS
    + _READ_RETURNED_CHAR_KEYS
    + _READ_OFFSET_KEYS
    + _READ_LENGTH_KEYS
    + _READ_EOF_KEYS
    + _READ_TRUNCATED_KEYS
    + _READ_DELIVERED_KEYS
    + _READ_UNDERSTOOD_KEYS
    + _READ_STATUS_KEYS
    + _READ_PARTIAL_LINE_KEYS
)

#: Nested dicts under an observation or event that may carry read metadata.
_READ_NESTED_KEYS = ("metadata", "read_evidence", "read", "view")

#: Outcome of one artifacts listing. ``empty`` is a positive answer and is
#: deliberately distinct from every way a listing can be unavailable or
#: incomplete.
ARTIFACTS_LISTED = "listed"
ARTIFACTS_EMPTY = "empty"
ARTIFACTS_TRUNCATED = "truncated"
ARTIFACTS_UNFILTERED = "unfiltered"
ARTIFACTS_PARTIAL = "partial"
ARTIFACTS_UNAVAILABLE = "unavailable"
ARTIFACTS_FAILED = "failed"

_ARTIFACTS_LIMIT = 200


def _read_containers(event: dict, observation: dict) -> list[dict]:
    """Dicts to search for optional read metadata, most specific first."""
    containers: list[dict] = []
    for source in (observation, event):
        containers.append(source)
        for key in _READ_NESTED_KEYS:
            nested = source.get(key)
            if isinstance(nested, dict):
                containers.append(nested)
    return containers


def _read_value(containers: list[dict], keys: tuple[str, ...]) -> object:
    """First non-None value for any candidate key, or None."""
    for container in containers:
        for key in keys:
            if key in container and container[key] is not None:
                return container[key]
    return None


def _present_keys(containers: list[dict], keys: tuple[str, ...]) -> list[str]:
    """Names from ``keys`` that appear with a non-None value anywhere."""
    found: list[str] = []
    for key in dict.fromkeys(keys):
        for container in containers:
            if key in container and container[key] is not None:
                found.append(key)
                break
    return found


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _first_int(container: dict, keys: tuple[str, ...]) -> int | None:
    for key in keys:
        if key in container:
            number = _as_int(container[key])
            if number is not None:
                return number
    return None


def _as_bool(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("true", "yes", "1"):
            return True
        if lowered in ("false", "no", "0"):
            return False
    return None


def _as_range(value: object, *, dimension: str = "generic") -> dict | None:
    """Normalise a range to ``{"start", "end"}`` without inventing an end.

    Accepts the shapes a persisted payload plausibly uses: a two-element list, a
    ``"start-end"`` / ``"start:end"`` string, or a dict. An ``end`` of ``-1`` is
    the file editor's "to end of file" sentinel and is kept as an open end,
    never turned into a literal position.
    """
    start: int | None = None
    end: int | None = None
    length: int | None = None
    if isinstance(value, dict):
        if dimension == "lines":
            start_keys = (
                "start_line",
                "start",
                "from",
                "first",
                "begin",
                "min",
                "gte",
            )
            end_keys = (
                "end_line",
                "end",
                "to",
                "last",
                "max",
                "lt",
                "finish",
            )
        elif dimension == "chars":
            start_keys = (
                "start_char",
                "start",
                "from",
                "first",
                "begin",
                "min",
                "gte",
            )
            end_keys = (
                "end_char",
                "end",
                "to",
                "last",
                "max",
                "lt",
                "finish",
            )
        else:
            start_keys = ("start", "from", "first", "begin", "min", "gte")
            end_keys = ("end", "to", "last", "max", "lt", "finish")
        start = _first_int(value, start_keys)
        end = _first_int(value, end_keys)
        length = _first_int(value, ("length", "count", "size"))
    elif isinstance(value, (list, tuple)) and len(value) >= 2:
        start = _as_int(value[0])
        end = _as_int(value[1])
    elif isinstance(value, str):
        parts = value
        for separator in ("..", ":", ",", "-"):
            parts = parts.replace(separator, " ")
        numbers = [
            number for number in map(_as_int, parts.split()) if number is not None
        ]
        if len(numbers) >= 2:
            start, end = numbers[0], numbers[1]
        elif len(numbers) == 1:
            start = end = numbers[0]
    if start is None:
        return None
    if end is None and length is not None:
        end = start + length
    result: dict[str, int | bool | None] = {"start": start, "end": end}
    if end == -1:
        result["end"] = None
        result["open_end"] = True
    return result


def _file_range_chars(value: object) -> dict | None:
    """Meaningful line-relative character offsets from a FileRange payload."""
    if not isinstance(value, dict):
        return None
    start = _as_int(value.get("start_char"))
    end = _as_int(value.get("end_char"))
    # FileRange serializes its default start_char=0 for every whole-line read.
    # That default is not a file-level character range and must not be projected
    # as one. A non-zero continuation or a bounded partial end is meaningful.
    if (start is None or start == 0) and end is None:
        return None
    return {"start": start if start is not None else 0, "end": end}


def _merge_ranges(ranges: list[dict], *, adjacent: bool) -> list[list[int]]:
    """Merge closed ranges that overlap (lines may also touch end to start)."""
    closed = sorted(
        (
            {"start": item["start"], "end": item["end"]}
            for item in ranges
            if item.get("start") is not None and item.get("end") is not None
        ),
        key=lambda item: (item["start"], item["end"]),
    )
    merged: list[list[int]] = []
    for item in closed:
        gap = 1 if adjacent else 0
        if merged and item["start"] <= merged[-1][1] + gap:
            if item["end"] > merged[-1][1]:
                merged[-1][1] = item["end"]
        else:
            merged.append([item["start"], item["end"]])
    return merged


def _stage_of(value: bool | None) -> str:
    if value is True:
        return "confirmed"
    if value is False:
        return "denied"
    return "unknown"


def _aggregate_stage(values: list[str]) -> str:
    known = [value for value in values if value != "unknown"]
    if not known:
        return "unknown"
    if all(value == "confirmed" for value in known):
        return "confirmed"
    if all(value == "denied" for value in known):
        return "denied"
    return "mixed"


def _range_key(value: dict | None) -> tuple | None:
    if not isinstance(value, dict):
        return None
    return (value.get("start"), value.get("end"), value.get("open_end", False))


def _relative_to_workspace(path: str, workspace: str | None) -> str | None:
    """Workspace-relative form of an observed path, or None if it escapes it."""
    if not workspace:
        return None
    try:
        relative = os.path.relpath(path, workspace)
    except (TypeError, ValueError):
        return None
    if relative == os.pardir or relative.startswith(os.pardir + os.sep):
        return None
    return relative.replace(os.sep, "/")


def _project_read(event: dict, workspace: str | None) -> dict | None:
    """Project one successful view observation into a read record, or None.

    Only ``view`` observations that carry a path qualify. Optional fields are
    read from generic keys when present; nothing is inferred from the file's
    text, so an unrecorded range or EOF stays ``None``.
    """
    observation = event.get("observation")
    if not isinstance(observation, dict):
        return None
    if _as_bool(observation.get("is_error")) is True:
        return None
    if observation.get("view_status") in {"directory", "image"}:
        return None
    command = observation.get("command")
    if command is None:
        command = event.get("command")
    containers = _read_containers(event, observation)
    path = _read_value(containers, _READ_PATH_KEYS)
    if not isinstance(path, str) or not path:
        return None

    version = _read_value(containers, _READ_VERSION_KEYS)
    version_text = str(version) if isinstance(version, (str, int)) else None

    requested_value = _read_value(containers, _READ_REQUESTED_LINE_KEYS)
    returned_value = _read_value(containers, _READ_RETURNED_LINE_KEYS)
    requested_lines = _as_range(requested_value, dimension="lines")
    returned_lines = _as_range(returned_value, dimension="lines")
    if returned_lines is None:
        start_line = _as_int(_read_value(containers, _READ_START_LINE_KEYS))
        if start_line is not None:
            returned_lines = {
                "start": start_line,
                "end": _as_int(_read_value(containers, _READ_END_LINE_KEYS)),
            }
    requested_chars = _as_range(
        _read_value(containers, _READ_REQUESTED_CHAR_KEYS), dimension="chars"
    )
    returned_chars = _as_range(
        _read_value(containers, _READ_RETURNED_CHAR_KEYS), dimension="chars"
    )
    requested_file_chars = _file_range_chars(requested_value)
    returned_file_chars = _file_range_chars(returned_value)
    if requested_file_chars is not None:
        requested_chars = requested_file_chars
    if returned_file_chars is not None:
        returned_chars = returned_file_chars
    if returned_chars is None:
        offset = _as_int(_read_value(containers, _READ_OFFSET_KEYS))
        length = _as_int(_read_value(containers, _READ_LENGTH_KEYS))
        if offset is not None or length is not None:
            returned_chars = {
                "start": offset,
                "end": offset + length
                if offset is not None and length is not None
                else None,
            }

    metadata_present = any(
        value is not None
        for value in (
            version_text,
            requested_lines,
            returned_lines,
            requested_chars,
            returned_chars,
        )
    )
    if command != "view" and not (command is None and metadata_present):
        return None

    return {
        "event_id": event.get("id"),
        "timestamp": event.get("timestamp"),
        "tool_call_id": event.get("tool_call_id"),
        "tool": event.get("tool_name"),
        "command": command,
        "path": path,
        "path_relative": _relative_to_workspace(path, workspace),
        "version": version_text,
        "version_known": version_text is not None,
        "requested": {"lines": requested_lines, "chars": requested_chars},
        "returned": {"lines": returned_lines, "chars": returned_chars},
        "char_range_unit": (
            "line_relative"
            if returned_file_chars is not None
            else "unspecified"
            if returned_chars is not None
            else None
        ),
        "eof": _as_bool(_read_value(containers, _READ_EOF_KEYS)),
        "truncated": _as_bool(_read_value(containers, _READ_TRUNCATED_KEYS)),
        "partial_line": _as_bool(_read_value(containers, _READ_PARTIAL_LINE_KEYS)),
        "stages": {
            "observed": "confirmed",
            "delivered": _stage_of(
                _as_bool(_read_value(containers, _READ_DELIVERED_KEYS))
            ),
            "understood": _stage_of(
                _as_bool(_read_value(containers, _READ_UNDERSTOOD_KEYS))
            ),
        },
        "metadata_keys": _present_keys(containers, _READ_METADATA_KEYS),
    }


def _group_reads(reads: list[dict]) -> list[dict]:
    """Group reads by path and explicit version; unknown versions never merge."""
    groups: dict[tuple, dict] = {}
    order: list[tuple] = []
    for read in reads:
        version = read.get("version")
        key = (
            ("version", read["path"], version)
            if version is not None
            else ("event", read["event_id"])
        )
        group = groups.get(key)
        if group is None:
            group = {
                "path": read["path"],
                "path_relative": read["path_relative"],
                "version": version,
                "version_known": version is not None,
                "reads": [],
            }
            groups[key] = group
            order.append(key)
        group["reads"].append(read)

    result: list[dict] = []
    for key in order:
        group = groups[key]
        included: list[dict] = group["reads"]
        lines = [
            read["returned"]["lines"]
            for read in included
            if read["returned"]["lines"] is not None
            and read.get("char_range_unit") != "line_relative"
        ]
        chars = [
            read["returned"]["chars"]
            for read in included
            if read["returned"]["chars"] is not None
            and read.get("char_range_unit") != "line_relative"
        ]
        line_chars: dict[int, list[dict]] = {}
        for read in included:
            line_range = read["returned"]["lines"]
            char_range = read["returned"]["chars"]
            if (
                read.get("char_range_unit") != "line_relative"
                or not isinstance(line_range, dict)
                or not isinstance(char_range, dict)
                or line_range.get("start") != line_range.get("end")
            ):
                continue
            line = line_range.get("start")
            if isinstance(line, int):
                line_chars.setdefault(line, []).append(char_range)
        result.append(
            {
                "path": group["path"],
                "path_relative": group["path_relative"],
                "version": group["version"],
                "version_known": group["version_known"],
                "read_count": len(included),
                "event_ids": [read["event_id"] for read in included],
                "merged_lines": _merge_ranges(lines, adjacent=True),
                "merged_chars": _merge_ranges(chars, adjacent=False),
                "line_char_ranges": [
                    {
                        "line": line,
                        "merged": _merge_ranges(ranges, adjacent=False),
                        "open_ended": [
                            item for item in ranges if item.get("end") is None
                        ],
                    }
                    for line, ranges in sorted(line_chars.items())
                ],
                "open_ended_lines": [item for item in lines if item.get("open_end")],
                "eof_confirmed": any(read["eof"] is True for read in included),
                "truncation_observed": any(
                    read["truncated"] is True for read in included
                ),
                "stages": {
                    "observed": "confirmed",
                    "delivered": _aggregate_stage(
                        [read["stages"]["delivered"] for read in included]
                    ),
                    "understood": _aggregate_stage(
                        [read["stages"]["understood"] for read in included]
                    ),
                },
            }
        )
    return result


def _repeated_reads(reads: list[dict]) -> list[dict]:
    """Repeated returned spans for the same path and observed version."""
    seen: dict[tuple, list[str]] = {}
    sample: dict[tuple, dict] = {}
    order: list[tuple] = []
    for read in reads:
        returned = read["returned"]
        basis = "returned"
        lines = _range_key(returned["lines"])
        chars = _range_key(returned["chars"])
        if lines is None and chars is None:
            basis = "requested"
            lines = _range_key(read["requested"]["lines"])
            chars = _range_key(read["requested"]["chars"])
        if lines is None and chars is None:
            continue
        key = (read["path"], read.get("version"), basis, lines, chars)
        if key not in seen:
            seen[key] = []
            sample[key] = read
            order.append(key)
        seen[key].append(read["event_id"])

    repeated: list[dict] = []
    for key in order:
        if len(seen[key]) < 2:
            continue
        read = sample[key]
        version_known = read.get("version") is not None
        repeated.append(
            {
                "path": read["path"],
                "path_relative": read["path_relative"],
                "version": read.get("version"),
                "version_known": version_known,
                "requested": read["requested"],
                "returned": read["returned"],
                "range_basis": key[2],
                "count": len(seen[key]),
                "event_ids": seen[key],
                "note": (
                    f"same {key[2]} range observed more than once"
                    if version_known
                    else f"same {key[2]} range observed more than once; "
                    "file version unverified"
                ),
            }
        )
    return repeated


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
        **kwargs: Any,
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
        except httpx.TransportError:
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

    def _wait_status(self, session: str) -> dict | None:
        """One wait sample, normalized, or None when the session is gone.

        The public ``status`` raises on an unknown id; a wait must classify it
        as missing and keep waiting for the others instead.
        """
        try:
            resolved = self._resolve_session(session)
        except ClientError:
            return None
        response = self._send(
            "GET",
            f"/api/conversations/{quote(resolved, safe='')}",
            tolerate_404=True,
        )
        if response.status_code == 404:
            return None
        data = response.json()
        return {
            "id": data.get("id", resolved),
            "status": _status_of(data),
            "admission_status": data.get("admission_status"),
            "result_state": data.get("result_state"),
            "iterations_used": data.get("iterations_used"),
            "iterations_remaining": data.get("iterations_remaining"),
        }

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
        # Every page, not the default one. This used `list_sessions()` with its
        # default limit of 50, so a session older than the fifty most recent
        # could not be addressed by short id at all -- and the message said "no
        # session matches", which reads exactly like "it was deleted". Hit for
        # real: a session the docs name as evidence not to delete was reported
        # missing, and it was sitting at row 53 of 58. Nothing expires here, so
        # every runtime crosses that line eventually and then quietly loses
        # its own history.
        for item in self._all_sessions():
            full = item.get("id")
            if full is None:
                continue
            if str(full).casefold().startswith(prefix):
                matches.append(str(full))

        if not matches:
            raise SessionNotFound(f"no session matches {session!r}")

        if len(matches) > 1:
            listed = ", ".join(short_id(full) for full in matches)
            raise AmbiguousSession(f"session prefix {session!r} is ambiguous: {listed}")

        return matches[0]

    def _profile_id(
        self, permission: str | None = None, llm_profile: str | None = None
    ) -> str:
        """Resolve a preset and an optional allowed LLM reference to a profile id.

        ``llm_profile`` is a name, never a credential. Selecting one that the
        runtime does not reference, or that the preset is not bound to, is a
        clear error rather than a silent fallback.
        """
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
        if llm_profile is not None:
            allowed = bootstrap.allowed_llm_profiles()
            if llm_profile not in allowed:
                raise ClientError(
                    f"LLM profile {llm_profile!r} is not allowed; allowed: "
                    f"{', '.join(allowed) or 'none'}. This runtime exposes only "
                    "the LLM profiles its agent profiles reference."
                )
            bound = bootstrap.agent_profile_llm_ref(preset)
            if bound != llm_profile:
                raise ClientError(
                    f"permission {preset!r} is bound to LLM profile {bound!r}; "
                    f"selecting {llm_profile!r} would retarget it. Apply a "
                    "selected LLM profile change instead of selecting one per "
                    "dispatch."
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
            "allowed_llm_profiles": bootstrap.allowed_llm_profiles(),
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
        llm_profile: str | None = None,
        max_iterations: int | None = None,
        tags: dict[str, str] | None = None,
        idempotency_key: str | None = None,
        attachments: list[str] | None = None,
    ) -> dict:
        """Start a new conversation in a workspace for a text task.

        ``llm_profile`` names the allowed LLM profile to run under. It is a
        reference the daemon resolves against its own stores, so no credential
        crosses this boundary; an unknown or unbound name is refused.
        """
        workspace = os.path.abspath(os.path.expanduser(workspace))
        os.makedirs(workspace, exist_ok=True)
        preset = permissions.normalise(permission)
        profile_id = self._profile_id(preset, llm_profile)
        resolved_llm = (
            bootstrap.agent_profile_llm_ref(preset) or bootstrap.LLM_PROFILE_NAME
        )

        cap = config.max_running_sessions()
        if cap > 0:
            running = [
                s
                for s in self.list_sessions(limit=100)
                if (s.get("status") or "").lower() == "running"
            ]
            if len(running) >= cap:
                raise ClientError(
                    f"{len(running)} sessions are already running and the limit "
                    f"is {cap}. Wait for one to finish, stop one, or raise "
                    "AGENTRT_MAX_SESSIONS in the environment of whatever "
                    "dispatches -- this check runs here, not on the daemon, so "
                    "setting it on the daemon has no effect."
                )

        body: dict = {
            "workspace": {"working_dir": workspace},
            "agent_profile_id": profile_id,
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
        if max_iterations is not None:
            body["max_iterations"] = max_iterations
        if tags:
            body["tags"] = _clean_tags(tags)
        if idempotency_key:
            body["idempotency_key"] = idempotency_key
        if attachments:
            blocks = self._attachment_blocks(attachments, workspace=workspace)
            body["initial_message"]["content"] = [
                {"type": "text", "text": task},
                *blocks,
            ]

        data = self._send("POST", "/api/conversations", json=body).json()
        full_id = data.get("id")
        return {
            "id": full_id,
            "short_id": short_id(full_id) if full_id else None,
            "status": _status_of(data),
            "workspace": workspace,
            "permission": preset,
            "llm_profile": resolved_llm,
        }

    def _all_sessions(self) -> list[dict]:
        """Every session the daemon knows, paged.

        Resolving a prefix has to see all of them: a partial view turns a real
        session into a "not found", and would also miss the case where a prefix
        is ambiguous because the second match is on a later page -- which would
        pick one of two sessions silently, the worse of the two failures.

        Bounded at 50 pages so a runtime with a pathological number of sessions
        degrades into a wrong answer rather than an unbounded loop; a caller
        that far out should be using full ids.
        """
        out: list[dict] = []
        page: str | None = None
        for _ in range(50):
            params: dict[str, object] = {"limit": 100}
            if page:
                params["page_id"] = page
            data = self._send("GET", "/api/conversations/search", params=params).json()
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list) or not items:
                break
            out.extend(item for item in items if isinstance(item, dict))
            page = data.get("next_page_id") if isinstance(data, dict) else None
            if not page:
                break
        return out

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
                    "title": session.get("title")
                    if isinstance(session, dict)
                    else None,
                    "status": _status_of(session),
                    "created_at": session.get("created_at")
                    if isinstance(session, dict)
                    else None,
                    "updated_at": session.get("updated_at")
                    if isinstance(session, dict)
                    else None,
                    "tags": (session.get("tags") or {})
                    if isinstance(session, dict)
                    else {},
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
        # Reported whenever the session was given one, because it is the only
        # thing that distinguishes the two ways a session reaches `error`. The
        # daemon carries no message with that state -- measured: a session that
        # hit its limit came back with `execution_status: "error"` and no error
        # field anywhere in the payload -- so a caller seeing `error` alongside
        # a limit it set has an explanation, and one seeing `error` without a
        # limit knows to go and read the transcript.
        if data.get("max_iterations") is not None:
            result["max_iterations"] = data.get("max_iterations")
        result["tags"] = data.get("tags") or {}
        # H2 request scope. `result_state` says whether the newest input has an
        # answer yet; `admission_status` distinguishes accepted-but-not-started
        # work from a session that has nothing to do -- a freshly dispatched
        # session is `queued`, not ambiguous `idle`.
        result["result_state"] = data.get("result_state")
        result["admission_status"] = data.get("admission_status")
        result["iterations_used"] = data.get("iterations_used")
        result["iterations_remaining"] = data.get("iterations_remaining")
        return result

    def result(self, session: str) -> dict:
        """Return the answer for the session's current request.

        ``result`` is the text, or ``None`` when ``state`` is ``pending`` --
        the newest input has no answer yet, and a previous request's answer is
        never returned in its place. An empty string is a valid ``final``
        answer. ``state``, ``error``, ``iterations_*``,
        ``last_completed_tool`` and ``last_progress_at`` describe provenance
        and progress.
        """
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
                "state": "unavailable",
                "result": None,
            }

        data = response.json()
        # The final-response payload describes the answer, not the session, so
        # it carries no id. Use the id we resolved to make the request.
        full_id = resolved
        status = _status_of(data)
        if status is None:
            status = self.status(session).get("status")

        payload = _response_payload(full_id, data)
        payload["status"] = status
        return payload

    def _attachment_blocks(
        self,
        attachments: list[str],
        *,
        workspace: str,
    ) -> list[dict]:
        """Typed image blocks for the initial message, guarded.

        Each path must resolve inside the workspace, must not be another name
        for one of the runtime's credential files, must sniff as an allowed
        image format, and must be within the size cap. Reading is enough: the
        session's permission does not have to grant writing.

        The guard is applied as the workspace permission regardless of the
        session's own preset. `broad` short-circuits `check_path`, so honouring
        it here would embed any file on the machine into the message.
        """
        urls: list[str] = []
        for raw in attachments:
            try:
                resolved = permissions.check_path(
                    str(raw),
                    root=workspace,
                    # Not the session's preset: see the docstring.
                    permission="workspace",
                    writing=False,
                )
            except permissions.PermissionDenied as exc:
                raise ClientError(
                    f"attachment {raw!r} is not readable from this workspace: {exc}"
                ) from exc
            if not resolved.is_file():
                raise ClientError(f"attachment {raw!r} is not a file")
            size = resolved.stat().st_size
            if size > MAX_ATTACHMENT_BYTES:
                raise ClientError(
                    f"attachment {raw!r} is {size} bytes, above the "
                    f"{MAX_ATTACHMENT_BYTES} byte cap"
                )
            with resolved.open("rb") as handle:
                mime = _sniff_image_mime(handle.read(16))
            if mime is None:
                raise ClientError(
                    f"attachment {raw!r} is not a PNG, JPEG, GIF or WebP image "
                    "(detected from its bytes, not its name)"
                )
            encoded = base64.b64encode(resolved.read_bytes()).decode("ascii")
            urls.append(f"data:{mime};base64,{encoded}")
        return [{"type": "image", "image_urls": urls}] if urls else []

    def dispatch_from(
        self,
        source: str,
        task: str,
        *,
        title: str | None = None,
        tags: dict[str, str] | None = None,
    ) -> dict:
        """Fork a session and give the fork the task.

        This is what context inheritance means here. The native sub-agent a
        caller may be used to forks *the caller's own conversation*, which this
        daemon cannot read: it holds no orchestrator context. What it can do is
        hand a new session everything another AgentRT session knows, so a
        reviewer can start from the writer's history instead of from a summary
        of it.

        The fork inherits the source's agent, workspace and permission -- that
        is what makes it useful -- so there is nothing to choose here except the
        task and its metadata. It runs in the same directory as the source, so
        two writers there are subject to the shared-writer cap.
        """
        resolved = self._resolve_session(source)
        body: dict = {}
        if title is not None:
            body["title"] = title
        if tags:
            body["tags"] = _clean_tags(tags)
        forked = self._send(
            "POST",
            f"/api/conversations/{quote(resolved, safe='')}/fork",
            json=body,
        ).json()
        new_id = forked.get("id")
        if not new_id:
            raise ClientError(f"fork of {source!r} returned no conversation id")
        self.send(new_id, task)
        return {
            "id": new_id,
            "short_id": short_id(new_id),
            "status": _status_of(forked) or self.status(new_id).get("status"),
            "forked_from": resolved,
            "title": forked.get("title"),
        }

    def dispatch_many(
        self,
        tasks: list[dict],
        *,
        max_batch: int = 25,
    ) -> dict:
        """Submit several tasks once and return a per-item outcome.

        Every item is validated before the first side effect, so a malformed
        item cannot leave half a batch created. Capacity overflow is not an
        error: the daemon queues accepted work and `capacity` reports the
        backlog, so one submission is followed by one collection of outcomes
        rather than caller-managed re-dispatch.

        An item is a dict of the same arguments `dispatch` takes (`task`,
        `workspace`, plus optional `title`, `permission`, `tags`, `max_iterations`,
        `idempotency_key`). Items that fail validation or creation are reported
        individually; the rest are created.
        """
        if not tasks:
            raise ValueError("dispatch_many needs at least one task")
        if len(tasks) > max_batch:
            raise ValueError(
                f"{len(tasks)} tasks exceeds the batch size of {max_batch}; "
                "submit in waves so a partial failure stays bounded"
            )
        # Validate everything before creating anything.
        for index, item in enumerate(tasks):
            if not isinstance(item, dict) or not item.get("task"):
                raise ValueError(f"item {index} has no task")
            if not item.get("workspace"):
                raise ValueError(f"item {index} has no workspace")

        accepted: list[dict] = []
        failed: list[dict] = []
        for index, item in enumerate(tasks):
            try:
                created = self.dispatch(**item)
            except ClientError as exc:
                failed.append(
                    {"index": index, "error": type(exc).__name__, "message": str(exc)}
                )
                continue
            accepted.append({"index": index, **created})
        return {
            "accepted": accepted,
            "failed": failed,
            "count": len(accepted),
            "requested": len(tasks),
        }

    def usage(self, session: str) -> dict:
        """Report what a session's model calls consumed.

        Raw counts are the provider's own numbers, copied verbatim; the
        normalized view is derived alongside them. The projection never guesses:
        a field the stats owner did not record is reported as unavailable rather
        than as zero. Nothing here contains a prompt, completion, reasoning
        content or credential.
        """
        resolved = self._resolve_session(session)
        return self._send(
            "GET",
            f"/api/conversations/{quote(resolved, safe='')}/usage",
        ).json()

    def capacity(self) -> dict:
        """Report the daemon's admission surface.

        `limiting_dimension` names the cap that is binding, or null when the run
        cap is disabled. `available` is null in that case: there is no ceiling
        to subtract from, and a numeric remainder would read as a small one.
        `queued` is accepted work waiting for a slot, in submission order.
        """
        return self._send("GET", "/api/conversations/capacity").json()

    def finalize(self, session: str, *, summary: bool = False) -> dict:
        """Stop a session at a safe boundary and return the outcome it has.

        The barrier lets the in-flight step reach a safe boundary and blocks
        further tool starts. Cancellation is not rollback: an external effect
        already running may still be. Repeating the call for the same input
        returns the same outcome rather than running anything again.

        `summary` asks for the tools-disabled wrap-up. It is off by default and
        the deployment may refuse it; when no summary can run, the partial
        record is returned rather than a summary that does not exist.
        """
        resolved = self._resolve_session(session)
        data = self._send(
            "POST",
            f"/api/conversations/{quote(resolved, safe='')}/finalize",
            json={"summary": bool(summary)},
        ).json()
        payload = _response_payload(resolved, data)
        payload["status"] = self.status(resolved).get("status")
        return payload

    def wait(
        self,
        session_ids: list[str],
        *,
        mode: str = "all",
        timeout: float = 600.0,
        poll_interval: float = 2.0,
    ) -> dict:
        """Block until the sessions settle, or the timeout elapses.

        This is the blocking wait of a foreground sub-agent: it returns once the
        work has an outcome. A timeout returns the unfinished ids as
        ``still_running`` -- never as failures, and never with partial output
        presented as a final answer.

        A session is reported settled only when its terminal execution status
        and its admission state agree *and* the same condition held on the
        previous sample. The second sample is deliberate: ``finished`` is
        provisional, because a run can flip back to ``running`` for a stop hook
        or a message that arrived during the final step, and a wait that fired on
        the first ``finished`` would report an answer still being revised.

        Items under ``still_running`` carry the last progress sample -- status,
        admission, result state and iterations -- so a poller can see movement
        without a second call. They carry no result: partial output is never
        presented as an answer.

        mode ``all`` waits for every id (or the timeout); ``any`` returns as soon
        as one settles. The result groups ids by outcome -- ``completed``,
        ``partial``, ``failed``, ``stopped`` (paused, resumable), ``missing``
        (unknown or deleted) and ``still_running`` -- with ``timed_out`` saying
        whether the deadline ended the wait. A ``wait_any`` that returns on its
        first outcome reports ``timed_out`` false even though other ids are
        still running: the deadline is not what ended it.

        SAFE CEILING. A large ``timeout`` is capped internally at
        ``AGENTRT_WAIT_SAFE_CEILING_SECONDS`` (900s by default) regardless of
        what was requested, and returns ``still_running``/``timed_out`` at
        that boundary rather than holding the call open indefinitely. This
        exists because a transport between an orchestrator and this server
        can have its own, undocumented idle-connection ceiling -- measured
        once at roughly 1800s -- that fires first and kills the call with a
        generic error before this method's own graceful answer is ever
        reached. Call again on ``still_running``; do not raise the requested
        ``timeout`` to work around this, since a larger request is
        truncated to the same safe ceiling either way. For anything you
        expect to run long, prefer polling `status` between other work, or a
        backgrounded poll loop, over one held-open `wait` call.
        """
        if mode not in ("all", "any"):
            raise ValueError("mode must be 'all' or 'any'")
        interval = max(0.5, float(poll_interval))
        requested_timeout = max(0.0, float(timeout))
        safe_ceiling = config.wait_safe_ceiling_seconds()
        effective_timeout = (
            min(requested_timeout, safe_ceiling)
            if safe_ceiling > 0
            else requested_timeout
        )
        deadline = time.monotonic() + effective_timeout
        ids = list(dict.fromkeys(str(session) for session in session_ids))

        started = time.monotonic()
        pending = set(ids)
        settled: set[str] = set()
        missing: set[str] = set()
        terminal_prev: dict[str, bool] = {}
        last_seen: dict[str, dict] = {}
        timed_out = False

        while True:
            for session in list(pending):
                status = self._wait_status(session)
                if status is None:
                    missing.add(session)
                    pending.discard(session)
                    continue
                last_seen[session] = status
                terminal = _is_settled(status)
                if terminal and terminal_prev.get(session):
                    settled.add(session)
                    pending.discard(session)
                else:
                    terminal_prev[session] = terminal
            if settled and mode == "any":
                break
            if not pending:
                break
            remaining_time = deadline - time.monotonic()
            if remaining_time <= 0:
                timed_out = True
                break
            # Never sleep past the deadline: a sweep plus the interval would
            # otherwise overshoot the timeout the caller asked for.
            time.sleep(min(interval, remaining_time))

        buckets: dict[str, list] = {
            "completed": [],
            "partial": [],
            "failed": [],
            "stopped": [],
            "missing": [],
            "still_running": [],
        }
        for session in ids:
            if session in missing:
                buckets["missing"].append(
                    {"id": session, "short_id": short_id(session), "bucket": "missing"}
                )
            elif session in settled:
                try:
                    payload = self.result(session)
                except ClientError:
                    # Deleted between the last sample and the result read.
                    buckets["missing"].append(
                        {
                            "id": session,
                            "short_id": short_id(session),
                            "bucket": "missing",
                        }
                    )
                    continue
                bucket = _wait_bucket(payload)
                payload["bucket"] = bucket
                buckets[bucket].append(payload)
            else:
                # Progress as last sampled, so a poller can see movement
                # (iterations, admission) without a second call. It is the same
                # sample the settle decision used, not a fresh one.
                item = {
                    "id": session,
                    "short_id": short_id(session),
                    "bucket": "still_running",
                }
                seen = last_seen.get(session) or {}
                for key in (
                    "status",
                    "admission_status",
                    "result_state",
                    "iterations_used",
                    "iterations_remaining",
                ):
                    value = seen.get(key)
                    if value is not None:
                        item[key] = value
                buckets["still_running"].append(item)

        return {
            **buckets,
            "timed_out": timed_out,
            "waited": round(time.monotonic() - started, 3),
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
                entry = {
                    "type": "action",
                    "id": item.get("id"),
                    "tool": item.get("tool_name"),
                    "thought": _capped(_join_text(item.get("thought")), 400),
                }
                _add_action_location(entry, item.get("action"))
                events.append(entry)
            elif kind == "ObservationEvent":
                observation = item.get("observation") or {}
                events.append(
                    {
                        "type": "observation",
                        "id": item.get("id"),
                        "tool": item.get("tool_name"),
                        "output": _capped(_join_text(observation.get("content")), 600),
                    }
                )
            elif kind == "ConversationErrorEvent":
                # H2: errors were silently dropped here, so a session that
                # failed for no visible reason looked like a clean stop.
                events.append(
                    {
                        "type": "error",
                        "id": item.get("id"),
                        "code": item.get("code"),
                        "detail": _capped(str(item.get("detail") or ""), 400),
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

    def read_evidence(
        self,
        session: str,
        *,
        limit: int = 100,
        max_pages: int = 50,
        cursor: str | None = None,
    ) -> dict:
        """Project what a session read, from its persisted observations.

        Nothing is stored for this: the projection reads the same
        ``ObservationEvent`` records the transcript does, so it stays correct
        for sessions that ran before this method existed.

        COST AND PAGINATION. It pages ``events/search`` at up to ``limit`` raw
        events per request (capped at 100 by the server) until the daemon returns
        no ``next_page_id`` or ``max_pages`` requests have been made -- a long
        session is several HTTP calls and this call blocks for all of them.
        ``complete`` says whether the end was reached; when it is false,
        ``next_cursor`` continues from where the bound stopped.

        WHAT IT PROVES. Only successful ``file_editor`` view observations.
        ``observed`` means the tool result was persisted. ``delivered`` (content
        actually serialized into an LLM request) and ``understood`` (content the
        model acted on) are separate stages and are reported as ``unknown``
        unless a source event records them; they cannot be inferred here.

        RANGES AND EOF. Optional version/hash, requested/returned line and
        character ranges, EOF and truncation metadata are read from generic keys
        when a paging writer has added them. Where the source event did not
        record a range or an EOF, the value is ``null`` -- never guessed. Ranges
        are merged only for reads carrying the same explicit file version; a
        read with no version is never merged across events. ``repeated_reads``
        reports the same requested range observed more than once.
        """
        if limit <= 0:
            raise ClientError("limit must be positive")
        if max_pages <= 0:
            raise ClientError("max_pages must be positive")

        resolved = self._resolve_session(session)
        page_size = min(limit, 100)

        raw_events: list[dict] = []
        pages = 0
        complete = True
        next_cursor = cursor
        while pages < max_pages:
            params: dict[str, object] = {
                "sort_order": "TIMESTAMP_DESC",
                "limit": page_size,
            }
            if next_cursor:
                params["page_id"] = next_cursor
            data = self._send(
                "GET",
                f"/api/conversations/{quote(resolved, safe='')}/events/search",
                params=params,
            ).json()
            items = data.get("items") if isinstance(data, dict) else None
            if not isinstance(items, list):
                complete = False
                next_cursor = None
                break
            pages += 1
            raw_events.extend(item for item in items if isinstance(item, dict))
            page = data.get("next_page_id") if isinstance(data, dict) else None
            next_cursor = page if isinstance(page, str) and page else None
            if not next_cursor:
                complete = True
                break
        else:
            complete = False
        status = self.status(resolved)
        workspace = status.get("workspace")

        reads: list[dict] = []
        for event in raw_events:
            if event.get("kind") != "ObservationEvent":
                continue
            if event.get("tool_name") not in FILE_EDITOR_TOOL_NAMES:
                continue
            workspace_dir = workspace if isinstance(workspace, str) else None
            read = _project_read(event, workspace_dir)
            if read is not None:
                reads.append(read)
        reads.sort(key=lambda read: str(read.get("timestamp") or ""))

        return {
            "id": resolved,
            "short_id": short_id(resolved),
            "status": status.get("status"),
            "events_scanned": len(raw_events),
            "pages": pages,
            "complete": complete,
            "next_cursor": None if complete else next_cursor,
            "reads": reads,
            "file_versions": _group_reads(reads),
            "repeated_reads": _repeated_reads(reads),
            "assumptions": [
                "read evidence is projected from already-persisted successful "
                "ObservationEvents; no second transcript store is written",
                "line ranges are treated as 1-based and inclusive; character "
                "ranges are reported exactly as recorded, without an assumed "
                "convention",
                "optional version, range, EOF and truncation metadata is read "
                "from generic keys and only when a source event records it",
                "ranges are merged only within one explicit file version; a "
                "read without a version is never merged across events",
                "observed, delivered and understood are separate stages: only "
                "observed is proven by a tool observation",
            ],
            "notes": [
                "a read with version_known false means the source event carried "
                "no version/hash, so its ranges cannot be merged with any other",
                "eof and range fields are null when the source event did not "
                "record them; this projection never infers them from file text",
                "metadata_keys on each read lists the optional keys actually "
                "found, so a caller can tell which assumptions applied",
            ],
            "root_integration": [
                "the event owner must persist version/hash, requested and "
                "returned line/character ranges, EOF and truncation on the "
                "file_editor view result; this projection only reads those keys "
                "and reports null where they are absent",
                "delivered requires the SDK/server to record which observation "
                "content was serialized into an LLM request; understood is not "
                "derivable from events and must be recorded explicitly to be "
                "claimed",
                "generic key spellings are accepted on the observation, under "
                "its metadata/read_evidence/read/view nested dicts, and on the "
                "event itself; metadata_keys reports what was recognized",
            ],
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
        self._send("POST", f"/api/conversations/{quote(resolved, safe='')}/interrupt")
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

    def tag(self, session: str, tags: dict[str, str]) -> dict:
        """Merge tags onto a session and return the result.

        Merge, although the daemon's PATCH replaces: it takes the whole map and
        writes it, so a caller adding one tag with the obvious call silently
        drops every tag already there. Measured before this was written. Reading
        first and merging costs one extra request and makes the operation mean
        what its name says.

        An empty string as a value removes that key, which is the only way to
        remove one when the write is a merge.
        """
        current = dict(self.status(session).get("tags") or {})
        for key, value in _clean_tags(tags).items():
            if value == "":
                current.pop(key, None)
            else:
                current[key] = value
        resolved = self._resolve_session(session)
        self._send(
            "PATCH",
            f"/api/conversations/{quote(resolved, safe='')}",
            json={"tags": current},
        )
        return {
            "id": resolved,
            "short_id": short_id(resolved),
            "tags": current,
        }

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

        ``outcome`` types the listing so an empty answer is not read as a
        failure. ``empty`` is the positive result of a successful filtered scan:
        the session ran and wrote nothing. It is distinct from ``unavailable``
        (no workspace, or a workspace path that is not a directory here),
        ``unfiltered`` (no start time, so nothing was filtered), ``partial`` (the
        walk or a ``stat`` failed), ``truncated`` (more than two hundred files
        matched) and ``failed`` (the walk itself raised). ``complete`` is true
        only for a fully filtered, untruncated scan; ``scan_errors`` counts
        entries that could not be read.
        """
        resolved = self._resolve_session(session)
        status = self.status(resolved)
        workspace = status.get("workspace")

        if path is not None:
            if not workspace:
                raise ClientError(
                    f"no workspace known for session {short_id(resolved)}"
                )
            if os.path.isabs(path):
                raise ClientError(f"absolute paths are not allowed: {path!r}")
            # `check_path` rather than a containment test of our own. An
            # earlier version asked only "is it inside the workspace", which is
            # most of the guard's job and not all of it: a hard link inside the
            # workspace is a second name for a file outside it, so the same
            # credential leak that `is_runtime_secret` was written to close
            # stayed open on this path -- reachable not by the agent but by the
            # orchestrator reading what it thought was a workspace file, which
            # puts the key in a model's context. Found by a dispatched review
            # and reproduced against the real credential before fixing.
            #
            # Asking the guard is also the only way these two stay in step. The
            # dot-and-space and UNC rules arrived in `check_path` and never
            # reached the copy here; routing through it means the next rule
            # does not have to be remembered twice.
            root = permissions._real(workspace)
            try:
                permissions.check_path(
                    path, root=root, permission="readonly", writing=False
                )
            except permissions.PermissionDenied as denied:
                raise ClientError(f"refusing {path!r}: {denied}") from denied
            workspace_path = (
                f"/api/conversations/{quote(resolved, safe='')}/workspace/"
                f"{quote(path, safe='/')}"
            )
            response = self._send("GET", workspace_path)
            return {
                "id": resolved,
                "short_id": short_id(resolved),
                "path": path,
                "content": response.text,
            }

        since = _started_at(status)
        base = {
            "id": resolved,
            "short_id": short_id(resolved),
            "workspace": workspace,
            "since": (
                datetime.fromtimestamp(since, tz=UTC).isoformat()
                if since is not None
                else None
            ),
            # Says out loud that the filter did not run. If `created_at` is
            # missing or unparseable there is no cutoff, every file in the
            # workspace is listed, and that is precisely the behaviour this
            # function was rewritten to stop -- silently, and looking like a
            # correct answer. A caller reading `files` alone cannot tell.
            "filtered": since is not None,
            "files": [],
            "truncated": False,
            "total_scanned": 0,
            # Files seen outside the pruned directories, which is not the
            # size of the workspace and should not be read as one: a repository
            # with a 400-file virtualenv reports 4. Its job is to distinguish
            # "walked the workspace and the session wrote nothing" from "the
            # walk found nothing at all". The first is a real and common answer
            # -- a session can run an hour and produce no file -- and it should
            # not look like a broken call.
            "pruned": sorted(PRUNED_DIRS),
        }

        # A workspace the daemon named but that is absent (or not a directory)
        # here is not an empty result: "wrote nothing" is a positive answer and
        # must not be confused with "nothing could be listed".
        if not workspace or not os.path.isdir(workspace):
            return {
                **base,
                "outcome": ARTIFACTS_UNAVAILABLE,
                "empty": False,
                "complete": False,
                "scan_errors": 0,
            }

        found: list[tuple[float, dict]] = []
        total = 0
        scan_errors = 0

        def _on_walk_error(_error: OSError) -> None:
            nonlocal scan_errors
            scan_errors += 1

        try:
            for dirpath, dirnames, filenames in os.walk(
                workspace, onerror=_on_walk_error
            ):
                dirnames[:] = sorted(d for d in dirnames if d not in PRUNED_DIRS)
                for name in filenames:
                    full = os.path.join(dirpath, name)
                    try:
                        entry_stat = os.stat(full)
                    except OSError:
                        scan_errors += 1
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
                                    entry_stat.st_mtime, tz=UTC
                                ).isoformat(),
                            },
                        )
                    )
        except OSError:
            # The walk itself failed, so the listing is unknown rather than
            # empty. The underlying message can carry a host path, so only the
            # outcome is reported.
            return {
                **base,
                "total_scanned": total,
                "outcome": ARTIFACTS_FAILED,
                "empty": False,
                "complete": False,
                "scan_errors": scan_errors,
            }

        # Newest first, so the file the session finished with is the one read
        # first. Path order buries it behind whatever the repository is called.
        found.sort(key=lambda entry: entry[0], reverse=True)
        files = [entry[1] for entry in found]
        listing_truncated = len(files) > _ARTIFACTS_LIMIT
        if scan_errors:
            outcome = ARTIFACTS_PARTIAL
        elif since is None:
            outcome = ARTIFACTS_UNFILTERED
        elif listing_truncated:
            outcome = ARTIFACTS_TRUNCATED
        elif files:
            outcome = ARTIFACTS_LISTED
        else:
            outcome = ARTIFACTS_EMPTY
        return {
            **base,
            "files": files[:_ARTIFACTS_LIMIT],
            "truncated": listing_truncated,
            "total_scanned": total,
            # Typed outcome: `empty` after a successful filtered scan is a
            # positive result and is distinct from an unavailable workspace, an
            # unfiltered or partial scan, truncation and failure.
            "outcome": outcome,
            "empty": outcome == ARTIFACTS_EMPTY,
            "complete": (
                since is not None and not scan_errors and not listing_truncated
            ),
            "scan_errors": scan_errors,
        }
