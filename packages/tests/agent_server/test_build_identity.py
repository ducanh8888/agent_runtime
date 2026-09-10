"""Tests for the agent-server build identity surface."""

from importlib.metadata import version

import pytest

from agentrt.agent_server.build_identity import (
    SERVER_DISTRIBUTION,
    BuildIdentity,
    package_version,
    runtime_version,
    server_build_identity,
)
from agentrt.agent_server.server_details_router import ServerInfo


def test_package_version_reports_installed_distribution():
    assert package_version(SERVER_DISTRIBUTION) == version(SERVER_DISTRIBUTION)


def test_package_version_unknown_for_missing_distribution():
    assert package_version("agentrt-does-not-exist") == "unknown"


def test_build_git_env_prefers_agentrt_over_legacy(monkeypatch):
    monkeypatch.setenv("AGENTRT_BUILD_GIT_SHA", "new-sha")
    monkeypatch.setenv("OPENHANDS_BUILD_GIT_SHA", "old-sha")
    monkeypatch.setenv("AGENTRT_BUILD_GIT_REF", "refs/heads/main")

    identity = server_build_identity()

    assert identity.distribution == SERVER_DISTRIBUTION
    assert identity.git_sha == "new-sha"
    assert identity.git_sha_env == "AGENTRT_BUILD_GIT_SHA"
    assert identity.git_ref == "refs/heads/main"
    assert identity.git_ref_env == "AGENTRT_BUILD_GIT_REF"


def test_build_git_env_falls_back_to_legacy_names(monkeypatch):
    monkeypatch.delenv("AGENTRT_BUILD_GIT_SHA", raising=False)
    monkeypatch.delenv("AGENTRT_BUILD_GIT_REF", raising=False)
    monkeypatch.setenv("OPENHANDS_BUILD_GIT_SHA", "legacy-sha")
    monkeypatch.setenv("OPENHANDS_BUILD_GIT_REF", "legacy-ref")

    identity = server_build_identity()

    assert identity.git_sha == "legacy-sha"
    assert identity.git_sha_env == "OPENHANDS_BUILD_GIT_SHA"
    assert identity.git_ref == "legacy-ref"
    assert identity.git_ref_env == "OPENHANDS_BUILD_GIT_REF"


def test_build_git_env_unknown_when_unset(monkeypatch):
    for name in (
        "AGENTRT_BUILD_GIT_SHA",
        "OPENHANDS_BUILD_GIT_SHA",
        "AGENTRT_BUILD_GIT_REF",
        "OPENHANDS_BUILD_GIT_REF",
    ):
        monkeypatch.delenv(name, raising=False)

    identity = server_build_identity()

    assert identity.git_sha == "unknown"
    assert identity.git_ref == "unknown"
    assert identity.git_sha_env is None
    assert identity.git_ref_env is None


def test_runtime_version_comes_from_runtime_distribution():
    reported = runtime_version()
    try:
        expected = version("agentrt-runtime")
    except Exception:
        assert reported == "unknown"
    else:
        assert reported == expected


def test_server_info_exposes_structured_build_and_runtime_version(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("AGENTRT_BUILD_GIT_SHA", "abc123")
    monkeypatch.setenv("AGENTRT_BUILD_GIT_REF", "refs/heads/main")

    info = ServerInfo(uptime=0.0, idle_time=0.0)

    assert isinstance(info.build, BuildIdentity)
    assert info.build.git_sha == "abc123"
    assert info.build.git_ref == "refs/heads/main"
    assert info.build_git_sha == "abc123"
    assert info.build_git_ref == "refs/heads/main"
    assert info.runtime_version == runtime_version()


def test_server_info_capabilities_advertise_new_surfaces():
    info = ServerInfo(uptime=0.0, idle_time=0.0)

    assert "build_identity_v1" in info.capabilities
    assert "usage_provenance_v1" in info.capabilities
