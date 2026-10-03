"""The shape of a ticket as their API returns it.

Written against a real TicketGet response, not a guess. Three things about it
are easy to get wrong:

  - the response wraps the ticket in a list, even for one ticket
  - there is no customer object; TicketGet gives CustomerID and CustomerUserID
    and nothing else, so the name and organisation shown in their UI come from
    a lookup this API does not expose
  - every value is a string, including numbers, booleans and timestamps

Unknown fields are kept, not rejected. Their response carries far more than we
read, and a payload with an extra key is not an error.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _parse_otrs_time(value: str | None, tz: str) -> datetime | None:
    """OTRS returns naive strings like "2025-05-21 10:50:05", in its own zone."""
    if not value or not value.strip():
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value.strip(), fmt).replace(tzinfo=ZoneInfo(tz))
        except ValueError:
            continue
    return None


class OtrsDynamicField(BaseModel):
    model_config = ConfigDict(extra="allow")

    Name: str
    Value: str | None = None


class IncomingArticle(BaseModel):
    model_config = ConfigDict(extra="allow")

    ArticleID: str
    ArticleNumber: int | None = None

    # Their SenderType means "came in from outside", not "is the customer": a
    # colleague replying by email to a ticket is recorded as customer. Kept for
    # the record; nothing decides on it.
    SenderType: str | None = None
    SenderTypeID: str | None = None
    CommunicationChannelID: str | None = None

    # "0" marks an article the customer cannot see -- an internal note or an
    # OTRS notification.
    IsVisibleForCustomer: str | None = None

    From: str | None = None
    To: str | None = None
    Cc: str | None = None
    Subject: str | None = None
    Body: str = ""
    MimeType: str | None = None
    ContentType: str | None = None
    MessageID: str | None = None
    InReplyTo: str | None = None
    CreateTime: str | None = None
    Attachment: list[dict] = Field(default_factory=list)

    @field_validator("ArticleID", "SenderTypeID", "CommunicationChannelID",
                     "IsVisibleForCustomer", mode="before")
    @classmethod
    def _stringify(cls, v: object) -> object:
        return str(v) if v is not None else v

    @property
    def visible_to_customer(self) -> bool:
        return self.IsVisibleForCustomer != "0"

    @property
    def sender(self) -> tuple[str | None, str | None]:
        """(display name, address) from a From header. Either may be absent."""
        raw = (self.From or "").strip()
        if not raw:
            return None, None
        if "<" in raw and ">" in raw:
            name = raw[: raw.index("<")].strip().strip('"') or None
            address = raw[raw.index("<") + 1 : raw.index(">")].strip().lower() or None
            return name, address
        return None, raw.lower()


class IncomingTicket(BaseModel):
    """One ticket as OTRS holds it, with its articles."""

    model_config = ConfigDict(extra="allow")

    TicketID: str
    TicketNumber: str
    Title: str | None = None

    Queue: str | None = None
    QueueID: str | None = None
    State: str | None = None
    StateType: str | None = None
    Type: str | None = None
    Priority: str | None = None
    Lock: str | None = None
    Owner: str | None = None
    Responsible: str | None = None

    # Empty on every ticket we have looked at, worked or not. Kept because the
    # fields exist, not because anyone fills them in.
    Service: str | None = None
    SLA: str | None = None

    CustomerID: str | None = None
    CustomerUserID: str | None = None

    Created: str | None = None
    Changed: str | None = None

    # Unix seconds. Present even with no SLA set, so escalation is configured
    # at the queue.
    EscalationResponseTime: str | None = None
    EscalationUpdateTime: str | None = None
    EscalationSolutionTime: str | None = None

    DynamicField: list[OtrsDynamicField] = Field(default_factory=list)
    Article: list[IncomingArticle] = Field(default_factory=list)

    @field_validator("TicketID", "TicketNumber", "QueueID", mode="before")
    @classmethod
    def _stringify(cls, v: object) -> object:
        return str(v) if v is not None else v

    def dynamic(self, name: str) -> str | None:
        for f in self.DynamicField:
            if f.Name == name:
                return f.Value or None
        return None

    def created_at(self, tz: str) -> datetime | None:
        return _parse_otrs_time(self.Created, tz)

    def escalation_at(self, which: str, tz: str) -> datetime | None:
        """One of response / update / solution, as a datetime.

        Their value is unix seconds in a string. Zero means unset.
        """
        raw = getattr(self, f"Escalation{which.capitalize()}Time", None)
        if not raw or raw in ("0", ""):
            return None
        try:
            return datetime.fromtimestamp(int(raw), ZoneInfo(tz))
        except (ValueError, OSError):
            return None

    @property
    def first_article(self) -> IncomingArticle | None:
        """The request. Sorted rather than taken by position, because array
        order is not something their API promises."""
        if not self.Article:
            return None
        return sorted(
            self.Article,
            key=lambda a: (a.ArticleNumber if a.ArticleNumber is not None else 10**9,
                           int(a.ArticleID) if a.ArticleID.isdigit() else 0),
        )[0]

    @property
    def requester_email(self) -> str:
        """Whoever the ticket belongs to.

        CustomerUserID is their record of it and is an address here. Falls back
        to parsing the first article's From when it is not.
        """
        if self.CustomerUserID and "@" in self.CustomerUserID:
            return self.CustomerUserID.strip().lower()
        first = self.first_article
        return (first.sender[1] if first else None) or ""

    @property
    def customer_org(self) -> str | None:
        """Their CustomerID sometimes carries the organisation name after a
        dash: "SOJ - المكتب الاستراتيجي في الجوف". Split it out when it does."""
        if not self.CustomerID:
            return None
        if " - " in self.CustomerID:
            return self.CustomerID.split(" - ", 1)[1].strip() or None
        return None


class TicketGetResponse(BaseModel):
    """What TicketGet returns. The ticket is wrapped in a list, always."""

    model_config = ConfigDict(extra="allow")

    Ticket: list[IncomingTicket] = Field(min_length=1)

    @property
    def ticket(self) -> IncomingTicket:
        return self.Ticket[0]


class IngestRequest(BaseModel):
    """The POST body for the entry point.

    Accepts what TicketGet returns, so a payload captured from their API can be
    replayed here unchanged. A bare object is accepted too, for hand-written
    fixtures.
    """

    model_config = ConfigDict(extra="allow")

    Ticket: list[IncomingTicket] | IncomingTicket

    @property
    def ticket(self) -> IncomingTicket:
        if isinstance(self.Ticket, list):
            return self.Ticket[0]
        return self.Ticket