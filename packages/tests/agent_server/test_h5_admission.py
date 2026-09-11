"""H5: durable admission queue, capacity surface and submission idempotency."""

from __future__ import annotations

from uuid import uuid4

import pytest
from pydantic import SecretStr

from agentrt.agent_server import (
    conversation_service as conversation_service_mod,
    llm_slots as llm_slots_mod,
)
from agentrt.agent_server.conversation_service import (
    ConversationService,
    IdempotencyConflict,
    _ConversationRecord,
)
from agentrt.agent_server.llm_slots import (
    ProviderSlots,
    install_provider_slots,
)
from agentrt.agent_server.models import StoredConversation
from agentrt.sdk import Agent
from agentrt.sdk.conversation.request import StartConversationRequest
from agentrt.sdk.conversation.state import ConversationExecutionStatus
from agentrt.sdk.llm import LLM
from agentrt.sdk.workspace import LocalWorkspace


class _Task:
    """A run task stand-in: only ``done()`` is consulted."""

    def __init__(self, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


class _Service:
    def __init__(self, run_task) -> None:
        self._run_task = run_task


def _stored(conversation_id, **overrides) -> StoredConversation:
    values = {
        "id": conversation_id,
        "workspace": LocalWorkspace(working_dir="/tmp/h5"),
    }
    values.update(overrides)
    return StoredConversation(**values)


def _record(conversation_id, **overrides) -> _ConversationRecord:
    return _ConversationRecord(
        stored=_stored(conversation_id, **overrides),
        execution_status=ConversationExecutionStatus.IDLE,
    )


def _request(**overrides) -> StartConversationRequest:
    payload = {
        "conversation_id": uuid4(),
        "agent": Agent(
            llm=LLM(model="gpt-4o-mini", api_key=SecretStr("k"), usage_id="test-llm"),
            tools=[],
        ),
        "workspace": {"working_dir": "/tmp/h5"},
        "initial_message": {
            "role": "user",
            "content": [{"type": "text", "text": "do the thing"}],
        },
    }
    payload.update(overrides)
    return StartConversationRequest.model_validate(payload)


# --- capacity -------------------------------------------------------------


@pytest.mark.asyncio
async def test_capacity_is_unbounded_when_the_cap_is_disabled(tmp_path) -> None:
    service = ConversationService(conversations_dir=tmp_path, max_concurrent_runs=0)
    service._event_services = {}

    surface = await service.capacity()

    assert surface["limiting_dimension"] is None
    assert surface["limit"] is None
    # "Unbounded" is not a number to subtract from.
    assert surface["available"] is None
    assert surface["running"] == 0


@pytest.mark.asyncio
async def test_capacity_counts_live_run_tasks(tmp_path) -> None:
    service = ConversationService(conversations_dir=tmp_path, max_concurrent_runs=2)
    first, second = uuid4(), uuid4()
    service._event_services = {
        first: _Service(_Task(done=False)),
        second: _Service(_Task(done=True)),
    }

    surface = await service.capacity()

    assert surface["running"] == 1
    assert surface["available"] == 1
    assert surface["limiting_dimension"] == "runs"
    assert service._has_free_slot() is True

    service._event_services[second] = _Service(_Task(done=False))
    service._event_services[uuid4()] = _Service(_Task(done=False))
    surface = await service.capacity()
    assert surface["running"] == 3
    assert surface["available"] == 0
    assert service._has_free_slot() is False


def test_queue_is_rebuilt_in_submission_order(tmp_path) -> None:
    service = ConversationService(conversations_dir=tmp_path)
    early, late, admitted = uuid4(), uuid4(), uuid4()
    service._conversation_records = {
        late: _record(
            late, admission_state="queued", admission_enqueued_at="2026-01-01T00:01:00Z"
        ),
        early: _record(
            early,
            admission_state="queued",
            admission_enqueued_at="2026-01-01T00:00:00Z",
        ),
        admitted: _record(admitted),
    }

    service._rebuild_admission_queue()

    assert service._admission_queue == [early, late]


# --- idempotency ----------------------------------------------------------


def test_fingerprint_ignores_the_conversation_id() -> None:
    """A retry with a fresh id is still the same submission."""
    first = _request()
    second = _request(conversation_id=uuid4())

    assert ConversationService._submission_fingerprint(
        first
    ) == ConversationService._submission_fingerprint(second)


def test_fingerprint_changes_with_the_task() -> None:
    first = _request()
    second = _request(
        initial_message={
            "role": "user",
            "content": [{"type": "text", "text": "a different job"}],
        }
    )

    assert ConversationService._submission_fingerprint(
        first
    ) != ConversationService._submission_fingerprint(second)


def test_repeated_submission_replays_the_existing_record(tmp_path) -> None:
    service = ConversationService(conversations_dir=tmp_path)
    request = _request(idempotency_key="batch-1")
    existing_id = uuid4()
    service._conversation_records = {
        existing_id: _record(
            existing_id,
            idempotency_key="batch-1",
            idempotency_fingerprint=ConversationService._submission_fingerprint(
                request
            ),
        )
    }

    found = service._find_by_idempotency_key(request)

    assert found is not None
    assert found.stored.id == existing_id


def test_same_key_with_different_work_is_a_conflict(tmp_path) -> None:
    service = ConversationService(conversations_dir=tmp_path)
    stored_request = _request(idempotency_key="batch-1")
    other_request = _request(
        idempotency_key="batch-1",
        initial_message={
            "role": "user",
            "content": [{"type": "text", "text": "a different job"}],
        },
    )
    existing_id = uuid4()
    service._conversation_records = {
        existing_id: _record(
            existing_id,
            idempotency_key="batch-1",
            idempotency_fingerprint=ConversationService._submission_fingerprint(
                stored_request
            ),
        )
    }

    with pytest.raises(IdempotencyConflict, match="different submission"):
        service._find_by_idempotency_key(other_request)


def test_a_request_without_a_key_never_replays(tmp_path) -> None:
    service = ConversationService(conversations_dir=tmp_path)

    assert service._find_by_idempotency_key(_request()) is None


# --- provider slots -------------------------------------------------------


class _Slots:
    def __init__(self, limit: int, in_flight: int = 0) -> None:
        self.limit = limit
        self.in_flight = in_flight


@pytest.mark.asyncio
async def test_provider_slots_cap_concurrency() -> None:
    import asyncio

    slots = ProviderSlots(limit=1)
    order: list[str] = []

    async def worker(name: str, hold: float) -> None:
        async with slots.lease():
            order.append(f"{name}-in")
            await asyncio.sleep(hold)
            order.append(f"{name}-out")

    await asyncio.gather(worker("a", 0.05), worker("b", 0.001))

    # One slot: the second cannot enter before the first has left.
    assert order == ["a-in", "a-out", "b-in", "b-out"]
    assert slots.in_flight == 0


@pytest.mark.asyncio
async def test_provider_slots_allow_the_limit() -> None:
    import asyncio

    slots = ProviderSlots(limit=2)
    barrier = asyncio.Event()

    async def worker() -> None:
        async with slots.lease():
            if slots.in_flight == 2:
                barrier.set()
            await asyncio.wait_for(barrier.wait(), timeout=1)

    await asyncio.gather(worker(), worker())
    assert slots.in_flight == 0


def test_install_provider_slots_is_a_noop_at_zero() -> None:
    assert install_provider_slots(0) is None
    assert install_provider_slots(-1) is None


@pytest.mark.asyncio
async def test_install_patches_the_transport_once() -> None:
    from agentrt.sdk.llm.llm import LLM

    original = LLM._atransport_call
    try:
        slots = install_provider_slots(1)
        assert slots is not None
        patched = LLM._atransport_call
        assert patched is not original

        # A second install updates the limit instead of stacking a patch.
        again = install_provider_slots(3)
        assert again is slots
        assert LLM._atransport_call is patched
        assert slots.limit == 3
    finally:
        LLM._atransport_call = original
        llm_slots_mod._slots = None
        llm_slots_mod._installed = False


@pytest.mark.asyncio
async def test_capacity_names_the_binding_llm_limit(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        conversation_service_mod, "provider_slots", lambda: _Slots(4, in_flight=4)
    )
    service = ConversationService(conversations_dir=tmp_path, max_concurrent_runs=10)
    service._event_services = {}

    surface = await service.capacity()

    assert surface["llm_limit"] == 4
    assert surface["in_flight_llm"] == 4
    # The LLM cap is what is binding, not the run cap.
    assert surface["limiting_dimension"] == "llm_requests"


@pytest.mark.asyncio
async def test_capacity_has_no_llm_limit_when_uninstalled(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(conversation_service_mod, "provider_slots", lambda: None)
    service = ConversationService(conversations_dir=tmp_path, max_concurrent_runs=5)
    service._event_services = {}

    surface = await service.capacity()

    assert surface["llm_limit"] is None
    assert surface["in_flight_llm"] == 0
    assert surface["limiting_dimension"] == "runs"
