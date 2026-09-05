"""Gemini preset configuration for OpenHands agents.

This preset uses gemini-style file editing tools instead of the default
claude-style file_editor tool.
"""

from agentrt.sdk import Agent
from agentrt.sdk.context.condenser import (
    LLMSummarizingCondenser,
)
from agentrt.sdk.context.condenser.base import CondenserBase
from agentrt.sdk.llm.llm import LLM
from agentrt.sdk.logger import get_logger
from agentrt.sdk.tool import Tool


logger = get_logger(__name__)


def register_gemini_tools(enable_browser: bool = True) -> None:
    """Register the gemini set of tools."""
    from agentrt.tools.gemini import (
        EditTool,
        ListDirectoryTool,
        ReadFileTool,
        WriteFileTool,
    )
    from agentrt.tools.task_tracker import TaskTrackerTool
    from agentrt.tools.terminal import TerminalTool

    logger.debug(f"Tool: {TerminalTool.name} registered.")
    logger.debug(f"Tool: {ReadFileTool.name} registered.")
    logger.debug(f"Tool: {WriteFileTool.name} registered.")
    logger.debug(f"Tool: {EditTool.name} registered.")
    logger.debug(f"Tool: {ListDirectoryTool.name} registered.")
    logger.debug(f"Tool: {TaskTrackerTool.name} registered.")

    if enable_browser:
        from agentrt.tools.browser_use import BrowserToolSet

        logger.debug(f"Tool: {BrowserToolSet.name} registered.")


def get_gemini_tools(
    enable_browser: bool = True,
) -> list[Tool]:
    """Get the gemini set of tool specifications.

    This uses gemini-style file editing tools (read_file, write_file, edit,
    list_directory) instead of the default claude-style file_editor tool.

    Args:
        enable_browser: Whether to include browser tools.
    """
    register_gemini_tools(enable_browser=enable_browser)

    from agentrt.tools.gemini import (
        EditTool,
        ListDirectoryTool,
        ReadFileTool,
        WriteFileTool,
    )
    from agentrt.tools.task_tracker import TaskTrackerTool
    from agentrt.tools.terminal import TerminalTool

    tools = [
        Tool(name=TerminalTool.name),
        Tool(name=ReadFileTool.name),
        Tool(name=WriteFileTool.name),
        Tool(name=EditTool.name),
        Tool(name=ListDirectoryTool.name),
        Tool(name=TaskTrackerTool.name),
    ]
    if enable_browser:
        from agentrt.tools.browser_use import BrowserToolSet

        tools.append(Tool(name=BrowserToolSet.name))
    return tools


def get_gemini_condenser(llm: LLM) -> CondenserBase:
    """Get the default condenser for gemini preset."""
    condenser = LLMSummarizingCondenser(llm=llm, max_size=80, keep_first=4)
    return condenser


def get_gemini_agent(
    llm: LLM,
    cli_mode: bool = False,
) -> Agent:
    """Get an agent with gemini-style tools: read_file, write_file, edit,
    list_directory."""
    tools = get_gemini_tools(
        enable_browser=not cli_mode,
    )
    agent = Agent(
        llm=llm,
        tools=tools,
        system_prompt_kwargs={"cli_mode": cli_mode},
        condenser=get_gemini_condenser(
            llm=llm.model_copy(update={"usage_id": "condenser"})
        ),
    )
    return agent
