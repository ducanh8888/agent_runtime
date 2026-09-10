import asyncio
import sys
import time

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field

from agentrt.agent_server.build_identity import (
    SERVER_DISTRIBUTION,
    BuildIdentity,
    package_version,
    runtime_version,
    server_build_identity,
)
from agentrt.sdk.tool.registry import list_usable_tools
from agentrt.tools.terminal.timeout_policy import (
    get_max_foreground_timeout_seconds,
    get_runtime_idle_timeout_seconds,
)


server_details_router = APIRouter(prefix="", tags=["Server Details"])
_start_time = time.time()
_last_event_time = time.time()
_initialization_complete = asyncio.Event()


class HealthStatus(BaseModel):
    status: str


class ServerInfo(BaseModel):
    uptime: float
    idle_time: float
    title: str = "OpenHands Agent Server"

    version: str = Field(default_factory=lambda: package_version(SERVER_DISTRIBUTION))
    sdk_version: str = Field(default_factory=lambda: package_version("agentrt-sdk"))
    tools_version: str = Field(default_factory=lambda: package_version("agentrt-tools"))
    workspace_version: str = Field(
        default_factory=lambda: package_version("agentrt-workspace")
    )
    runtime_version: str = Field(default_factory=runtime_version)

    build_git_sha: str = Field(default_factory=lambda: server_build_identity().git_sha)
    build_git_ref: str = Field(default_factory=lambda: server_build_identity().git_ref)
    build: BuildIdentity = Field(default_factory=server_build_identity)
    python_version: str = Field(default_factory=lambda: sys.version)
    usable_tools: list[str] = Field(default_factory=lambda: list_usable_tools())
    runtime_idle_timeout_seconds: float | None = Field(
        default_factory=lambda: get_runtime_idle_timeout_seconds()
    )
    capabilities: list[str] = Field(
        default_factory=lambda: [
            "credential_binding_v1",
            "credential_binding_readiness_probe_v1",
            "credential_binding_activation_guard_v1",
            "build_identity_v1",
            "usage_provenance_v1",
        ]
    )
    max_foreground_terminal_timeout_seconds: float | None = Field(
        default_factory=lambda: get_max_foreground_timeout_seconds()
    )

    docs: str = "/docs"
    redoc: str = "/redoc"


def update_last_execution_time():
    global _last_event_time
    _last_event_time = time.time()


def mark_initialization_complete() -> None:
    """Mark the server as fully initialized and ready to serve requests.

    This should be called after all services (VSCode, desktop, tool preload, etc.)
    have finished initializing. Until this is called, the /ready endpoint will
    return 503 Service Unavailable.
    """
    _initialization_complete.set()


@server_details_router.get("/alive")
async def alive() -> HealthStatus:
    """Basic liveness check - returns OK if the server process is running."""
    return HealthStatus(status="ok")


@server_details_router.get("/health")
async def health() -> HealthStatus:
    """Basic health check - returns OK if the server process is running."""
    return HealthStatus(status="ok")


@server_details_router.get("/ready")
async def ready(response: Response) -> dict[str, str]:
    """Readiness check - returns OK only if the server has completed initialization.

    This endpoint should be used by Kubernetes readiness probes to determine
    when the pod is ready to receive traffic. Returns 503 during initialization.
    """
    if _initialization_complete.is_set():
        return {"status": "ready"}
    else:
        response.status_code = 503
        return {"status": "initializing", "message": "Server is still initializing"}


@server_details_router.get("/server_info")
async def get_server_info() -> ServerInfo:
    now = time.time()
    return ServerInfo(
        uptime=int(now - _start_time),
        idle_time=int(now - _last_event_time),
    )
