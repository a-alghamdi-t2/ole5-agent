"""What the model must return, and what a decision is once validated.

The model fills Decision. Code then checks it against the option lists and the
rules that are not the model's to bend, and what comes out is what gets stored.
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Action(str, Enum):
    ANSWER = "answer"
    ROUTE = "route"


class ReplyKind(str, Enum):
    ANSWER = "answer"
    ASK_MORE = "ask_more"


class Service(str, Enum):
    OLE5 = "OLE5 / Ole5 New"
    RICH = "RiCH"


class Confidence(str, Enum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Decision(BaseModel):
    """One decision about one ticket. Every field, both actions.

    Only reply_body and reply_kind depend on the action; everything else is
    produced either way, because the ticket leaves the intake queue regardless
    and someone has to classify it.
    """

    model_config = ConfigDict(extra="ignore")

    action: Action
    reply_kind: ReplyKind | None = None
    reply_body: str | None = None

    queue: str
    next_state: str
    type: str
    subtype: str
    conclusions: str
    priority: str
    sla: str

    # A string rather than the Service enum: the list is configurable from the
    # console now, so a value added there must not fail parsing here. The
    # validator snaps it onto the active list.
    service: str
    issue_type: str = Field(description="Our own label, free text, for analytics")
    summary: str = Field(description="What the customer is asking, in one or two sentences")
    collected: dict[str, str] = Field(
        default_factory=dict,
        description="What the ticket already tells us: error text, sizes, names, dates",
    )
    missing: list[str] = Field(
        default_factory=list,
        description="What is still unknown. What ask_more asks for, or what the "
                    "receiving team will have to find out.",
    )
    note: str | None = Field(
        default=None,
        description="Anything worth telling the reviewer that no other field carries",
    )

    urgent: bool = Field(
        default=False,
        description="True when the ticket needs someone now: judged from impact, not tone",
    )
    urgent_reason: str | None = Field(
        default=None,
        description="One line saying why it is urgent, when urgent is true",
    )

    confidence: Confidence
    reasoning: str = Field(description="Why this decision, in two or three sentences")
    queue_reason: str = Field(description="One line: why this queue and not the others")
