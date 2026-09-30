"""Tests for the optional SDK startup banner."""

import pytest

from agentrt.sdk.banner import _print_banner


@pytest.fixture
def reset_banner_state(monkeypatch):
    import agentrt.sdk.banner as banner_module

    monkeypatch.delenv("AGENTRT_SHOW_BANNER", raising=False)
    original_state = banner_module._BANNER_PRINTED
    banner_module._BANNER_PRINTED = False
    yield
    banner_module._BANNER_PRINTED = original_state


def test_banner_is_off_by_default(reset_banner_state, capsys):
    _print_banner("1.0.0")
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out == ""


def test_banner_prints_only_when_enabled(reset_banner_state, monkeypatch, capsys):
    monkeypatch.setenv("AGENTRT_SHOW_BANNER", "1")
    _print_banner("1.0.0")
    _print_banner("1.0.0")
    captured = capsys.readouterr()
    assert captured.err.count("AgentRT SDK v1.0.0") == 1
    assert "openhands.dev" not in captured.err
    assert "all-hands.dev" not in captured.err
    assert captured.out == ""


@pytest.mark.parametrize("value", ["true", "yes", "0"])
def test_banner_requires_exact_one(reset_banner_state, monkeypatch, capsys, value):
    monkeypatch.setenv("AGENTRT_SHOW_BANNER", value)
    _print_banner("1.0.0")
    assert capsys.readouterr().err == ""
