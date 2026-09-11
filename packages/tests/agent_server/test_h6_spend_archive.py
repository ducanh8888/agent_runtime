"""H6: the lifetime ledger that session deletion does not reach."""

from __future__ import annotations

import json
from types import SimpleNamespace

from agentrt.agent_server.spend_archive import (
    archive_path,
    fold_conversation,
    purge,
    read_archive,
    record_unarchived,
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

    fold_conversation(
        conversations,
        _stats(("m", 10, 2)),
        at="2026-01-01T00:00:00Z",
        conversation_id="a",
    )
    archive = fold_conversation(
        conversations,
        _stats(("m", 5, 1)),
        at="2026-01-02T00:00:00Z",
        conversation_id="b",
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
    fold_conversation(conversations, _stats(("m", 1, 1)), at="t", conversation_id="c")

    assert purge(conversations) is True
    assert purge(conversations) is False
    assert read_archive(conversations)["sessions"] == 0


def test_folding_the_same_conversation_twice_counts_once(tmp_path) -> None:
    """A delete whose directory removal failed must not double-count."""
    conversations = tmp_path / "conversations"
    conversations.mkdir()

    fold_conversation(conversations, _stats(("m", 10, 2)), at="t", conversation_id="x")
    archive = fold_conversation(
        conversations, _stats(("m", 10, 2)), at="t2", conversation_id="x"
    )

    assert archive["sessions"] == 1
    assert archive["by_model"]["m"]["prompt_tokens"] == 10


def test_the_ledger_keeps_the_full_field_names(tmp_path) -> None:
    """The report prices from these names; short ones priced as zero."""
    conversations = tmp_path / "conversations"
    conversations.mkdir()

    archive = fold_conversation(
        conversations, _stats(("deepseek-flash", 1000, 20)), at="t", conversation_id="y"
    )

    bucket = archive["by_model"]["deepseek-flash"]
    assert bucket["prompt_tokens"] == 1000
    assert bucket["completion_tokens"] == 20


def test_an_unarchivable_delete_is_recorded_not_hidden(tmp_path) -> None:
    """The gap is written into the ledger, so a short total says so."""
    conversations = tmp_path / "conversations"
    conversations.mkdir()

    archive = record_unarchived(
        conversations, conversation_id="z", reason="RuntimeError", at="t"
    )

    assert archive["unarchived"] == [
        {"conversation_id": "z", "reason": "RuntimeError", "at": "t"}
    ]
    assert read_archive(conversations)["unarchived"][0]["conversation_id"] == "z"

