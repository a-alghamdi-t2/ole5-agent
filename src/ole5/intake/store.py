"""Writing an incoming ticket into our mirror.

Idempotent by their identifiers: re-posting the same ticket updates its fields
and adds only articles we have not seen. Nothing here decides anything -- the
scope stays 'unknown' until the orchestrator reads the content.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ole5.config import get_settings
from ole5.db import postgres
from ole5.intake.contracts import IncomingTicket, _parse_otrs_time
from ole5.logging import get_logger
from ole5.intake.cleaner import clean as clean_body
from ole5.intake import search_text

log = get_logger(__name__)


@dataclass
class StoredTicket:
    ticket_id: int
    ticket_number: str
    articles_inserted: int
    articles_skipped: int
    is_new: bool


def upsert(ticket: IncomingTicket) -> StoredTicket:
    tz = get_settings().timezone

    with postgres.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM tickets WHERE otrs_ticket_id = %s",
                        (ticket.TicketID,))
            is_new = cur.fetchone() is None

            cur.execute(
                """
                INSERT INTO tickets (
                    otrs_ticket_id, ticket_number, title,
                    queue, state, type, priority, service, sla, lock,
                    owner, responsible, subtype, conclusions,
                    requester_email, customer_id_otrs, customer_email,
                    customer_org, otrs_created_at
                ) VALUES (
                    %s, %s, %s,
                    %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s,
                    %s, %s, %s,
                    %s, %s
                )
                ON CONFLICT (otrs_ticket_id) DO UPDATE SET
                    title = EXCLUDED.title,
                    queue = EXCLUDED.queue,
                    state = EXCLUDED.state,
                    type = EXCLUDED.type,
                    priority = EXCLUDED.priority,
                    service = EXCLUDED.service,
                    sla = EXCLUDED.sla,
                    lock = EXCLUDED.lock,
                    owner = EXCLUDED.owner,
                    responsible = EXCLUDED.responsible,
                    subtype = EXCLUDED.subtype,
                    conclusions = EXCLUDED.conclusions,
                    last_synced_at = now()
                RETURNING id
                """,
                (
                    ticket.TicketID, ticket.TicketNumber, ticket.Title,
                    ticket.Queue, ticket.State, ticket.Type, ticket.Priority,
                    ticket.Service, ticket.SLA, ticket.Lock,
                    ticket.Owner, ticket.Responsible,
                    ticket.dynamic("SubType"), ticket.dynamic("Conclusions"),
                    ticket.requester_email,
                    ticket.CustomerID,
                    # Who OTRS says the ticket belongs to. Usually the same as
                    # requester_email, but not on a forwarded ticket -- and the
                    # difference is worth being able to see.
                    ticket.CustomerUserID,
                    ticket.customer_org,
                    ticket.created_at(tz),
                ),
            )
            ticket_id = cur.fetchone()["id"]

            first = ticket.first_article
            first_id = first.ArticleID if first else None
            inserted = skipped = 0
            request_is_new = False

            for article in ticket.Article:
                sender_name, sender_address = article.sender
                parts = clean_body(article.Body)

                cur.execute(
                    """
                    INSERT INTO articles (
                        ticket_id, otrs_article_id, article_no, party, via,
                        sender_name, sender_address, recipients, cc, subject,
                        body_raw, body_clean, body_quoted, body_signature,
                        attachments, otrs_created_at
                    ) VALUES (
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s, %s,
                        %s, %s, %s, %s,
                        %s::jsonb, %s
                    )
                    ON CONFLICT (otrs_article_id) DO NOTHING
                    RETURNING id
                    """,
                    (
                        ticket_id, article.ArticleID, article.ArticleNumber,
                        "request" if article.ArticleID == first_id else "other",
                        "Email" if article.CommunicationChannelID == "1" else "OTRS",
                        sender_name, sender_address, article.To, article.Cc,
                        article.Subject,
                        article.Body, parts.clean, parts.quoted, parts.signature,
                        json.dumps(article.Attachment),
                        _parse_otrs_time(article.CreateTime, tz),
                    ),
                )
                if cur.fetchone() is None:
                    skipped += 1
                else:
                    inserted += 1
                    if article.ArticleID == first_id:
                        request_is_new = True

    # After the commit, not inside it: the text is derived, and a failure to
    # build it must never lose the ticket. fill_ticket does not raise.
    if request_is_new:
        search_text.fill_ticket(ticket_id)

    log.info(
        "ticket stored",
        extra={"ticket": ticket.TicketNumber, "new": is_new,
               "articles_in": inserted, "articles_known": skipped},
    )
    return StoredTicket(ticket_id, ticket.TicketNumber, inserted, skipped, is_new)