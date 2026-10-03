"""Storing a closed ticket for extraction later.

Separate from the live mirror on purpose. `tickets` is what the agent works
from; this is history, read once and then read many times by whatever we build
on top of it.

Idempotent by their identifiers, so an interrupted export can be re-run.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ole5.config import get_settings
from ole5.db import postgres
from ole5.history.noise import is_noise
from ole5.intake.cleaner import clean as clean_body
from ole5.intake.contracts import IncomingTicket, _parse_otrs_time
from ole5.logging import get_logger

log = get_logger(__name__)


@dataclass
class StoredClosed:
    ticket_id: int
    ticket_number: str
    kept: int
    dropped: int


def save(ticket: IncomingTicket, raw: dict) -> StoredClosed:
    """Write one closed ticket and the articles somebody actually wrote."""
    tz = get_settings().timezone
    articles = raw.get("Article") or []
    real = [a for a in articles if not is_noise(a)]
    dropped = len(articles) - len(real)

    dynamic = {f.Name: f.Value for f in ticket.DynamicField if f.Value}

    with postgres.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO closed_tickets (
                    otrs_ticket_id, ticket_number, title,
                    queue, state, type, priority, service, sla,
                    owner, responsible, subtype, conclusions,
                    requester_email, customer_id_otrs, customer_org,
                    otrs_created_at, otrs_closed_at, solution_minutes,
                    article_count, noise_count, dynamic_fields
                ) VALUES (
                    %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s,
                    %s, %s, %s::jsonb
                )
                ON CONFLICT (otrs_ticket_id) DO UPDATE SET
                    article_count = EXCLUDED.article_count,
                    noise_count = EXCLUDED.noise_count,
                    fetched_at = now()
                RETURNING id
                """,
                (
                    ticket.TicketID, ticket.TicketNumber, ticket.Title,
                    ticket.Queue, ticket.State, ticket.Type, ticket.Priority,
                    ticket.Service, ticket.SLA,
                    ticket.Owner, ticket.Responsible,
                    ticket.dynamic("SubType"), ticket.dynamic("Conclusions"),
                    ticket.requester_email, ticket.CustomerID,
                    ticket.customer_org,
                    ticket.created_at(tz),
                    _parse_otrs_time(raw.get("Closed"), tz),
                    int(raw["SolutionInMin"]) if raw.get("SolutionInMin") else None,
                    len(articles), dropped,
                    json.dumps(dynamic, ensure_ascii=False),
                ),
            )
            ticket_id = cur.fetchone()["id"]

            kept = 0
            for a in real:
                sender = (a.get("From") or "").strip()
                name = address = None
                if "<" in sender and ">" in sender:
                    name = sender[: sender.index("<")].strip().strip('"') or None
                    address = sender[sender.index("<") + 1: sender.index(">")].strip().lower()
                elif sender:
                    address = sender.lower()

                parts = clean_body(a.get("Body") or "")

                cur.execute(
                    """
                    INSERT INTO closed_articles (
                        closed_ticket_id, otrs_article_id, article_no,
                        sender_type, channel_id, visible,
                        sender_name, sender_address, recipients, cc, subject,
                        body_raw, body_clean, body_quoted, body_signature,
                        attachments, otrs_created_at
                    ) VALUES (
                        %s, %s, %s,
                        %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s::jsonb, %s
                    )
                    ON CONFLICT (otrs_article_id) DO NOTHING
                    """,
                    (
                        ticket_id, str(a.get("ArticleID")), a.get("ArticleNumber"),
                        a.get("SenderType"), str(a.get("CommunicationChannelID")),
                        str(a.get("IsVisibleForCustomer")) != "0",
                        name, address, a.get("To"), a.get("Cc"), a.get("Subject"),
                        a.get("Body") or "", parts.clean, parts.quoted,
                        parts.signature,
                        json.dumps(a.get("Attachment") or []),
                        _parse_otrs_time(a.get("CreateTime"), tz),
                    ),
                )
                kept += 1

    return StoredClosed(ticket_id, ticket.TicketNumber, kept, dropped)