"""What passes between the nodes.

One dict, added to as it goes. Nothing here reads the database or calls out;
the nodes do that and put their results in.
"""

from __future__ import annotations

from typing import Any, TypedDict

from ole5.intake.context import TicketContext
from ole5.knowledge.contracts import Retrieval
from ole5.orchestrator.contracts import Decision


class OrchestratorState(TypedDict, total=False):
    context: TicketContext

    # every search the model ran, in order, kept whole for the audit trail
    retrievals: list[Retrieval]
    messages_so_far: list[Any]

    decision: Decision
    corrections: list[str]
    meta: dict[str, Any]
    failure: str | None