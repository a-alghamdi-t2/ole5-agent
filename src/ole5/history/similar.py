"""Past tickets that resemble the one a draft was made for.

When a draft is made, its ticket's search_text is searched against the history
index (ole5.history.precedent) and the closest closed tickets are stored with
the draft in draft_similar. The review page shows them; the OTRS note lists
them.

Stored, not looked up on demand: what the reviewer saw next to the decision is
what the audit trail keeps, even after the index is rebuilt.

Nothing here may stop a draft. Every entry point catches its own failures and
returns an empty result: a draft without similar tickets is still a draft.
While the index is empty -- before it is first built, or while the embedding
service is down and it cannot be -- attach() returns at once, without calling
the embedding service at all.
"""

from __future__ import annotations

from ole5.config import get_settings
from ole5.db import postgres
from ole5.history import precedent
from ole5.logging import get_logger

log = get_logger(__name__)


def find(text: str, *, limit: int | None = None,
         exclude: str | None = None, strict: bool = False) -> list[dict]:
    """The closest closed tickets to this text, as rows ready to show.

    Empty when the index is empty or the search fails. exclude leaves one
    ticket number out -- a closed ticket searched with its own text would
    otherwise come back first.
    """
    limit = limit or get_settings().similar_limit
    if not (text or "").strip() or precedent.index_size() == 0:
        return []

    precedent.exclude_ticket(exclude)
    try:
        hits = precedent.search(text, limit=limit, strict=strict)
    finally:
        precedent.exclude_ticket(None)
    if not hits:
        return []

    numbers = [h.ticket_number for h in hits]
    rows = {r["ticket_number"]: r for r in postgres.query(
        """
        SELECT id, ticket_number, title, queue, type, subtype, otrs_closed_at
        FROM closed_tickets WHERE ticket_number = ANY(%s)
        """,
        (numbers,),
    )}
    # Only what clears the threshold. When nothing is really alike, the search
    # still returns its least-bad matches, scoring near zero; those would show
    # a reviewer unrelated tickets as if they were precedents.
    floor = get_settings().similar_min_score
    out = []
    for h in hits:
        row = rows.get(h.ticket_number)
        if row is None:          # indexed but no longer in the table
            continue
        if (h.score or 0) < floor:
            continue
        out.append({**row, "rank": len(out) + 1, "score": h.score})
    return out


def _mark(draft_id: int, status: str) -> None:
    postgres.execute("UPDATE drafts SET similar_status = %s WHERE id = %s",
                     (status, draft_id))


def attach(draft_id: int, ticket_id: int) -> int:
    """Find and store the similar tickets for a new draft. Returns how many.

    Records on the draft what happened (drafts.similar_status), so the review
    page can say "the search failed" rather than "none found" when the
    embedding service was down. Never raises.
    """
    try:
        row = postgres.query_one("SELECT search_text FROM tickets WHERE id = %s",
                                 (ticket_id,))
        text = (row or {}).get("search_text") or ""
        if not text.strip():
            _mark(draft_id, "none")
            return 0
        if precedent.index_size() == 0:
            _mark(draft_id, "no_index")
            return 0
        try:
            matches = find(text, strict=True)
        except Exception:
            _mark(draft_id, "failed")
            return 0
        if not matches:
            _mark(draft_id, "none")
            return 0
        with postgres.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO draft_similar (draft_id, closed_ticket_id, rank, score)
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT (draft_id, closed_ticket_id) DO NOTHING
                    """,
                    [(draft_id, m["id"], m["rank"], m["score"]) for m in matches],
                )
        _mark(draft_id, "found")
        log.info("similar attached", extra={"draft": draft_id, "count": len(matches)})
        return len(matches)
    except Exception:
        log.exception("similar tickets failed", extra={"draft": draft_id})
        # Still mark it: the review page waits for a status before listing
        # the draft, and must not wait forever.
        try:
            _mark(draft_id, "failed")
        except Exception:
            log.exception("could not mark similar status", extra={"draft": draft_id})
        return 0


def for_draft(draft_id: int) -> list[dict]:
    """The stored matches for one draft, for the review page."""
    try:
        rows = postgres.query(
            """
            SELECT s.rank, s.score, c.ticket_number, c.title, c.queue, c.type,
                   c.subtype, c.otrs_closed_at, left(c.search_text, 600) AS text,
                   -- The route it took, from OTRS's own queue notifications,
                   -- and what the journey extraction made of it. Both empty
                   -- for tickets older than those notifications.
                   coalesce(tl.queue_path, '{}') AS path,
                   j.summary AS journey, coalesce(j.moves, '[]'::jsonb) AS moves
            FROM draft_similar s
            JOIN closed_tickets c ON c.id = s.closed_ticket_id
            LEFT JOIN ticket_timelines tl ON tl.closed_ticket_id = c.id
            LEFT JOIN ticket_journeys j ON j.closed_ticket_id = c.id
            WHERE s.draft_id = %s
            ORDER BY s.rank
            """,
            (draft_id,),
        )
        return rows
    except Exception:
        log.exception("could not read similar tickets", extra={"draft": draft_id})
        return []


def for_note(draft_id: int) -> list[dict]:
    """The same, trimmed to what the OTRS note prints."""
    return [{"ticket_number": r["ticket_number"], "queue": r["queue"],
             "title": r["title"], "path": r.get("path") or [],
             "journey": r.get("journey")}
            for r in for_draft(draft_id)]


def past_ticket(ticket_number: str) -> dict | None:
    """Everything about one closed ticket, for the viewer the review page
    opens when a similar ticket (or a junk match) is clicked: its details, the
    route it took, what happened on it, and its cleaned conversation.

    Junk tickets have no timeline or journey -- they were never extracted --
    so for them the cleaned first message stands alone.
    """
    return postgres.query_one(
        """
        SELECT c.ticket_number, c.title, c.queue, c.type, c.subtype,
               c.otrs_created_at, c.otrs_closed_at, c.search_text,
               position('junk' in lower(coalesce(c.queue, ''))) > 0 AS junk,
               coalesce(tl.queue_path, '{}') AS path,
               tl.entries,
               j.summary AS journey, coalesce(j.steps, '[]'::jsonb) AS steps,
               coalesce(j.moves, '[]'::jsonb) AS moves
        FROM closed_tickets c
        LEFT JOIN ticket_timelines tl ON tl.closed_ticket_id = c.id
        LEFT JOIN ticket_journeys j ON j.closed_ticket_id = c.id
        WHERE c.ticket_number = %s
        """,
        (ticket_number,),
    )
