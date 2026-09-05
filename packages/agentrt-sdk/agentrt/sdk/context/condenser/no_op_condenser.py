from agentrt.sdk.context.condenser.base import CondenserBase
from agentrt.sdk.context.view import View
from agentrt.sdk.event.condenser import Condensation
from agentrt.sdk.llm import LLM


class NoOpCondenser(CondenserBase):
    """Simple condenser that returns a view un-manipulated.

    Primarily intended for testing purposes.
    """

    def condense(self, view: View, agent_llm: LLM | None = None) -> View | Condensation:  # noqa: ARG002
        return view
