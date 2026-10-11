"""What passes between the nodes.

One dict, added to as it goes. Nothing here reads the database or calls out;
the nodes do that and put their results in.
"""

from __future__ import annotations

from typing import Any, TypedDict

from support_agent.intake.context import TicketContext
from support_agent.knowledge.contracts import Retrieval
from support_agent.orchestrator.contracts import Decision


class OrchestratorState(TypedDict, total=False):
    context: TicketContext

    # every search the model ran, in order, kept whole for the audit trail
    retrievals: list[Retrieval]
    messages_so_far: list[Any]

    decision: Decision
    corrections: list[str]
    meta: dict[str, Any]
    failure: str | None