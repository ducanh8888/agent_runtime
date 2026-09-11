"""A lifetime token ledger that survives session deletion.

A total derived from the conversation store is not a ledger: deleting a session
removes the row it is measured from. Measured in this project, deleting 73
conversations took the reported total from about $0.84 to about $0.39 with no
money coming back. A session's totals are therefore folded in here *before* it
is removed, so a lifetime figure cannot silently fall.

Only token counts per model are kept -- never conversation content, prompts or
private reasoning.
"""

from __future__ import annotations

import json
from pathlib import Path

from agentrt.sdk.utils.files import atomic_write_text


SPEND_ARCHIVE_NAME = "spend_archive.json"
_TOKEN_FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "reasoning_tokens",
)
ARCHIVE_VERSION = 1


def archive_path(conversations_dir: str | Path) -> Path:
    """Where the ledger lives: beside the state directory, not inside it."""
    return Path(conversations_dir).parent / SPEND_ARCHIVE_NAME


def _empty() -> dict:
    return {
        "version": ARCHIVE_VERSION,
        "sessions": 0,
        "by_model": {},
        "updated_at": None,
    }


def read_archive(conversations_dir: str | Path) -> dict:
    """The ledger as it stands. An unreadable file reads as empty, not as an error."""
    path = archive_path(conversations_dir)
    if not path.is_file():
        return _empty()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    data.setdefault("version", ARCHIVE_VERSION)
    data.setdefault("sessions", 0)
    data.setdefault("by_model", {})
    data.setdefault("updated_at", None)
    return data


def usage_by_model(stats) -> dict[str, dict[str, int]]:
    """Per-model token totals from a conversation's persisted stats."""
    totals: dict[str, dict[str, int]] = {}
    services = getattr(stats, "usage_to_metrics", None) or {}
    for metrics in services.values():
        for usage in getattr(metrics, "token_usages", None) or []:
            model = getattr(usage, "model", None) or "unknown"
            bucket = totals.setdefault(model, dict.fromkeys(_TOKEN_FIELDS, 0))
            for field in _TOKEN_FIELDS:
                bucket[field] += int(getattr(usage, field, 0) or 0)
    return totals


def fold_conversation(conversations_dir: str | Path, stats, *, at: str) -> dict:
    """Fold one conversation's totals in before its directory is removed."""
    archive = read_archive(conversations_dir)
    for model, totals in usage_by_model(stats).items():
        bucket = archive["by_model"].setdefault(model, dict.fromkeys(_TOKEN_FIELDS, 0))
        for field in _TOKEN_FIELDS:
            bucket[field] = int(bucket.get(field, 0)) + totals[field]
    archive["sessions"] = int(archive.get("sessions", 0)) + 1
    archive["updated_at"] = at
    atomic_write_text(archive_path(conversations_dir), json.dumps(archive, indent=2))
    return archive


def purge(conversations_dir: str | Path) -> bool:
    """Remove the ledger deliberately. Deletion of a session is not this."""
    path = archive_path(conversations_dir)
    if not path.exists():
        return False
    path.unlink()
    return True
