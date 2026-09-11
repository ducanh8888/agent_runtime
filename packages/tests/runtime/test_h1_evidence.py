"""H1: read-evidence projection and typed artifact listings.

The client is exercised against a MockTransport, so every request and response
is deterministic and no daemon is started.
"""

from __future__ import annotations

import os
import types
from collections.abc import Callable

import httpx
import pytest

from agentrt.runtime import client as client_mod
from agentrt.tools.file_editor.definition import FileEditorObservation
from agentrt.tools.file_editor.view_contract import FileRange


SESSION = "11111111-1111-1111-1111-111111111111"
Handler = Callable[[httpx.Request], httpx.Response]


def _mock_client(handler: Handler) -> client_mod.Client:
    """A Client whose HTTP surface is a deterministic MockTransport."""
    client = client_mod.Client()
    # setattr because `_daemon_info` is only ever assigned by the info property.
    setattr(
        client,
        "_daemon_info",
        types.SimpleNamespace(base_url="http://daemon.test", token="test-token"),
    )
    client._profile_ref = {}
    client._http = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def _conversation_payload(
    workspace: str | None,
    *,
    created_at: str | None = "2026-01-01T00:00:00Z",
) -> dict:
    payload: dict = {
        "id": SESSION,
        "execution_status": "finished",
        "tags": {},
    }
    if workspace is not None:
        payload["workspace"] = {"working_dir": workspace}
    if created_at is not None:
        payload["created_at"] = created_at
    return payload


def _make_handler(
    *,
    workspace: str | None,
    pages: dict | None = None,
    created_at: str | None = "2026-01-01T00:00:00Z",
) -> Handler:
    def handle(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == f"/api/conversations/{SESSION}":
            return httpx.Response(
                200, json=_conversation_payload(workspace, created_at=created_at)
            )
        if path == f"/api/conversations/{SESSION}/events/search":
            cursor = request.url.params.get("page_id")
            items, next_page = (pages or {}).get(cursor, ([], None))
            return httpx.Response(200, json={"items": items, "next_page_id": next_page})
        if path.startswith(f"/api/conversations/{SESSION}/workspace/"):
            return httpx.Response(200, text="file body")
        return httpx.Response(404, json={"detail": "not found"})

    return handle


def _obs(
    event_id: str,
    *,
    timestamp: str,
    path: str = "/ws/a.py",
    command: str = "view",
    is_error: bool = False,
    tool: str = "file_editor",
    extra: dict | None = None,
) -> dict:
    observation: dict = {
        "command": command,
        "path": path,
        "is_error": is_error,
        "content": [{"type": "text", "text": "body"}],
    }
    if extra:
        observation.update(extra)
    return {
        "kind": "ObservationEvent",
        "id": event_id,
        "timestamp": timestamp,
        "tool_name": tool,
        "tool_call_id": f"call-{event_id}",
        "observation": observation,
    }


# ---------------------------------------------------------------------------
# read evidence
# ---------------------------------------------------------------------------


def test_plain_view_projects_without_metadata() -> None:
    pages = {
        None: (
            [
                _obs("e1", timestamp="2026-01-01T00:00:01"),
                _obs("e2", timestamp="2026-01-01T00:00:02"),
            ],
            None,
        )
    }
    client = _mock_client(_make_handler(workspace="/ws", pages=pages))

    out = client.read_evidence(SESSION)

    assert out["events_scanned"] == 2
    assert out["complete"] is True
    assert out["next_cursor"] is None
    assert len(out["reads"]) == 2
    for read in out["reads"]:
        assert read["version"] is None
        assert read["version_known"] is False
        assert read["returned"]["lines"] is None
        assert read["eof"] is None  # absent in the source event
        assert read["stages"] == {
            "observed": "confirmed",
            "delivered": "unknown",
            "understood": "unknown",
        }
    # Unknown versions are never merged with one another.
    assert len(out["file_versions"]) == 2
    assert all(not group["eof_confirmed"] for group in out["file_versions"])
    assert all(group["merged_lines"] == [] for group in out["file_versions"])
    # The projection states its assumptions and what the event owner must add.
    assert out["assumptions"]
    assert out["root_integration"]


def test_merges_ranges_only_for_same_version() -> None:
    pages = {
        None: (
            [
                _obs(
                    "e1",
                    timestamp="2026-01-01T00:00:01",
                    extra={"file_version": "v1", "returned_lines": [1, 10]},
                ),
                _obs(
                    "e2",
                    timestamp="2026-01-01T00:00:02",
                    extra={"file_version": "v1", "returned_lines": [11, 20]},
                ),
                _obs(
                    "e3",
                    timestamp="2026-01-01T00:00:03",
                    extra={"file_version": "v2", "returned_lines": [1, 5]},
                ),
            ],
            None,
        )
    }
    client = _mock_client(_make_handler(workspace="/ws", pages=pages))

    out = client.read_evidence(SESSION)

    by_version = {group["version"]: group for group in out["file_versions"]}
    assert by_version["v1"]["read_count"] == 2
    assert by_version["v1"]["merged_lines"] == [[1, 20]]
    assert by_version["v2"]["read_count"] == 1
    assert by_version["v2"]["merged_lines"] == [[1, 5]]


def test_reports_repeated_identical_reads() -> None:
    pages = {
        None: (
            [
                _obs(
                    "e1",
                    timestamp="2026-01-01T00:00:01",
                    extra={
                        "file_version": "v1",
                        "requested_lines": [1, 50],
                        "returned_lines": [1, 50],
                    },
                ),
                _obs(
                    "e2",
                    timestamp="2026-01-01T00:00:02",
                    extra={
                        "file_version": "v1",
                        "requested_lines": [1, 50],
                        "returned_lines": [1, 50],
                    },
                ),
            ],
            None,
        )
    }
    client = _mock_client(_make_handler(workspace="/ws", pages=pages))

    out = client.read_evidence(SESSION)

    assert len(out["repeated_reads"]) == 1
    repeated = out["repeated_reads"][0]
    assert repeated["count"] == 2
    assert repeated["event_ids"] == ["e1", "e2"]
    assert repeated["version_known"] is True


def test_never_invents_eof_or_ranges() -> None:
    pages = {
        None: (
            [
                _obs(
                    "e1",
                    timestamp="2026-01-01T00:00:01",
                    extra={"file_version": "v1"},
                )
            ],
            None,
        )
    }
    client = _mock_client(_make_handler(workspace="/ws", pages=pages))

    out = client.read_evidence(SESSION)

    read = out["reads"][0]
    assert read["eof"] is None
    assert read["truncated"] is None
    assert read["returned"] == {"lines": None, "chars": None}
    group = out["file_versions"][0]
    assert group["eof_confirmed"] is False
    assert group["merged_lines"] == []
    assert group["merged_chars"] == []


def test_reports_eof_and_truncation_when_present() -> None:
    pages = {
        None: (
            [
                _obs(
                    "e1",
                    timestamp="2026-01-01T00:00:01",
                    extra={
                        "file_version": "v9",
                        "returned_lines": [1, 10],
                        "eof": True,
                        "truncated": True,
                    },
                )
            ],
            None,
        )
    }
    client = _mock_client(_make_handler(workspace="/ws", pages=pages))

    out = client.read_evidence(SESSION)

    read = out["reads"][0]
    assert read["eof"] is True
    assert read["truncated"] is True
    group = out["file_versions"][0]
    assert group["eof_confirmed"] is True
    assert group["truncation_observed"] is True
    assert group["merged_lines"] == [[1, 10]]


def test_pages_all_events_and_respects_page_bound() -> None:
    page_one = [
        _obs("e1", timestamp="2026-01-01T00:00:01"),
        _obs("e2", timestamp="2026-01-01T00:00:02"),
    ]
    pages = {None: (page_one, "page-2"), "page-2": ([_obs("e3", timestamp="x")], None)}
    client = _mock_client(_make_handler(workspace="/ws", pages=pages))

    out = client.read_evidence(SESSION, limit=2)

    assert out["pages"] == 2
    assert out["events_scanned"] == 3
    assert out["complete"] is True

    bounded = _mock_client(
        _make_handler(
            workspace="/ws",
            pages={None: (page_one, "page-2"), "page-2": ([], "page-3")},
        )
    ).read_evidence(SESSION, limit=2, max_pages=1)

    assert bounded["pages"] == 1
    assert bounded["complete"] is False
    assert bounded["next_cursor"] == "page-2"


def test_ignores_errors_non_view_and_other_tools() -> None:
    pages = {
        None: (
            [
                _obs("err", timestamp="2026-01-01T00:00:01", is_error=True),
                _obs("edit", timestamp="2026-01-01T00:00:02", command="str_replace"),
                _obs("term", timestamp="2026-01-01T00:00:03", tool="terminal"),
                _obs("ok", timestamp="2026-01-01T00:00:04"),
            ],
            None,
        )
    }
    client = _mock_client(_make_handler(workspace="/ws", pages=pages))

    out = client.read_evidence(SESSION)

    assert [read["event_id"] for read in out["reads"]] == ["ok"]


def test_delivered_and_understood_are_distinct_stages() -> None:
    pages = {
        None: (
            [
                _obs(
                    "e1",
                    timestamp="2026-01-01T00:00:01",
                    extra={"delivered": True},
                ),
                _obs(
                    "e2",
                    timestamp="2026-01-01T00:00:02",
                    extra={"understood": False},
                ),
            ],
            None,
        )
    }
    client = _mock_client(_make_handler(workspace="/ws", pages=pages))

    out = client.read_evidence(SESSION)

    by_id = {read["event_id"]: read for read in out["reads"]}
    assert by_id["e1"]["stages"]["delivered"] == "confirmed"
    assert by_id["e1"]["stages"]["understood"] == "unknown"
    assert by_id["e2"]["stages"]["delivered"] == "unknown"
    assert by_id["e2"]["stages"]["understood"] == "denied"


def test_character_ranges_and_open_ended_lines() -> None:
    pages = {
        None: (
            [
                _obs(
                    "e1",
                    timestamp="2026-01-01T00:00:01",
                    extra={
                        "file_version": "v1",
                        "returned_lines": [5, -1],
                        "returned_chars": {"start": 0, "length": 100},
                    },
                )
            ],
            None,
        )
    }
    client = _mock_client(_make_handler(workspace="/ws", pages=pages))

    out = client.read_evidence(SESSION)

    read = out["reads"][0]
    assert read["returned"]["lines"] == {"start": 5, "end": None, "open_end": True}
    assert read["returned"]["chars"] == {"start": 0, "end": 100}
    group = out["file_versions"][0]
    assert group["open_ended_lines"] == [{"start": 5, "end": None, "open_end": True}]
    assert group["merged_lines"] == []


def test_recognizes_nested_metadata_and_path_relative_to_workspace() -> None:
    pages = {
        None: (
            [
                _obs(
                    "e1",
                    timestamp="2026-01-01T00:00:01",
                    path="/ws/src/a.py",
                    extra={
                        "metadata": {
                            "file_version": "nested",
                            "returned_lines": [1, 2],
                        }
                    },
                )
            ],
            None,
        )
    }
    client = _mock_client(_make_handler(workspace="/ws", pages=pages))

    out = client.read_evidence(SESSION)

    read = out["reads"][0]
    assert read["version"] == "nested"
    assert read["path_relative"] == "src/a.py"
    assert read["returned"]["lines"] == {"start": 1, "end": 2}


def test_projects_and_merges_the_actual_file_editor_paging_schema() -> None:
    events = []
    for index, (start, end, eof) in enumerate(
        ((1, 500, False), (501, 1000, False), (1001, 1200, True)), start=1
    ):
        observation = FileEditorObservation.from_text(
            text="page",
            command="view",
            path="/ws/src/exact.py",
            requested_range=FileRange(start_line=1, end_line=1200),
            returned_range=FileRange(start_line=start, end_line=end),
            page_content=f"line {start}\n",
            file_hash="sha256-exact",
            eof=eof,
            truncated=not eof,
        ).model_dump(mode="json", exclude_none=True)
        events.append(
            {
                "kind": "ObservationEvent",
                "id": f"exact-{index}",
                "timestamp": f"2026-01-01T00:00:0{index}",
                "tool_name": "file_editor",
                "tool_call_id": f"call-exact-{index}",
                "observation": observation,
            }
        )
    client = _mock_client(_make_handler(workspace="/ws", pages={None: (events, None)}))

    out = client.read_evidence(SESSION)

    read = out["reads"][0]
    assert read["version"] == "sha256-exact"
    assert read["requested"] == {
        "lines": {"start": 1, "end": 1200},
        "chars": {"start": 0, "end": None},
    }
    assert read["returned"] == {
        "lines": {"start": 1, "end": 500},
        "chars": {"start": 0, "end": None},
    }
    assert read["eof"] is False
    assert read["truncated"] is True
    assert "page_content" not in read
    assert len(read["metadata_keys"]) == len(set(read["metadata_keys"]))
    assert len(out["file_versions"]) == 1
    assert out["file_versions"][0]["merged_lines"] == [[1, 1200]]


@pytest.mark.parametrize("view_status", ["directory", "image"])
def test_non_text_views_are_not_projected_as_file_reads(view_status: str) -> None:
    pages = {
        None: (
            [
                _obs(
                    "non-text",
                    timestamp="2026-01-01T00:00:01",
                    extra={"view_status": view_status},
                )
            ],
            None,
        )
    }

    out = _mock_client(_make_handler(workspace="/ws", pages=pages)).read_evidence(
        SESSION
    )

    assert out["reads"] == []


def test_malformed_first_event_page_is_incomplete() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/api/conversations/{SESSION}":
            return httpx.Response(200, json=_conversation_payload("/ws"))
        if request.url.path.endswith("/events/search"):
            return httpx.Response(200, json={"unexpected": True})
        return httpx.Response(404)

    out = _mock_client(handler).read_evidence(SESSION)

    assert out["complete"] is False
    assert out["pages"] == 0


# ---------------------------------------------------------------------------
# artifacts listing outcome
# ---------------------------------------------------------------------------


def test_artifacts_empty_is_a_positive_outcome(tmp_path) -> None:
    client = _mock_client(_make_handler(workspace=str(tmp_path)))

    out = client.artifacts(SESSION)

    assert out["outcome"] == client_mod.ARTIFACTS_EMPTY
    assert out["empty"] is True
    assert out["complete"] is True
    assert out["files"] == []
    assert out["filtered"] is True
    # Legacy fields are preserved.
    for key in (
        "workspace",
        "since",
        "filtered",
        "files",
        "truncated",
        "total_scanned",
        "pruned",
    ):
        assert key in out


def test_artifacts_empty_differs_from_unavailable(tmp_path) -> None:
    empty = _mock_client(_make_handler(workspace=str(tmp_path))).artifacts(SESSION)
    unavailable = _mock_client(_make_handler(workspace=None)).artifacts(SESSION)

    assert empty["outcome"] == client_mod.ARTIFACTS_EMPTY
    assert unavailable["outcome"] == client_mod.ARTIFACTS_UNAVAILABLE
    assert unavailable["empty"] is False
    assert unavailable["workspace"] is None


def test_artifacts_unavailable_when_workspace_dir_missing(tmp_path) -> None:
    missing = str(tmp_path / "does-not-exist")
    client = _mock_client(_make_handler(workspace=missing))

    out = client.artifacts(SESSION)

    assert out["outcome"] == client_mod.ARTIFACTS_UNAVAILABLE
    assert out["files"] == []


def test_artifacts_unfiltered_without_start_time(tmp_path) -> None:
    (tmp_path / "written.txt").write_text("x", encoding="utf-8")
    client = _mock_client(_make_handler(workspace=str(tmp_path), created_at=None))

    out = client.artifacts(SESSION)

    assert out["filtered"] is False
    assert out["outcome"] == client_mod.ARTIFACTS_UNFILTERED
    assert out["complete"] is False
    assert [entry["path"] for entry in out["files"]] == ["written.txt"]


def test_artifacts_listed_and_truncated(tmp_path) -> None:
    (tmp_path / "one.txt").write_text("x", encoding="utf-8")
    listed = _mock_client(_make_handler(workspace=str(tmp_path))).artifacts(SESSION)
    assert listed["outcome"] == client_mod.ARTIFACTS_LISTED
    assert listed["truncated"] is False

    for index in range(client_mod._ARTIFACTS_LIMIT + 1):
        (tmp_path / f"f{index:03d}.txt").write_text("x", encoding="utf-8")
    truncated = _mock_client(_make_handler(workspace=str(tmp_path))).artifacts(SESSION)
    assert truncated["outcome"] == client_mod.ARTIFACTS_TRUNCATED
    assert truncated["truncated"] is True
    assert len(truncated["files"]) == client_mod._ARTIFACTS_LIMIT


def test_artifacts_path_read_still_guarded(tmp_path) -> None:
    (tmp_path / "hello.txt").write_text("file body", encoding="utf-8")
    client = _mock_client(_make_handler(workspace=str(tmp_path)))

    assert client.artifacts(SESSION, path="hello.txt")["content"] == "file body"

    absolute = os.path.join(os.path.abspath(os.sep), "etc", "passwd")
    with pytest.raises(client_mod.ClientError):
        client.artifacts(SESSION, path=absolute)
    with pytest.raises(client_mod.ClientError):
        client.artifacts(SESSION, path=os.path.join("..", "escape.txt"))
