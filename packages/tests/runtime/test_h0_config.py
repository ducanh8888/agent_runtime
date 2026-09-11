"""H0: neutral config aliases, per-layer conflicts and redacted provenance."""

from __future__ import annotations

import importlib.metadata

import pytest

from agentrt.runtime import config


ALL_SETTINGS = (
    "AGENTRT_API_KEY",
    "AGENTRT_BASE_URL",
    "AGENTRT_DEFAULT_MODEL",
    "AGENTRT_9ROUTER_API_KEY",
    "AGENTRT_9ROUTER_BASE_URL",
)

SECRET = "sk-do-not-leak-7f3a"


@pytest.fixture
def clean_env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ALL_SETTINGS:
        monkeypatch.delenv(name, raising=False)
    # Point the state-file layer at an empty directory so an operator's real
    # ~/.agentrt/.env cannot supply settings the test meant to be absent.
    monkeypatch.setenv("AGENTRT_STATE_DIR", str(tmp_path))
    # The checkout layer is located by walking up from the runtime module, so a
    # development `.env` anywhere above the repository -- common on a working
    # machine -- would silently supply the settings these tests require to be
    # absent. Neutralise that layer explicitly; the state layer above is
    # already isolated.
    monkeypatch.setattr(config, "repo_env_file", lambda: None)


def test_neutral_aliases_supply_router_config(clean_env, monkeypatch) -> None:
    monkeypatch.setenv("AGENTRT_API_KEY", SECRET)
    monkeypatch.setenv("AGENTRT_BASE_URL", "https://api.deepseek.com/")
    monkeypatch.setenv("AGENTRT_DEFAULT_MODEL", "deepseek-flash")

    router = config.load_router_config()

    assert router.api_key == SECRET
    assert router.base_url == "https://api.deepseek.com"
    assert router.model == "deepseek-flash"
    assert SECRET not in repr(router)


def test_legacy_aliases_still_supply_router_config(clean_env, monkeypatch) -> None:
    monkeypatch.setenv("AGENTRT_9ROUTER_API_KEY", "legacy-key")
    monkeypatch.setenv("AGENTRT_9ROUTER_BASE_URL", "https://legacy.example")
    monkeypatch.setenv("AGENTRT_DEFAULT_MODEL", "ds/deepseek-flash")

    router = config.load_router_config()

    assert router.api_key == "legacy-key"
    assert router.base_url == "https://legacy.example"
    assert router.model == "ds/deepseek-flash"


def test_conflicting_aliases_in_one_layer_are_an_error(clean_env, monkeypatch) -> None:
    monkeypatch.setenv("AGENTRT_API_KEY", SECRET)
    monkeypatch.setenv("AGENTRT_9ROUTER_API_KEY", "different-key")

    with pytest.raises(config.ConfigConflictError) as excinfo:
        config.resolve_settings()

    message = str(excinfo.value)
    assert "AGENTRT_API_KEY" in message
    assert SECRET not in message
    assert "different-key" not in message


def test_equal_aliases_in_one_layer_are_accepted(clean_env, monkeypatch) -> None:
    monkeypatch.setenv("AGENTRT_API_KEY", SECRET)
    monkeypatch.setenv("AGENTRT_9ROUTER_API_KEY", SECRET)
    monkeypatch.setenv("AGENTRT_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("AGENTRT_9ROUTER_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("AGENTRT_DEFAULT_MODEL", "deepseek-flash")

    router = config.load_router_config()

    assert router.api_key == SECRET


def test_provenance_names_layer_and_alias_with_secret_redacted(
    clean_env, monkeypatch
) -> None:
    monkeypatch.setenv("AGENTRT_9ROUTER_API_KEY", SECRET)
    monkeypatch.setenv("AGENTRT_BASE_URL", "https://api.deepseek.com")
    monkeypatch.setenv("AGENTRT_DEFAULT_MODEL", "deepseek-flash")

    report = {entry["setting"]: entry for entry in config.describe_settings()}

    assert report["AGENTRT_API_KEY"]["source"] == "process environment"
    assert report["AGENTRT_API_KEY"]["alias"] == "AGENTRT_9ROUTER_API_KEY"
    assert report["AGENTRT_API_KEY"]["value"] == "<set>"
    assert report["AGENTRT_BASE_URL"]["alias"] == "AGENTRT_BASE_URL"
    assert report["AGENTRT_BASE_URL"]["value"] == "https://api.deepseek.com"


def test_environment_wins_per_setting_over_state_file(
    clean_env, monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("AGENTRT_STATE_DIR", str(tmp_path))
    (tmp_path / ".env").write_text(
        "AGENTRT_BASE_URL=https://from-file.example\n"
        "AGENTRT_DEFAULT_MODEL=deepseek-flash\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("AGENTRT_API_KEY", SECRET)

    provenance = config.resolve_settings_provenance()

    assert provenance["AGENTRT_API_KEY"].source == "process environment"
    assert provenance["AGENTRT_BASE_URL"].source == str(tmp_path / ".env")
    router = config.load_router_config()
    assert router.base_url == "https://from-file.example"


def test_conflict_is_reported_inside_explicit_env_file(clean_env, tmp_path) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "AGENTRT_BASE_URL=https://a.example\n"
        "AGENTRT_9ROUTER_BASE_URL=https://b.example\n"
        "AGENTRT_API_KEY=k\n"
        "AGENTRT_DEFAULT_MODEL=m\n",
        encoding="utf-8",
    )

    with pytest.raises(config.ConfigConflictError):
        config.load_router_config(env_path=env_file)


def test_missing_settings_name_both_spellings(clean_env) -> None:
    with pytest.raises(ValueError) as excinfo:
        config.load_router_config()

    message = str(excinfo.value)
    assert "AGENTRT_API_KEY or AGENTRT_9ROUTER_API_KEY" in message


def test_runtime_version_matches_installed_distribution() -> None:
    try:
        expected = importlib.metadata.version(config.RUNTIME_DISTRIBUTION)
    except importlib.metadata.PackageNotFoundError:
        expected = "unknown"

    assert config.runtime_version() == expected
    assert config.runtime_version()


def test_mcp_initialize_reports_runtime_version_not_library_version() -> None:
    from agentrt.runtime import mcp_server

    options = mcp_server.mcp._mcp_server.create_initialization_options()

    assert options.server_version == config.runtime_version()
    assert options.server_version != importlib.metadata.version("mcp")


def test_cli_supports_conventional_version_flag(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from agentrt.runtime.cli import main

    with pytest.raises(SystemExit) as excinfo:
        main(["--version"])

    assert excinfo.value.code == 0
    assert capsys.readouterr().out.strip() == (
        f"agentrt-runtime {config.runtime_version()}"
    )
