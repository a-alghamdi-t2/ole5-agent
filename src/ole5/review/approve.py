"""Approving a draft, and writing it to OTRS.

Two steps, deliberately separate. Approving records the decision and queues the
write; the writer sends it. A reviewer's click is never blocked on their network
being up, and a failed write is retried without asking them again.

The outbox is what makes a retry safe. One row per draft, UNIQUE on draft_id, so
a second attempt cannot post the note twice -- which is the one mistake that
cannot be taken back once a support agent has read it.

TicketUpdate is not atomic: a rejected dynamic field still leaves the queue
moved. So a retry must expect a ticket that is already half-changed, and setting
the same values again is harmless.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from ole5 import options
from ole5.history import junk, similar
from ole5.notify import urgent
from ole5.clients.otrs import OtrsClient, OtrsError
from ole5.config import get_settings
from ole5.db import audit, postgres
from ole5.logging import get_logger
from ole5.orchestrator import note as note_builder
from ole5.orchestrator.contracts import Decision

log = get_logger(__name__)

DECISION_FIELDS = (
    "action", "reply_kind", "reply_body", "queue", "queue_reason", "next_state",
    "type", "subtype", "conclusions", "priority", "sla", "service",
    "issue_type", "summary", "collected", "missing", "note", "confidence",
    "reasoning",
)


class NotPending(RuntimeError):
    """Someone got there first, or the draft was superseded."""


@dataclass
class Approval:
    draft_id: int
    review_id: int
    outbox_id: int
    changed_fields: list[str]


def _decision_from(row: dict) -> Decision:
    return Decision.model_validate({k: row[k] for k in DECISION_FIELDS})


def _changed(row: dict, final: dict) -> tuple[list[str], dict]:
    """Which fields the human altered, and what they were before.

    The names alone answer "what gets corrected"; the before and after answer
    "was the agent close", which is the more useful question and is not
    recoverable once a draft is superseded.
    """
    names, changes = [], {}
    for field in DECISION_FIELDS:
        if field in final and final[field] != row[field]:
            names.append(field)
            changes[field] = {"from": row[field], "to": final[field]}
    return names, changes


def _reviewer_name(reviewer_id: int) -> str | None:
    """For the audit trail. The actor column says which component acted; this
    says who, which is the question asked of a review months later."""
    row = postgres.query_one(
        "SELECT display_name, email FROM reviewers WHERE id = %s", (reviewer_id,)
    )
    if row is None:
        return None
    return row["display_name"] or row["email"]


def approve(draft_id: int, reviewer_id: int, *,
            final: dict | None = None,
            justification: str | None = None,
            comment: str | None = None) -> Approval:
    """Approve a draft and queue the write.

    `final` holds whatever the reviewer changed; anything absent keeps the
    agent's value. Passing nothing is a clean approval.

    Raises NotPending if the draft is no longer pending -- a new customer
    article may have superseded it while the reviewer was reading.
    """
    final = final or {}

    with postgres.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM drafts WHERE id = %s", (draft_id,))
            row = cur.fetchone()
            if row is None:
                raise NotPending(f"no draft {draft_id}")

            changed, changes = _changed(row, final)

            # A reviewer's edit must be a value that is active now. The agent's
            # own value is left alone even if it was disabled since the draft
            # was made: that is the agent's choice on record, and the reviewer
            # sees it marked as disabled in the dropdown.
            for field in changed:
                if field in options.KINDS and final[field] not in options.active(field):
                    raise ValueError(f"{final[field]!r} is not an active "
                                     f"{options.LABELS[field]} option")

            if changed and not justification:
                raise ValueError("a justification is required when anything "
                                 "was changed")

            # Conditional: only a pending draft can be approved. If it was
            # superseded a moment ago this updates nothing, and we say so
            # rather than writing a reply to a conversation that moved on.
            cur.execute(
                """
                UPDATE drafts SET status = 'approved'
                WHERE id = %s AND status = 'pending'
                """,
                (draft_id,),
            )
            if cur.rowcount == 0:
                raise NotPending(
                    f"draft {draft_id} is {row['status']}, not pending"
                )

            merged = {**{k: row[k] for k in DECISION_FIELDS}, **final}

            cur.execute(
                """
                INSERT INTO reviews (draft_id, reviewer_id, outcome, final,
                                     changed_fields, changes, justification,
                                     comment)
                VALUES (%s, %s, %s, %s::jsonb, %s, %s::jsonb, %s, %s)
                RETURNING id
                """,
                (draft_id, reviewer_id, "edit" if changed else "approve",
                 json.dumps(merged, ensure_ascii=False, default=str),
                 changed,
                 json.dumps(changes, ensure_ascii=False, default=str),
                 justification, comment),
            )
            review_id = cur.fetchone()["id"]

            cur.execute(
                """
                INSERT INTO otrs_outbox (draft_id, payload)
                VALUES (%s, %s::jsonb)
                RETURNING id
                """,
                (draft_id, json.dumps(merged, ensure_ascii=False, default=str)),
            )
            outbox_id = cur.fetchone()["id"]

    audit.record(actor="reviewer", action="approve", ticket_id=row["ticket_id"],
                 draft_id=draft_id,
                 reasoning=justification or comment,
                 evidence={"reviewer": _reviewer_name(reviewer_id),
                           "changed_fields": changed,
                           "changes": changes,
                           "review": review_id})

    log.info("approved", extra={"draft": draft_id, "review": review_id,
                                "changed": len(changed)})
    return Approval(draft_id, review_id, outbox_id, changed)


def reject(draft_id: int, reviewer_id: int, justification: str,
           comment: str | None = None) -> int:
    """Reject a draft. Nothing is written to OTRS."""
    if not justification or not justification.strip():
        raise ValueError("a justification is required to reject")

    with postgres.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT ticket_id, status FROM drafts WHERE id = %s",
                        (draft_id,))
            row = cur.fetchone()
            if row is None:
                raise NotPending(f"no draft {draft_id}")

            cur.execute(
                """
                UPDATE drafts SET status = 'rejected'
                WHERE id = %s AND status = 'pending'
                """,
                (draft_id,),
            )
            if cur.rowcount == 0:
                raise NotPending(f"draft {draft_id} is {row['status']}, not pending")

            cur.execute(
                """
                INSERT INTO reviews (draft_id, reviewer_id, outcome,
                                     justification, comment)
                VALUES (%s, %s, 'reject', %s, %s)
                RETURNING id
                """,
                (draft_id, reviewer_id, justification, comment),
            )
            review_id = cur.fetchone()["id"]

    audit.record(actor="reviewer", action="reject", ticket_id=row["ticket_id"],
                 draft_id=draft_id, reasoning=justification,
                 evidence={"reviewer": _reviewer_name(reviewer_id),
                           "review": review_id})
    log.info("rejected", extra={"draft": draft_id, "review": review_id})
    return review_id


def send_pending(*, limit: int = 20) -> tuple[int, int]:
    """Write approved drafts to OTRS. Returns (sent, failed).

    Safe to run repeatedly. A row is marked sent only when OTRS returns an
    ArticleID; anything else leaves it pending with the error recorded, and the
    next pass tries again.
    """
    settings = get_settings()
    rows = postgres.query(
        """
        SELECT o.id, o.draft_id, o.payload, o.attempts, t.otrs_ticket_id,
               t.ticket_number, d.evidence,
               -- The customer's original message: the request article
               -- with the lowest article_no on the ticket. Falls to NULL
               -- if no request article exists (rare, but possible if the
               -- ticket was created without one).
               (
                 SELECT body_clean FROM articles a
                 WHERE a.ticket_id = d.ticket_id AND a.party = 'request'
                 ORDER BY a.article_no NULLS LAST, a.id
                 LIMIT 1
               ) AS customer_message
        FROM otrs_outbox o
        JOIN drafts d ON d.id = o.draft_id
        JOIN tickets t ON t.id = d.ticket_id
        WHERE o.status = 'pending'
        ORDER BY o.created_at
        LIMIT %s
        """,
        (limit,),
    )
    if not rows:
        return 0, 0

    if settings.dry_run:
        log.warning("dry run: not writing to OTRS",
                    extra={"waiting": len(rows)})
        return 0, 0

    sent = failed = 0
    with OtrsClient() as otrs:
        for row in rows:
            payload = row["payload"]
            decision = Decision.model_validate(payload)
            body = note_builder.render(decision, draft_id=row["draft_id"],
                                       evidence=row["evidence"],
                                       message=row.get("customer_message"),
                                       similar=similar.for_note(row["draft_id"]),
                                       urgent=urgent.for_draft(row["draft_id"]),
                                       junk=junk.for_draft(row["draft_id"]))
            # Last check before writing: if someone moved or closed the ticket
            # in OTRS since the draft was made, writing now would undo their
            # work. Nothing is written; the reason is recorded.
            try:
                where = otrs.whereabouts(row["otrs_ticket_id"])
                why = otrs.left_intake(where)
            except OtrsError:
                why = None    # could not check: the write below reports any real error
            if why:
                postgres.execute(
                    "UPDATE otrs_outbox SET status = 'failed', attempts = attempts + 1, "
                    "last_error = %s WHERE id = %s",
                    (f"not written: the ticket was {why} after the draft was made", row["id"]))
                audit.record(actor="writer", action="not_written", draft_id=row["draft_id"],
                             reasoning=f"the ticket was {why} after the draft was made; "
                                       "nothing was written so their change stands",
                             evidence=where)
                log.warning("approval not written", extra={"ticket": row["ticket_number"], "why": why})
                failed += 1
                continue

            try:
                article_id = otrs.apply_decision(
                    row["otrs_ticket_id"],
                    note_subject=note_builder.subject(decision),
                    note_body=body,
                    queue=decision.queue,
                    state=decision.next_state,
                    type_=decision.type,
                    priority=decision.priority,
                    subtype=decision.subtype,
                    conclusions=decision.conclusions,
                )
            except OtrsError as exc:
                postgres.execute(
                    """
                    UPDATE otrs_outbox
                    SET status = 'pending', attempts = attempts + 1,
                        last_error = %s
                    WHERE id = %s
                    """,
                    (str(exc)[:500], row["id"]),
                )
                log.error("write failed", extra={"draft": row["draft_id"],
                                                 "ticket": row["ticket_number"],
                                                 "attempts": row["attempts"] + 1,
                                                 "error": str(exc)[:200]})
                failed += 1
                continue

            postgres.execute(
                """
                UPDATE otrs_outbox
                SET status = 'sent', attempts = attempts + 1,
                    otrs_article_id = %s, delivered_at = now(), last_error = NULL
                WHERE id = %s
                """,
                (article_id, row["id"]),
            )
            audit.record(actor="writer", action="posted",
                         draft_id=row["draft_id"],
                         evidence={"article_id": article_id})

            # Service and SLA, after the rest is safely written. A refusal is
            # recorded with OTRS's reason and does not fail the approval: the
            # note, queue and state are already on the ticket.
            try:
                otrs.set_service_sla(row["otrs_ticket_id"], service=decision.service,
                                     sla=decision.sla)
                audit.record(actor="writer", action="sla_set", draft_id=row["draft_id"],
                             reasoning=f"{decision.service} / {decision.sla}",
                             evidence={"service": decision.service, "sla": decision.sla})
            except OtrsError as exc:
                audit.record(actor="writer", action="sla_not_set", draft_id=row["draft_id"],
                             reasoning=str(exc)[:500],
                             evidence={"service": decision.service, "sla": decision.sla,
                                       "error": str(exc)[:500]})
                log.warning("service and sla not set", extra={
                    "draft": row["draft_id"], "ticket": row["ticket_number"],
                    "error": str(exc)[:200]})
            log.info("written", extra={"draft": row["draft_id"],
                                       "ticket": row["ticket_number"],
                                       "article": article_id})
            sent += 1

    return sent, failed
