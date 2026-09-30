"""Server-side fixes from the 2026-09-21/25 orchestrator feedback."""

from __future__ import annotations

from datetime import datetime, timedelta

from agentrt.agent_server.event_service import _progress_age_seconds


def test_progress_age_reads_naive_event_timestamp_as_local_time() -> None:
    """Event.timestamp is naive local time; a 10-minute-old event must read
    ~600s regardless of the host's UTC offset (it read 0.0 on GMT+7)."""
    ten_minutes_ago = (datetime.now() - timedelta(minutes=10)).isoformat()
    age = _progress_age_seconds(ten_minutes_ago)
    assert age is not None
    assert 590 <= age <= 610


def test_progress_age_still_honours_explicit_offsets() -> None:
    aware = (datetime.now().astimezone() - timedelta(seconds=30)).isoformat()
    age = _progress_age_seconds(aware)
    assert age is not None
    assert 25 <= age <= 40
