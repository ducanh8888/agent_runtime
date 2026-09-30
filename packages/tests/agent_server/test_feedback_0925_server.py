"""Server-side fixes from the 2026-09-21/25 orchestrator feedback."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4

from agentrt.agent_server.conversation_service import ConversationService
from agentrt.agent_server.event_service import _progress_age_seconds
from agentrt.agent_server.models import StartConversationRequest
from agentrt.sdk import Agent
from agentrt.sdk.conversation.state import ConversationExecutionStatus
from agentrt.sdk.event.conversation_error import ConversationErrorEvent
from agentrt.sdk.workspace import LocalWorkspace
from tests.agent_server.stress.scripts import placeholder_llm


def test_progress_age_reads_naive_event_timestamp_as_local_time() -> None:
    """Event.timestamp is naive local time; a 10-minute-old event must read
    ~600s regardless of the host's UTC offset (it read 0.0 on GMT+7)."""
    ten_minutes_ago = (datetime.now() - timedelta(minutes=10)).isoformat()
    age = _progress_age_seconds(ten_minutes_ago)
    assert age is not None
    assert 590 <= age <= 610


def test_progress_age_still_honours_explicit_offsets() -> None:
    aware = (datetime.now().astimezone() - timedelta(seconds=30)).isoformat()
    age = _progress_age_seconds(aware)
    assert age is not None
    assert 25 <= age <= 40


async def _persisted_running_conversation(persist: Path, workspace: Path):
    """Create a conversation, shut its service down, then make the persisted
    state read RUNNING -- what a daemon killed mid-run leaves behind."""
    conv_id = uuid4()
    async with ConversationService(conversations_dir=persist) as first:
        await first.start_conversation(
            StartConversationRequest(
                conversation_id=conv_id,
                agent=Agent(llm=placeholder_llm("restart"), tools=[]),
                workspace=LocalWorkspace(working_dir=str(workspace)),
                autotitle=False,
            )
        )
    conv_dir = persist / conv_id.hex
    base_state = conv_dir / "base_state.json"
    payload = json.loads(base_state.read_text())
    payload["execution_status"] = ConversationExecutionStatus.RUNNING.value
    base_state.write_text(json.dumps(payload))
    return conv_id, conv_dir


async def test_running_record_held_by_live_lease_is_recovered_after_expiry(
    tmp_path: Path,
) -> None:
    """A daemon started while the previous one still held a conversation's
    lease used to skip that RUNNING record once and never look again: it read
    `running` forever with nothing driving it. Now it is retried until the
    lease frees, then settled as ERROR with a DaemonRestarted event, and
    capacity() reports it."""
    persist = tmp_path / "persist"
    persist.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    conv_id, conv_dir = await _persisted_running_conversation(persist, workspace)
    # A still-live foreign owner (our parent process) holding a short lease.
    (conv_dir / "owner_lease.json").write_text(
        json.dumps(
            {
                "owner_instance_id": "previous-daemon",
                "generation": 1,
                "expires_at": time.time() + 2.0,
                "owner_host": socket.gethostname(),
                "owner_pid": os.getppid(),
            }
        )
    )

    second = ConversationService(conversations_dir=persist, lease_ttl_seconds=3.0)
    async with second:
        capacity = await second.capacity()
        assert capacity["pending_restart_recovery"] == 1
        assert capacity["recovered_after_restart"] == 0
        assert capacity["server_started_at"]

        for _ in range(80):
            capacity = await second.capacity()
            if capacity["pending_restart_recovery"] == 0:
                break
            await asyncio.sleep(0.1)
        assert capacity["pending_restart_recovery"] == 0
        assert capacity["recovered_after_restart"] == 1

        event_service = await second.get_event_service(conv_id)
        assert event_service is not None
        state = await event_service.get_state()
        assert state.execution_status == ConversationExecutionStatus.ERROR
        codes = [e.code for e in state.events if isinstance(e, ConversationErrorEvent)]
        assert "DaemonRestarted" in codes


async def test_updated_at_advances_with_activity_in_search_and_on_disk(
    tmp_path: Path,
) -> None:
    """25/09 #5: `updated_at` in list/status stayed at creation time while a
    session was active. Activity must move it both in this daemon's search
    and in the persisted meta.json another reader (a restarted daemon, the
    catalog before hydration) sees."""
    from agentrt.sdk.event import PauseEvent

    persist = tmp_path / "persist"
    persist.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    conv_id = uuid4()
    async with ConversationService(conversations_dir=persist) as svc:
        info, _ = await svc.start_conversation(
            StartConversationRequest(
                conversation_id=conv_id,
                agent=Agent(llm=placeholder_llm("upd"), tools=[]),
                workspace=LocalWorkspace(working_dir=str(workspace)),
                autotitle=False,
            )
        )
        created = info.updated_at
        await asyncio.sleep(0.05)
        event_service = await svc.get_event_service(conv_id)
        assert event_service is not None
        await event_service._pub_sub(PauseEvent())

        page = await svc.search_conversations(limit=10)
        listed = next(item for item in page.items if item.id == conv_id)
        assert listed.updated_at > created

        meta = json.loads((persist / conv_id.hex / "meta.json").read_text())
        assert datetime.fromisoformat(meta["updated_at"]) > created


async def test_running_record_with_free_lease_is_recovered_at_startup(
    tmp_path: Path,
) -> None:
    persist = tmp_path / "persist"
    persist.mkdir()
    workspace = tmp_path / "ws"
    workspace.mkdir()
    await _persisted_running_conversation(persist, workspace)

    async with ConversationService(conversations_dir=persist) as second:
        capacity = await second.capacity()
        assert capacity["pending_restart_recovery"] == 0
        assert capacity["recovered_after_restart"] == 1
