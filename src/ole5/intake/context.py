"""What the orchestrator reads.

Assembles one ticket from our mirror into a single object: the ticket's own
fields, whatever is known about the customer, the request, and the other
articles as background.

Reads. Decides nothing. The scope stays whatever intake stored, and no text is
altered here beyond what the cleaner already separated.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ole5.db import postgres
from ole5.logging import get_logger

from zoneinfo import ZoneInfo
from ole5.config import get_settings

log = get_logger(__name__)

# How much of a background article to carry. They are context, not the question,
# and a long one should not crowd out the request.
BACKGROUND_CHARS = 600


@dataclass
class ArticleView:
    article_no: int | None
    sender_name: str | None
    sender_address: str | None
    subject: str | None
    body: str
    created_at: datetime | None
    attachments: list[dict] = field(default_factory=list)

    @property
    def sender(self) -> str:
        if self.sender_name and self.sender_address:
            return f"{self.sender_name} <{self.sender_address}>"
        return self.sender_address or self.sender_name or "unknown"


@dataclass
class Customer:
    email: str | None
    customer_id: str | None
    org: str | None

    @property
    def known(self) -> bool:
        return bool(self.customer_id or self.org)


@dataclass
class TicketContext:
    ticket_id: int
    ticket_number: str
    title: str | None
    queue: str | None
    state: str | None
    type: str | None
    priority: str | None
    service: str | None
    sla: str | None
    subtype: str | None
    conclusions: str | None
    created_at: datetime | None
    requester_email: str
    customer: Customer
    request: ArticleView
    background: list[ArticleView]

    def render(self) -> str:
        
        def show(value: str | None) -> str:
            return value if value else "(empty)"

        created = "(empty)"
        if self.created_at:
            local = self.created_at.astimezone(ZoneInfo(get_settings().timezone))
            created = local.strftime("%Y-%m-%d %H:%M") + " (Asia/Riyadh)"

        lines = [
            "TICKET",
            f"  Number:      {self.ticket_number}",
            f"  Title:       {show(self.title)}",
            f"  Queue:       {show(self.queue)}",
            f"  State:       {show(self.state)}",
            f"  Type:        {show(self.type)}",
            f"  Priority:    {show(self.priority)}",
            f"  Created:     {created}",
        ]

        for label, value in (
            ("Service", self.service),
            ("SLA", self.sla),
            ("SubType", self.subtype),
            ("Conclusions", self.conclusions),
        ):
            if value:
                lines.append(f"  {label}:{' ' * max(1, 12 - len(label))}{value}")

        lines += [
            "",
            "CUSTOMER",
            f"  Email:       {self.customer.email or self.requester_email}",
        ]
        if self.customer.customer_id:
            lines.append(f"  Customer ID: {self.customer.customer_id}")
        if self.customer.org and self.customer.org not in (self.customer.customer_id or ""):
            lines.append(f"  Organisation: {self.customer.org}")

        lines += [
            "",
            "REQUEST",
            f"  From:    {self.request.sender}",
            f"  Subject: {show(self.request.subject)}",
        ]
        if self.request.attachments:
            names = ", ".join(a.get("Filename", "?") for a in self.request.attachments)
            lines.append(f"  Attached: {names}")
        lines += ["", self.request.body.strip() or "(empty body)"]

        if self.background:
            lines += ["", f"ALREADY ON THE TICKET ({len(self.background)})",
                      "  Context only. Not from the customer; never answer these."]
            for a in self.background:
                body = a.body.strip()
                if len(body) > BACKGROUND_CHARS:
                    body = body[:BACKGROUND_CHARS] + " ...(truncated)"
                lines += [
                    "",
                    f"  [{a.article_no}] {a.sender} — {show(a.subject)}",
                    "  " + body.replace("\n", "\n  "),
                ]

        return "\n".join(lines)


def _to_article(row: dict) -> ArticleView:
    return ArticleView(
        article_no=row["article_no"],
        sender_name=row["sender_name"],
        sender_address=row["sender_address"],
        subject=row["subject"],
        # body_clean is null only if the cleaner never ran; fall back rather
        # than hand the model an empty request.
        body=row["body_clean"] or row["body_raw"] or "",
        created_at=row["otrs_created_at"],
        attachments=row["attachments"] or [],
    )


def build(ticket_id: int) -> TicketContext:
    """Assemble the context for one stored ticket. Raises if it is not there."""
    ticket = postgres.query_one(
        """
        SELECT id, ticket_number, title, queue, state, type, priority,
               service, sla, subtype, conclusions,
               otrs_created_at, requester_email, customer_id_otrs, customer_org
        FROM tickets WHERE id = %s
        """,
        (ticket_id,),
    )
    if ticket is None:
        raise LookupError(f"no ticket {ticket_id}")

    rows = postgres.query(
        """
        SELECT article_no, party, sender_name, sender_address, subject,
               body_raw, body_clean, attachments, otrs_created_at
        FROM articles
        WHERE ticket_id = %s
        ORDER BY article_no NULLS LAST, id
        """,
        (ticket_id,),
    )

    request_rows = [r for r in rows if r["party"] == "request"]
    if not request_rows:
        raise LookupError(f"ticket {ticket_id} has no request article")

    context = TicketContext(
        ticket_id=ticket["id"],
        ticket_number=ticket["ticket_number"],
        title=ticket["title"],
        queue=ticket["queue"],
        state=ticket["state"],
        type=ticket["type"],
        priority=ticket["priority"],
        service=ticket["service"],
        sla=ticket["sla"],
        subtype=ticket["subtype"],
        conclusions=ticket["conclusions"],
        created_at=ticket["otrs_created_at"],
        requester_email=ticket["requester_email"],
        customer=Customer(
            email=ticket["requester_email"],
            customer_id=ticket["customer_id_otrs"],
            org=ticket["customer_org"],
        ),
        request=_to_article(request_rows[0]),
        background=[_to_article(r) for r in rows if r["party"] != "request"],
    )

    log.debug(
        "context built",
        extra={"ticket": context.ticket_number,
               "background": len(context.background),
               "chars": len(context.request.body)},
    )
    return context