"""Urgent tickets: noticing them, and telling the people on call.

Two signals, kept apart so everyone can see which one fired:

    keyword   a word or phrase from the team's list appears in the customer's
              message -- a rule the team wrote, checked here in code
    agent     the agent judged it urgent from the content (Decision.urgent),
              with a one-line reason -- a model's call, said so in the email

Either one marks the draft urgent and emails every recipient on the list at
once, after the decision is made -- before any reviewer has opened it. There
is no limit per ticket: each urgent draft sends its own email.

Nothing here may stop a draft. Every failure is caught, logged, and recorded
against the email it concerns.
"""

from __future__ import annotations

import re

from ole5.config import get_settings
from ole5.db import audit, postgres
from ole5.knowledge import team_docs
from ole5.logging import get_logger
from ole5.notify import mail

log = get_logger(__name__)

MESSAGE_CHARS = 600


class UrgentError(ValueError):
    """A change that cannot be made. The message is shown to the person."""


# ---------------------------------------------------------------------------
# matching
# ---------------------------------------------------------------------------

_DIACRITICS = re.compile("[\u064b-\u0652\u0670\u0640]")    # harakat, tatweel
_ALEF = re.compile("[\u0622\u0623\u0625\u0671]")           # آ أ إ ٱ -> ا
_LATIN = re.compile(r"[A-Za-z]")


def normalise(text: str) -> str:
    """Compare text the way a person would: capitals, Arabic diacritics and
    the common spelling variants (أ/إ/آ, ى/ي, ة/ه) do not change a word."""
    t = _DIACRITICS.sub("", (text or "").casefold())
    t = _ALEF.sub("\u0627", t).replace("\u0649", "\u064a").replace("\u0629", "\u0647")
    return " ".join(t.split())


def matching_keywords(text: str, words: list[str]) -> list[str]:
    """The keywords that appear in the text.

    A keyword with Latin letters must appear as a whole word, so "down" does
    not fire on "download". An Arabic keyword may appear inside a word, since
    Arabic attaches و and ال to the front: "متوقف" is found in "والمتوقف".
    """
    hay = normalise(text)
    found = []
    for word in words:
        needle = normalise(word)
        if not needle:
            continue
        if _LATIN.search(needle):
            if re.search(r"(?<!\w)" + re.escape(needle) + r"(?!\w)", hay):
                found.append(word)
        elif needle in hay:
            found.append(word)
    return found


# ---------------------------------------------------------------------------
# recipients
# ---------------------------------------------------------------------------

def recipients() -> list[dict]:
    return postgres.query(
        """
        SELECT u.id, u.email, u.name, u.created_at,
               coalesce(r.display_name, r.email) AS added_by
        FROM urgent_recipients u LEFT JOIN reviewers r ON r.id = u.added_by
        WHERE u.active ORDER BY u.created_at
        """
    )


def add_recipient(email: str, name: str | None, reviewer_id: int) -> dict:
    address = (email or "").strip()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", address):
        raise UrgentError("that is not an email address")
    label = " ".join((name or "").split()) or None
    existing = postgres.query_one(
        "SELECT id, email, active FROM urgent_recipients WHERE lower(btrim(email)) = lower(%s)",
        (address,))
    if existing and existing["active"]:
        raise UrgentError(f"{existing['email']} is already a recipient")
    if existing:
        postgres.execute("UPDATE urgent_recipients SET active = true, name = coalesce(%s, name), "
                         "added_by = %s WHERE id = %s", (label, reviewer_id, existing["id"]))
        rid = existing["id"]
    else:
        rid = postgres.query_one(
            "INSERT INTO urgent_recipients (email, name, added_by) VALUES (%s, %s, %s) "
            "RETURNING id", (address, label, reviewer_id))["id"]
    audit.record(actor="reviewer", action="urgent_recipient_added", reasoning=address,
                 evidence={"recipient_id": rid, "reviewer": team_docs._who(reviewer_id)})
    return {"id": rid, "email": address, "name": label}


def remove_recipient(recipient_id: int, reviewer_id: int) -> None:
    row = postgres.query_one("SELECT email, active FROM urgent_recipients WHERE id = %s",
                             (recipient_id,))
    if row is None or not row["active"]:
        raise UrgentError("no such recipient")
    postgres.execute("UPDATE urgent_recipients SET active = false WHERE id = %s", (recipient_id,))
    audit.record(actor="reviewer", action="urgent_recipient_removed", reasoning=row["email"],
                 evidence={"recipient_id": recipient_id, "reviewer": team_docs._who(reviewer_id)})


# ---------------------------------------------------------------------------
# the email
# ---------------------------------------------------------------------------

def _email(ticket: dict, draft_id: int, keywords: list[str],
           agent_reason: str | None) -> tuple[str, str]:
    title = (ticket.get("title") or "").strip() or "(no subject)"
    subject = f"[URGENT] Ticket {ticket['ticket_number']} — {title}"[:200]

    why = []
    if keywords:
        why.append("Matched urgent keyword" + ("s" if len(keywords) > 1 else "") + ": "
                   + ", ".join(keywords))
    if agent_reason is not None:
        why.append("The agent judged this urgent (a model's call, not a rule): "
                   + (agent_reason or "no reason given"))

    message = (ticket.get("search_text") or "").strip()
    if len(message) > MESSAGE_CHARS:
        message = message[:MESSAGE_CHARS] + " …"
    link = get_settings().console_url
    lines = [
        f"Ticket {ticket['ticket_number']} looks urgent.",
        "",
        "Why:",
        *[f"  - {w}" for w in why],
        "",
        f"Customer: {ticket.get('requester_email') or 'unknown'}",
        f"Subject:  {title}",
        "",
        "Their message:",
        message or "(no text)",
        "",
    ]
    if link:
        lines.append(f"Review the draft: {link.rstrip('/')}/#draft-{draft_id}")
    lines.append("Sent by the support agent. Nothing has been sent to the customer.")
    return subject, "\n".join(lines)


def _send_all(draft_id: int, ticket: dict, subject: str, body: str) -> int:
    """One email per recipient, each recorded with what happened to it."""
    people = recipients()
    if not people:
        log.warning("urgent ticket but no recipients", extra={"draft": draft_id})
        return 0
    for p in people:
        status, error, message_id = "sent", None, None
        try:
            msg = mail.compose(p["email"], subject, body)
            message_id = msg["Message-ID"]
            status = "sent" if mail.deliver(msg) else "dry_run"
        except mail.NotConfigured as exc:
            status, error = "skipped", str(exc)
        except Exception as exc:
            status, error = "failed", f"{type(exc).__name__}: {exc}"[:500]
            log.exception("urgent email failed", extra={"draft": draft_id, "to": p["email"]})
        postgres.execute(
            """
            INSERT INTO urgent_emails (draft_id, ticket_id, to_address, subject, body,
                                       status, error, rfc_message_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (draft_id, ticket["id"], p["email"], subject, body, status, error, message_id),
        )
    return len(people)


# ---------------------------------------------------------------------------
# after a decision
# ---------------------------------------------------------------------------

def attach(draft_id: int, ticket_id: int, decision) -> bool:
    """Check a new draft for urgency; mark it and email if either signal fires.
    Returns whether it was urgent. Never raises."""
    try:
        ticket = postgres.query_one(
            "SELECT id, ticket_number, title, requester_email, search_text "
            "FROM tickets WHERE id = %s", (ticket_id,))
        if ticket is None:
            return False
        words = [k["keyword"] for k in team_docs.keywords()]
        found = matching_keywords(
            f"{ticket.get('title') or ''}\n{ticket.get('search_text') or ''}", words)
        by_agent = bool(getattr(decision, "urgent", False))
        agent_reason = (getattr(decision, "urgent_reason", None) or "").strip() if by_agent else None
        if not found and not by_agent:
            return False

        postgres.execute(
            """
            INSERT INTO draft_urgent (draft_id, by_keyword, keywords, by_agent, agent_reason)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (draft_id) DO UPDATE SET
                by_keyword = EXCLUDED.by_keyword, keywords = EXCLUDED.keywords,
                by_agent = EXCLUDED.by_agent, agent_reason = EXCLUDED.agent_reason
            """,
            (draft_id, bool(found), found, by_agent, agent_reason),
        )
        subject, body = _email(ticket, draft_id, found, agent_reason)
        sent_to = _send_all(draft_id, ticket, subject, body)
        log.info("urgent draft", extra={"draft": draft_id, "keywords": found,
                                        "by_agent": by_agent, "recipients": sent_to})
        return True
    except Exception:
        log.exception("urgent check failed", extra={"draft": draft_id})
        return False


def for_draft(draft_id: int) -> dict | None:
    """Why a draft is urgent and who was emailed, for the review page."""
    try:
        row = postgres.query_one(
            "SELECT by_keyword, keywords, by_agent, agent_reason, created_at "
            "FROM draft_urgent WHERE draft_id = %s", (draft_id,))
        if row is None:
            return None
        row["emails"] = postgres.query(
            "SELECT to_address, status, error, created_at FROM urgent_emails "
            "WHERE draft_id = %s ORDER BY id", (draft_id,))
        return row
    except Exception:
        log.exception("could not read urgency", extra={"draft": draft_id})
        return None
