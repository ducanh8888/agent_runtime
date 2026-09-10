"""Build identity for the agent-server process.

The server reports the identity of the distribution it *is*, not of a
dependency it happens to import. ``importlib.metadata`` resolves the installed
distribution; when the server runs from a source tree with no installed
metadata the version is reported as ``unknown`` rather than borrowed from the
SDK.

The git build stamp comes from environment variables baked in at image build
time. Both the post-rename ``AGENTRT_`` names and the legacy ``OPENHANDS_``
names are accepted: the published image workflow still passes the legacy build
args, so reading only the new name silently reported ``unknown`` on released
images.
"""

from __future__ import annotations

import os
from importlib.metadata import version
from typing import Final

from pydantic import BaseModel, ConfigDict, Field


SERVER_DISTRIBUTION: Final[str] = "agentrt-server"
RUNTIME_DISTRIBUTION: Final[str] = "agentrt-runtime"
UNKNOWN: Final[str] = "unknown"

# Ordered by precedence; the first non-empty value wins. Each layer resolves
# old/new aliases before the service reports provenance.
BUILD_GIT_SHA_ENVIRONMENTS: Final[tuple[str, ...]] = (
    "AGENTRT_BUILD_GIT_SHA",
    "OPENHANDS_BUILD_GIT_SHA",
)
BUILD_GIT_REF_ENVIRONMENTS: Final[tuple[str, ...]] = (
    "AGENTRT_BUILD_GIT_REF",
    "OPENHANDS_BUILD_GIT_REF",
)


def package_version(distribution: str) -> str:
    """Return an installed distribution's version, or ``unknown``."""
    try:
        return version(distribution)
    except Exception:
        return UNKNOWN


def _resolve_environ(environments: tuple[str, ...]) -> tuple[str, str | None]:
    """Return the first non-empty value and the variable that supplied it."""
    for name in environments:
        value = os.environ.get(name)
        if value:
            return value, name
    return UNKNOWN, None


class BuildIdentity(BaseModel):
    """Structured identity of the server build, safe to expose on the wire."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    distribution: str = SERVER_DISTRIBUTION
    version: str = Field(default_factory=lambda: package_version(SERVER_DISTRIBUTION))
    git_sha: str = UNKNOWN
    git_ref: str = UNKNOWN
    # The variable an env-provided value came from, or ``None`` when unset.
    # This is identity provenance, never a secret.
    git_sha_env: str | None = None
    git_ref_env: str | None = None


def server_build_identity() -> BuildIdentity:
    """Snapshot this server's distribution version and build stamp."""
    git_sha, git_sha_env = _resolve_environ(BUILD_GIT_SHA_ENVIRONMENTS)
    git_ref, git_ref_env = _resolve_environ(BUILD_GIT_REF_ENVIRONMENTS)
    return BuildIdentity(
        git_sha=git_sha,
        git_ref=git_ref,
        git_sha_env=git_sha_env,
        git_ref_env=git_ref_env,
    )


def runtime_version() -> str:
    """Version of the ``agentrt-runtime`` distribution owning this server.

    Distribution metadata, not the version of the ``mcp`` library that hosts
    the runtime's MCP surface. ``unknown`` for a bare server deployment where
    the runtime is not installed.
    """
    return package_version(RUNTIME_DISTRIBUTION)
