"""AgentRT #10: a search page rebuilds its stale rows in one thread hop.

A running session autosaves after every step, so its cached search row is
stale almost every time. Rebuilding each row with its own two ``to_thread``
hops made a page cost one event-loop round trip per hop, which grows with
how busy the loop is.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from uuid import uuid4

import pytest

from agentrt.agent_server import conversation_service as cs
from agentrt.agent_server.conversation_service import ConversationService
from agentrt.agent_server.models import StartConversationRequest
from agentrt.sdk import Agent
from agentrt.sdk.workspace import LocalWorkspace
from tests.agent_server.stress.scripts import placeholder_llm


pytestmark = pytest.mark.asyncio


async def test_stale_rows_of_a_page_are_rebuilt_in_one_hop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    persist = tmp_path / "persist"
    persist.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    async with ConversationService(conversations_dir=persist) as svc:
        ids = []
        for _ in range(20):
            info, _ = await svc.start_conversation(
                StartConversationRequest(
                    conversation_id=uuid4(),
                    agent=Agent(llm=placeholder_llm("batch"), tools=[]),
                    workspace=LocalWorkspace(working_dir=str(workspace)),
                    autotitle=False,
                )
            )
            ids.append(info.id)
        await svc.search_conversations(limit=50)  # warm every row's cache

        # What an autosave does to each running session's row.
        for conversation_id in ids:
            base_state = persist / conversation_id.hex / "base_state.json"
            stat = base_state.stat()
            os.utime(base_state, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))

        hops = 0
        real_to_thread = asyncio.to_thread

        async def counting_to_thread(func, /, *args, **kwargs):
            nonlocal hops
            hops += 1
            return await real_to_thread(func, *args, **kwargs)

        monkeypatch.setattr(cs.asyncio, "to_thread", counting_to_thread)
        page = await svc.search_conversations(limit=50)

    assert {item.id for item in page.items} == set(ids)
    assert hops == 1
