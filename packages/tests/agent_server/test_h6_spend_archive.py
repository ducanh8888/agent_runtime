"""H6: the lifetime ledger that session deletion does not reach."""

from __future__ import annotations

import json
from types import SimpleNamespace

from agentrt.agent_server.spend_archive import (
    archive_path,
    fold_conversation,
    purge,
    read_archive,
    usage_by_model,
)


def _stats(*calls) -> SimpleNamespace:
    """A stats object shaped like the persisted one."""
    return SimpleNamespace(
        usage_to_metrics={
            "worker": SimpleNamespace(
                token_usages=[
                    SimpleNamespace(model=model, prompt_tokens=p, completion_tokens=c)
                    for model, p, c in calls
                ]
            )
        }
    )


def test_usage_is_grouped_by_model(tmp_path) -> None:
    totals = usage_by_model(_stats(("deepseek-flash", 10, 2), ("deepseek-flash", 5, 1)))

    assert totals["deepseek-flash"]["prompt_tokens"] == 15
    assert totals["deepseek-flash"]["completion_tokens"] == 3


def test_folding_accumulates_across_deletions(tmp_path) -> None:
    conversations = tmp_path / "conversations"
    conversations.mkdir()

    fold_conversation(conversations, _stats(("m", 10, 2)), at="2026-01-01T00:00:00Z")
    archive = fold_conversation(
        conversations, _stats(("m", 5, 1)), at="2026-01-02T00:00:00Z"
    )

    assert archive["sessions"] == 2
    assert archive["by_model"]["m"]["prompt_tokens"] == 15
    assert archive["updated_at"] == "2026-01-02T00:00:00Z"
    # Durable: it is on disk, not in the process.
    assert json.loads(archive_path(conversations).read_text())["sessions"] == 2


def test_an_unreadable_archive_reads_as_empty(tmp_path) -> None:
    conversations = tmp_path / "conversations"
    conversations.mkdir()
    archive_path(conversations).write_text("{not json")

    assert read_archive(conversations)["sessions"] == 0


def test_purge_is_explicit_and_idempotent(tmp_path) -> None:
    conversations = tmp_path / "conversations"
    conversations.mkdir()
    fold_conversation(conversations, _stats(("m", 1, 1)), at="t")

    assert purge(conversations) is True
    assert purge(conversations) is False
    assert read_archive(conversations)["sessions"] == 0
