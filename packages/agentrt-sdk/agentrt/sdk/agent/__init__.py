from __future__ import annotations

from typing import TYPE_CHECKING

from agentrt.sdk.agent.agent import Agent
from agentrt.sdk.agent.base import AgentBase


if TYPE_CHECKING:
    from agentrt.sdk.agent.acp_agent import ACPAgent


# Lazy import: eagerly importing ACPAgent registers it in the
# DiscriminatedUnionMixin, which makes `kind` required in Agent payloads
# that previously defaulted.
def __getattr__(name: str):
    if name == "ACPAgent":
        from agentrt.sdk.agent.acp_agent import ACPAgent

        return ACPAgent
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "Agent",
    "AgentBase",
    "ACPAgent",
]
