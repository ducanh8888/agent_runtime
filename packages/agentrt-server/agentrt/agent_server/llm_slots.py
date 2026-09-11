"""Deployment-level cap on concurrent in-flight LLM requests.

The cap belongs to a provider account, not to a conversation, so there is one
limiter per daemon process and it is shared by every run. It is off by default
(`AGENTRT_MAX_INFLIGHT_LLM` unset or zero): the generic SDK keeps its unbounded
behaviour, and a deployment that knows its account's limit asks for this.

The slot is taken around the transport call, not around the whole completion.
That matters for the retry policy: a 429 retry sleeps for seconds between
attempts, and holding a provider slot through that sleep would idle exactly the
capacity the retry is waiting to use.
"""

from __future__ import annotations

import asyncio
import threading


class ProviderSlots:
    """Counts and caps concurrent in-flight LLM transport calls."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self._semaphore: asyncio.Semaphore | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._counter_lock = threading.Lock()
        self.in_flight = 0

    def _semaphore_for_loop(self) -> asyncio.Semaphore:
        """A semaphore bound to the running loop.

        An ``asyncio.Semaphore`` is bound to the loop that first awaits it, and
        tests run each case in a fresh loop, so it is recreated when the loop
        changes rather than shared across them.
        """
        loop = asyncio.get_running_loop()
        if self._semaphore is None or self._loop is not loop:
            self._semaphore = asyncio.Semaphore(self.limit)
            self._loop = loop
        return self._semaphore

    def lease(self) -> _SlotLease:
        """Acquire a slot for the duration of a ``async with`` block."""
        return _SlotLease(self)


class _SlotLease:
    def __init__(self, slots: ProviderSlots) -> None:
        self._slots = slots
        self._acquired = False

    async def __aenter__(self) -> _SlotLease:
        semaphore = self._slots._semaphore_for_loop()
        await semaphore.acquire()
        self._acquired = True
        with self._slots._counter_lock:
            self._slots.in_flight += 1
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if not self._acquired:
            return
        self._acquired = False
        with self._slots._counter_lock:
            self._slots.in_flight -= 1
        semaphore = self._slots._semaphore
        if semaphore is not None:
            semaphore.release()


_slots: ProviderSlots | None = None
_installed = False


def install_provider_slots(limit: int) -> ProviderSlots | None:
    """Cap concurrent in-flight LLM requests in this process.

    Idempotent: a second call with a new limit updates the existing limiter
    rather than stacking a second patch on the transport. Zero or negative
    leaves the SDK exactly as it was and returns None.
    """
    global _slots, _installed
    if limit <= 0:
        return None
    if _installed and _slots is not None:
        _slots.limit = limit
        return _slots

    from agentrt.sdk.llm.llm import LLM

    slots = ProviderSlots(limit)
    original = LLM._atransport_call

    async def _limited_atransport(self, *args: object, **kwargs: object) -> object:
        async with slots.lease():
            return await original(self, *args, **kwargs)

    # A deployment policy, installed once in the daemon process. It narrows
    # concurrency without removing any SDK capability, and every process that
    # does not ask for it keeps the SDK's unbounded behaviour.
    setattr(LLM, "_atransport_call", _limited_atransport)
    _slots = slots
    _installed = True
    return slots


def provider_slots() -> ProviderSlots | None:
    """The installed limiter, or None when this process did not install one."""
    return _slots
