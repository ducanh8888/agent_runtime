from pydantic import Field

from agentrt.sdk.event.base import Event
from agentrt.sdk.event.types import SourceType


class TokenEvent(Event):
    """Event from VLLM representing token IDs used in LLM interaction."""

    source: SourceType
    prompt_token_ids: list[int] = Field(
        ..., description="The exact prompt token IDs for this message event"
    )
    response_token_ids: list[int] = Field(
        ..., description="The exact response token IDs for this message event"
    )
