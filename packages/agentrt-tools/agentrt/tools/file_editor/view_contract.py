"""Paging and continuation-cursor primitives for the file editor ``view`` command.

The view contract keeps reads *contiguous*: a page is a span of whole source
lines, and any continuation is carried by an opaque cursor bound to the file's
identity, content hash and the options that produced the page.  A cursor that
no longer matches is rejected rather than silently returning shifted line
numbers.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict


CURSOR_VERSION = 1

ViewStatus = Literal[
    "ok",
    "eof",
    "empty",
    "truncated",
    "partial_line",
    "binary",
    "too_large",
    "decode_error",
    "read_error",
    "out_of_range",
    "file_changed",
    "invalid_cursor",
    "directory",
    "image",
    "validation_error",
]


class FileRange(BaseModel):
    """A span of source, expressed in 1-based lines and 0-based characters."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    start_line: int
    end_line: int
    start_char: int = 0
    end_char: int | None = None


class FileScan(BaseModel):
    """Decode-free facts about a file, gathered in a single binary pass."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    size: int
    sha256: str
    line_count: int
    newline: str
    has_final_newline: bool
    file_id: str


class Page(BaseModel):
    """One whole-line (or partial-line) page of a file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    lines: list[tuple[int, str]]
    returned_range: FileRange | None
    partial: bool
    truncated: bool
    eof: bool
    next_line: int | None = None
    next_char: int = 0


class InvalidCursorError(Exception):
    """Raised when a continuation cursor cannot be trusted."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def scan_file(path: Path, chunk_size: int = 1 << 16) -> FileScan:
    """Hash and characterise a file without decoding it into memory."""
    digest = hashlib.sha256()
    size = 0
    n_lf = 0
    n_cr = 0
    n_crlf = 0
    prev_cr = False
    last_byte = -1
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            size += len(chunk)
            n_lf += chunk.count(0x0A)
            n_cr += chunk.count(0x0D)
            n_crlf += chunk.count(b"\r\n")
            if prev_cr and chunk[0] == 0x0A:
                n_crlf += 1
            prev_cr = chunk[-1] == 0x0D
            last_byte = chunk[-1]
            digest.update(chunk)

    lone_lf = n_lf - n_crlf
    lone_cr = n_cr - n_crlf
    total = n_crlf + lone_lf + lone_cr
    has_final_newline = last_byte in (0x0A, 0x0D)

    if size == 0:
        line_count = 0
    elif has_final_newline:
        line_count = total
    else:
        line_count = total + 1

    if size == 0 or total == 0:
        newline = "none"
    elif n_crlf and not lone_lf and not lone_cr:
        newline = "\r\n"
    elif lone_lf and not n_crlf and not lone_cr:
        newline = "\n"
    elif lone_cr and not n_crlf and not lone_lf:
        newline = "\r"
    else:
        newline = "mixed"

    try:
        stat = path.stat()
        file_id = f"{stat.st_dev}:{stat.st_ino}"
    except OSError:
        file_id = ""

    return FileScan(
        size=size,
        sha256=digest.hexdigest(),
        line_count=line_count,
        newline=newline,
        has_final_newline=has_final_newline,
        file_id=file_id,
    )


def _strip_terminator(line: str) -> str:
    if line.endswith("\r\n"):
        return line[:-2]
    if line.endswith("\n") or line.endswith("\r"):
        return line[:-1]
    return line


def read_page(
    path: Path,
    encoding: str,
    *,
    start_line: int,
    start_char: int,
    req_end: int | None,
    max_lines: int,
    char_budget: int,
) -> Page:
    """Read one page of whole lines, splitting a line only when it cannot fit.

    ``start_char`` continues inside ``start_line``; the returned
    ``next_char`` always advances past ``start_char`` so callers cannot loop.
    """
    out: list[tuple[int, str]] = []
    used = 0
    partial = False
    next_line: int | None = None
    next_char = 0
    max_lines = max(1, max_lines)
    char_budget = max(1, char_budget)

    with open(path, encoding=encoding, newline="") as f:
        for lineno, raw in enumerate(f, 1):
            if lineno < start_line:
                continue
            if req_end is not None and lineno > req_end:
                break

            content = _strip_terminator(raw)
            offset = start_char if lineno == start_line else 0
            if offset:
                if offset >= len(content):
                    continue
                content = content[offset:]

            if len(out) >= max_lines or (used + len(content) > char_budget and out):
                next_line = lineno
                next_char = 0
                break
            if used + len(content) > char_budget:
                room = max(1, char_budget - used)
                piece = content[:room]
                out.append((lineno, piece))
                partial = True
                next_line = lineno
                next_char = offset + len(piece)
                break

            out.append((lineno, content))
            used += len(content)

    returned_range: FileRange | None = None
    if out:
        first_line = out[0][0]
        returned_range = FileRange(
            start_line=first_line,
            end_line=out[-1][0],
            start_char=start_char if first_line == start_line else 0,
            end_char=next_char if partial else None,
        )

    return Page(
        lines=out,
        returned_range=returned_range,
        partial=partial,
        truncated=next_line is not None,
        eof=False,
        next_line=next_line,
        next_char=next_char,
    )


def _signature(body: dict[str, Any]) -> str:
    payload = {key: body[key] for key in sorted(body) if key != "sig"}
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


def encode_cursor(
    *,
    path: str,
    file_id: str,
    digest: str,
    line: int,
    char: int,
    req_start: int,
    end_line: int | None,
    max_lines: int,
) -> str:
    body: dict[str, Any] = {
        "v": CURSOR_VERSION,
        "path": path,
        "file_id": file_id,
        "digest": digest,
        "line": line,
        "char": char,
        "req_start": req_start,
        "end_line": end_line,
        "max_lines": max_lines,
    }
    body["sig"] = _signature(body)
    raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


_CURSOR_TYPES: tuple[tuple[str, type], ...] = (
    ("path", str),
    ("file_id", str),
    ("digest", str),
    ("line", int),
    ("char", int),
    ("req_start", int),
    ("max_lines", int),
)


def decode_cursor(token: str) -> dict[str, Any]:
    try:
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        body = json.loads(raw.decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - any malformed token is invalid
        raise InvalidCursorError("not a valid continuation token") from exc

    if not isinstance(body, dict):
        raise InvalidCursorError("payload is not an object")

    signature = body.get("sig")
    if not isinstance(signature, str) or signature != _signature(body):
        raise InvalidCursorError("integrity check failed")

    if body.get("v") != CURSOR_VERSION:
        raise InvalidCursorError("unsupported cursor version")

    for key, expected in _CURSOR_TYPES:
        value = body.get(key)
        if isinstance(value, bool) or not isinstance(value, expected):
            raise InvalidCursorError(f"field {key!r} is missing or invalid")

    end_line = body.get("end_line")
    if end_line is not None and (
        isinstance(end_line, bool) or not isinstance(end_line, int)
    ):
        raise InvalidCursorError("field 'end_line' is invalid")

    if body["line"] < 1 or body["char"] < 0 or body["req_start"] < 1:
        raise InvalidCursorError("position is out of range")
    if body["max_lines"] < 1:
        raise InvalidCursorError("page size is invalid")

    return body
