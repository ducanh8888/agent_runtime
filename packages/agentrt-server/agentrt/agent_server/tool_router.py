"""Tool router for OpenHands SDK."""

import os

from fastapi import APIRouter

from agentrt.sdk.tool.registry import list_registered_tools
from agentrt.tools.preset.default import (
    register_builtins_agents,
    register_default_tools,
)
from agentrt.tools.preset.gemini import register_gemini_tools
from agentrt.tools.preset.planning import register_planning_tools


tool_router = APIRouter(prefix="/tools", tags=["Tools"])
register_default_tools(enable_browser=True)
# `register_builtins_agents` registers OpenHands' own four vendored
# sub-agents (code_explorer, bash_runner, web_researcher, default) and the
# `delegate` tool that spawns them. No AgentRT permission preset grants
# `delegate` (readonly/inspect/broad/workspace all omit it), so no dispatched
# session has ever been able to reach this -- registered, logged, and (via
# register_default_tools above) probed for a browser tool at every boot for a
# capability that is dead weight in this deployment. This gate is module-
# level, import-time code (runs before any Config instance exists), so it
# reads the environment directly rather than through the declarative
# Config/env_parser field machinery the rest of this package uses -- that
# machinery is not available yet at this point in import order.
# docs/plans/deepseek-hardening.md H9 (OpenHands ceremony), 2026-09-17.
if os.environ.get("AGENTRT_REGISTER_BUILTIN_SUBAGENTS", "1").strip().lower() not in (
    "0",
    "false",
    "no",
):
    register_builtins_agents(enable_browser=True)
register_gemini_tools(enable_browser=True)
register_planning_tools()


# Tool listing
@tool_router.get("/")
async def list_available_tools() -> list[str]:
    """List all available tools."""
    tools = list_registered_tools()
    return tools
