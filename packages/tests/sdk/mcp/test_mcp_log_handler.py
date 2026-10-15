"""MCP server log notifications reach the Python log whatever shape `data` has.

MCP lets a log notification carry any JSON value. A server sending a plain
string made the handler raise, and the mcp session logged that failure as a
multi-line warning per notification -- 67 MB of daemon.log in 22 minutes on
one AgentRT client (#22).
"""

import logging

import pytest
from fastmcp.client.logging import LogMessage

from agentrt.sdk.mcp.utils import log_handler


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "data, expected",
    [
        ("Contents API call returned 3 results", "Contents API call returned 3"),
        ({"msg": "structured message", "extra": {"k": "v"}}, "structured message"),
        ({"other": 1}, "other"),
        (["a", "list"], "list"),
        (42, "42"),
    ],
)
async def test_log_handler_accepts_any_json_data(caplog, data, expected) -> None:
    with caplog.at_level(logging.DEBUG, logger="agentrt.sdk.mcp.utils"):
        await log_handler(LogMessage(level="warning", data=data))

    assert any(expected in record.getMessage() for record in caplog.records)
