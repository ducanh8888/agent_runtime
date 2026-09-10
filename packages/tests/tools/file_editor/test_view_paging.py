"""Deterministic tests for the H1 contiguous paged file-view contract."""

import base64
import json
import re
from pathlib import Path

from agentrt.tools.file_editor import file_editor


_NUMBERED_LINE = re.compile(r"^\s*(\d+)\t(.*)$")


def _numbered_lines(result):
    """Parse the ``cat -n`` body of a view observation into (line, content)."""
    _, _, body = result.text.partition("\n")
    pairs = []
    for line in body.split("\n"):
        match = _NUMBERED_LINE.match(line)
        if match:
            pairs.append((int(match.group(1)), match.group(2)))
    return pairs


def _collect_pages(path, **kwargs):
    """Follow the continuation cursor until the file is exhausted."""
    pages = []
    result = file_editor(command="view", path=str(path), **kwargs)
    while True:
        pages.append(result)
        if result.cursor is None:
            return pages
        assert len(pages) < 100, "cursor did not terminate"
        result = file_editor(command="view", path=str(path), cursor=result.cursor)


def _write_lines(path: Path, prefix: str, count: int) -> list[str]:
    expected = [f"{prefix}{i}" for i in range(1, count + 1)]
    path.write_text("".join(f"{line}\n" for line in expected))
    return expected


def test_paged_view_reconstructs_long_file(tmp_path):
    path = tmp_path / "big.txt"
    expected = _write_lines(path, "line ", 1200)

    pages = _collect_pages(path)

    assert [page.view_status for page in pages] == ["truncated", "truncated", "eof"]
    for page in pages:
        assert page.requested_range.start_line == 1
        assert page.requested_range.end_line == 1200
        assert page.line_count == 1200
        assert page.encoding == "utf-8"
        assert page.newline == "\n"
        assert page.has_final_newline is True
        assert page.file_hash

    ranges = [
        (page.returned_range.start_line, page.returned_range.end_line) for page in pages
    ]
    assert ranges == [(1, 500), (501, 1000), (1001, 1200)]
    assert pages[0].eof is False
    assert pages[0].truncated is True
    assert pages[-1].eof is True
    assert pages[-1].truncated is False

    # Numbers stay attached to their source line across the whole file.
    recovered = [content for page in pages for _, content in _numbered_lines(page)]
    assert recovered == expected
    assert _numbered_lines(pages[0])[-1] == (500, "line 500")
    assert _numbered_lines(pages[1])[0] == (501, "line 501")


def test_small_file_single_page_has_no_phantom_line(tmp_path):
    path = tmp_path / "small.txt"
    path.write_text("a\nb\n")

    result = file_editor(command="view", path=str(path))

    assert result.view_status == "eof"
    assert result.eof is True and result.truncated is False
    assert result.cursor is None
    assert result.line_count == 2
    assert _numbered_lines(result) == [(1, "a"), (2, "b")]


def test_long_single_line_continuation_always_advances(tmp_path):
    path = tmp_path / "long.txt"
    path.write_text("A" * 40000 + "\n")

    pages = _collect_pages(path)

    assert [page.partial_line for page in pages] == [True, True, False]
    starts = [page.returned_range.start_char for page in pages]
    assert starts == [0, 16000, 32000]
    recovered = "".join(
        content for page in pages for _, content in _numbered_lines(page)
    )
    assert recovered == "A" * 40000

    assert pages[0].returned_range.end_char == 16000
    assert pages[1].returned_range.start_char == 16000
    assert "partial line" in pages[0].text
    assert "continues at character 16000" in pages[0].text
    assert pages[-1].eof is True


def test_partial_line_offsets_are_characters_not_bytes(tmp_path):
    path = tmp_path / "unicode_long.txt"
    content = "hello caf\u00e9 " * 2000
    path.write_text(content + "\n")
    assert path.stat().st_size > len(content)  # non-ASCII bytes

    pages = _collect_pages(path)

    starts = [page.returned_range.start_char for page in pages]
    assert starts == [0, 16000]
    assert all(later > earlier for earlier, later in zip(starts, starts[1:]))
    recovered = "".join(
        content for page in pages for _, content in _numbered_lines(page)
    )
    assert recovered == content


def test_unicode_line_numbers_and_reconstruction(tmp_path):
    path = tmp_path / "unicode.txt"
    expected = ["h\u00e9llo", "\u4e16\u754c", "na\u00efve \u2713", "\U0001d518nicode"]
    path.write_text("".join(f"{line}\n" for line in expected))

    result = file_editor(command="view", path=str(path))

    assert result.encoding == "utf-8"
    assert result.line_count == len(expected)
    assert _numbered_lines(result) == list(enumerate(expected, start=1))


def test_crlf_metadata_and_numbering(tmp_path):
    path = tmp_path / "crlf.txt"
    path.write_bytes(b"alpha\r\nbeta\r\ngamma\r\n")

    result = file_editor(command="view", path=str(path))

    assert result.newline == "\r\n"
    assert result.has_final_newline is True
    assert result.line_count == 3
    assert _numbered_lines(result) == [(1, "alpha"), (2, "beta"), (3, "gamma")]


def test_missing_final_newline_counts_last_line(tmp_path):
    path = tmp_path / "no_final_newline.txt"
    path.write_bytes(b"alpha\r\nbeta")

    result = file_editor(command="view", path=str(path))

    assert result.newline == "\r\n"
    assert result.has_final_newline is False
    assert result.line_count == 2
    assert _numbered_lines(result) == [(1, "alpha"), (2, "beta")]


def test_empty_file_is_explicit(tmp_path):
    path = tmp_path / "empty.txt"
    path.write_text("")

    result = file_editor(command="view", path=str(path))

    assert result.is_error is False
    assert result.view_status == "empty"
    assert result.eof is True and result.truncated is False
    assert result.line_count == 0
    assert result.file_size == 0
    assert result.returned_range is None
    assert "empty" in result.text.lower()
    # Backwards compatible with the historical empty-line rendering.
    assert "1\t" in result.text


def test_binary_file_is_typed(tmp_path):
    path = tmp_path / "binary.bin"
    path.write_bytes(b"text\x00more\x00")

    result = file_editor(command="view", path=str(path))

    assert result.is_error is True
    assert result.view_status == "binary"
    assert "binary" in result.text.lower()


def test_decode_error_is_typed(tmp_path):
    path = tmp_path / "latin.txt"
    path.write_bytes(b"caf\xe9 latte\n")

    result = file_editor(command="view", path=str(path))

    assert result.is_error is True
    assert result.view_status == "decode_error"
    assert "cannot be decoded" in result.text.lower()


def test_out_of_range_view_range_is_typed(tmp_path):
    path = tmp_path / "five.txt"
    path.write_text("1\n2\n3\n4\n5\n")

    result = file_editor(command="view", path=str(path), view_range=[9, 10])

    assert result.is_error is True
    assert result.view_status == "out_of_range"
    assert "within the range" in result.text

    result = file_editor(command="view", path=str(path), view_range=[1])
    assert result.is_error is True and "two integers" in result.text

    result = file_editor(command="view", path=str(path), view_range=[3, 1])
    assert result.is_error is True and "greater than or equal" in result.text


def test_view_range_compatibility(tmp_path):
    path = tmp_path / "range.txt"
    _write_lines(path, "L", 10)

    result = file_editor(command="view", path=str(path), view_range=[3, 5])
    assert _numbered_lines(result) == [(3, "L3"), (4, "L4"), (5, "L5")]
    assert result.returned_range is not None
    assert result.returned_range.start_line == 3
    assert result.returned_range.end_line == 5
    assert result.truncated is False
    assert result.cursor is None

    result = file_editor(command="view", path=str(path), view_range=[8, -1])
    assert _numbered_lines(result) == [(8, "L8"), (9, "L9"), (10, "L10")]

    result = file_editor(command="view", path=str(path), view_range=[1, 99])
    assert result.is_error is False
    assert (
        "NOTE: We only show up to 10 since there're only 10 lines in this file."
        in result.text
    )


def test_cursor_invalidated_by_mutation(tmp_path):
    path = tmp_path / "mutating.txt"
    _write_lines(path, "L", 1200)
    first = file_editor(command="view", path=str(path))
    assert first.cursor is not None

    path.write_text("changed\n")

    result = file_editor(command="view", path=str(path), cursor=first.cursor)
    assert result.is_error is True
    assert result.view_status == "file_changed"
    assert result.file_changed is True
    assert result.cursor is None


def test_stale_cursor_for_other_file_is_rejected(tmp_path):
    first_path = tmp_path / "first.txt"
    second_path = tmp_path / "second.txt"
    _write_lines(first_path, "L", 1200)
    _write_lines(second_path, "X", 1200)

    cursor = file_editor(command="view", path=str(first_path)).cursor
    assert cursor is not None

    result = file_editor(command="view", path=str(second_path), cursor=cursor)
    assert result.is_error is True
    assert result.view_status == "invalid_cursor"


def test_tampered_cursor_is_rejected(tmp_path):
    path = tmp_path / "tampered.txt"
    _write_lines(path, "L", 1200)
    cursor = file_editor(command="view", path=str(path)).cursor
    assert cursor is not None

    corrupted = cursor[:-2] + ("AA" if cursor[-2:] != "AA" else "BB")
    result = file_editor(command="view", path=str(path), cursor=corrupted)
    assert result.is_error is True
    assert result.view_status == "invalid_cursor"

    padded = cursor + "=" * (-len(cursor) % 4)
    payload = json.loads(base64.urlsafe_b64decode(padded))
    payload["line"] = 1
    forged = (
        base64.urlsafe_b64encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        )
        .decode()
        .rstrip("=")
    )
    result = file_editor(command="view", path=str(path), cursor=forged)
    assert result.is_error is True
    assert result.view_status == "invalid_cursor"


def test_cursor_page_size_is_bound(tmp_path):
    path = tmp_path / "paged.txt"
    _write_lines(path, "L", 1200)

    first = file_editor(command="view", path=str(path), max_lines=10)
    assert _numbered_lines(first)[-1][0] == 10
    assert first.cursor is not None

    mismatched = file_editor(
        command="view", path=str(path), cursor=first.cursor, max_lines=20
    )
    assert mismatched.is_error is True
    assert mismatched.view_status == "invalid_cursor"

    continued = file_editor(
        command="view", path=str(path), cursor=first.cursor, max_lines=10
    )
    assert continued.is_error is False
    assert _numbered_lines(continued)[0][0] == 11


def test_cursor_cannot_be_combined_with_view_range(tmp_path):
    path = tmp_path / "conflict.txt"
    _write_lines(path, "L", 1200)
    cursor = file_editor(command="view", path=str(path)).cursor
    assert cursor is not None

    result = file_editor(
        command="view", path=str(path), cursor=cursor, view_range=[1, 5]
    )
    assert result.is_error is True
    assert result.view_status == "invalid_cursor"


def test_cursor_continues_within_requested_range(tmp_path):
    path = tmp_path / "ranged.txt"
    _write_lines(path, "L", 25)

    first = file_editor(command="view", path=str(path), view_range=[5, 12], max_lines=4)
    assert _numbered_lines(first) == [(5, "L5"), (6, "L6"), (7, "L7"), (8, "L8")]
    assert first.cursor is not None
    assert first.requested_range is not None
    assert first.requested_range.start_line == 5
    assert first.requested_range.end_line == 12

    second = _collect_pages(path, cursor=first.cursor)[0]
    assert _numbered_lines(second) == [(9, "L9"), (10, "L10"), (11, "L11"), (12, "L12")]
    assert second.requested_range is not None
    assert second.requested_range.start_line == 5
    assert second.requested_range.end_line == 12
    assert second.eof is False
    assert second.cursor is None


def test_directory_view_is_typed(tmp_path):
    (tmp_path / "child.txt").write_text("x")

    result = file_editor(command="view", path=str(tmp_path))

    assert result.is_error is False
    assert result.view_status == "directory"
