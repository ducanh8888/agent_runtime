"""`agentrt mcp add --header` accepts the forms people actually type."""

from __future__ import annotations

import pytest

from agentrt.runtime import cli, client as client_mod


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Authorization: Bearer abc==", {"Authorization": "Bearer abc=="}),
        ("Authorization:Bearer a:b", {"Authorization": "Bearer a:b"}),
        ("Authorization=Bearer abc==", {"Authorization": "Bearer abc=="}),
        ("X-Api-Key=k:v", {"X-Api-Key": "k:v"}),
    ],
)
def test_header_forms(raw: str, expected: dict[str, str]) -> None:
    assert cli._header_pairs([raw]) == expected


def test_header_value_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("TOK", "Bearer secret")
    assert cli._header_pairs(["Authorization: env:TOK"]) == {
        "Authorization": "Bearer secret"
    }
    assert cli._header_pairs(["Authorization=env:TOK"]) == {
        "Authorization": "Bearer secret"
    }


@pytest.mark.parametrize("raw", ["Bearer abc", ": x", "=x", "Bad Name: x"])
def test_malformed_header_is_refused(raw: str) -> None:
    with pytest.raises(client_mod.ClientError):
        cli._header_pairs([raw])
