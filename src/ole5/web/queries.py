"""Everything the console reads, and the few things it writes.

v1's rule was that this file only reads, so the console could not alter a case.
That rule cannot survive a review page -- approving is the point -- so the
narrower rule is: the only writes here are a reviewer's own decisions, and they
go through ole5.review.approve, which records who and why.

Table browsing is by name against a fixed list. A table not on the list cannot
be reached, whatever the query string says.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from ole5.db import postgres

# Not reviewers, and not sessions: one holds every reviewer's password hash,
# the other live sign-in tokens. Nobody needs to browse either, and anyone
# signed in can open this page.
TABLES = (
    "tickets", "articles", "drafts", "reviews",
    "documents", "otrs_outbox", "audit_log", "agent_options",
)

# Columns held back from the table view: a 40KB email body makes a row
# unreadable, and the detail panel shows everything anyway.
BULKY = {
    "articles": ("body_raw", "body_clean", "body_quoted", "body_signature"),
    "drafts": ("reply_body", "evidence", "reasoning"),
    "reviews": ("final",),
    "audit_log": ("evidence", "reasoning"),
}

SEARCHABLE = {
    "tickets": ("ticket_number", "title", "requester_email", "queue"),
    "articles": ("subject", "sender_address", "body_clean"),
    "drafts": ("summary", "queue", "subtype", "conclusions"),
    "reviews": ("justification", "comment"),
    "documents": ("filename",),
    "otrs_outbox": ("last_error", "otrs_article_id"),
    "audit_log": ("action", "actor"),
    "agent_options": ("kind", "value", "description"),
}


def _check(table: str) -> str:
    if table not in TABLES:
        raise ValueError(f"unknown table {table!r}")
    return table


def jsonable(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (dict, list)):
        return value
    return value


def _clean(rows: list[dict]) -> list[dict]:
    return [{k: jsonable(v) for k, v in row.items()} for row in rows]


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------

def pending_drafts() -> list[dict]:
    """What is waiting, newest first."""
    return _clean(postgres.query(
        """
        SELECT d.id, d.action, d.reply_kind, d.queue, d.next_state, d.type,
               d.subtype, d.priority, d.service, d.confidence, d.summary,
               d.created_at,
               t.ticket_number, t.title, t.requester_email, t.otrs_ticket_id,
               t.otrs_created_at,
               (u.draft_id IS NOT NULL) AS urgent,
               -- Flagged by the junk check, or sent to Junk by the agent itself.
               (coalesce(j.flagged, false)
                OR position('junk' in lower(coalesce(d.queue, ''))) > 0) AS junk
        FROM drafts d JOIN tickets t ON t.id = d.ticket_id
        LEFT JOIN draft_urgent u ON u.draft_id = d.id
        LEFT JOIN draft_junk j ON j.draft_id = d.id
        WHERE d.status = 'pending'
          -- Listed once its checks have run (similar is the last to finish),
          -- so it never appears without its urgency or junk flag. A draft
          -- older than five minutes is listed regardless: if a check died,
          -- the draft must not stay hidden.
          AND (d.similar_status IS NOT NULL
               OR d.created_at < now() - interval '5 minutes')
        -- Urgent first: they are the ones someone is waiting on.
        ORDER BY (u.draft_id IS NOT NULL) DESC, d.created_at DESC
        """
    ))


def draft(draft_id: int) -> dict | None:
    """One draft, with its ticket and the article it answers."""
    row = postgres.query_one(
        """
        SELECT d.*, t.ticket_number, t.title, t.requester_email,
               t.customer_id_otrs, t.customer_org, t.otrs_ticket_id,
               t.queue AS ticket_queue, t.state AS ticket_state,
               t.type AS ticket_type, t.priority AS ticket_priority,
               t.service AS ticket_service, t.sla AS ticket_sla,
               t.subtype AS ticket_subtype, t.conclusions AS ticket_conclusions,
               t.otrs_created_at
        FROM drafts d JOIN tickets t ON t.id = d.ticket_id
        WHERE d.id = %s
        """,
        (draft_id,),
    )
    if row is None:
        return None

    out = {k: jsonable(v) for k, v in row.items()}
    out["articles"] = _clean(postgres.query(
        """
        SELECT article_no, party, via, sender_name, sender_address, subject,
               body_clean, body_raw, otrs_created_at
        FROM articles WHERE ticket_id = %s
        ORDER BY article_no NULLS LAST, id
        """,
        (row["ticket_id"],),
    ))
    return out


def draft_history(ticket_id: int) -> list[dict]:
    """Every draft on this ticket, including superseded ones."""
    return _clean(postgres.query(
        """
        SELECT d.id, d.status, d.action, d.created_at,
               r.outcome, r.changed_fields, r.justification,
               rev.display_name AS reviewer
        FROM drafts d
        LEFT JOIN reviews r ON r.draft_id = d.id
        LEFT JOIN reviewers rev ON rev.id = r.reviewer_id
        WHERE d.ticket_id = %s
        ORDER BY d.id DESC
        """,
        (ticket_id,),
    ))


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------

def dashboard() -> dict[str, Any]:
    """The numbers worth watching.

    Clean-approval rate is the one that matters: it is what would eventually
    justify letting the agent act on some decisions alone.
    """
    counts = postgres.query_one(
        """
        SELECT
          count(*) FILTER (WHERE status = 'pending')     AS pending,
          count(*) FILTER (WHERE status = 'approved')    AS approved,
          count(*) FILTER (WHERE status = 'rejected')    AS rejected,
          count(*) FILTER (WHERE status = 'superseded')  AS superseded,
          count(*) FILTER (WHERE action = 'answer')      AS answers,
          count(*) FILTER (WHERE action = 'route')       AS routes,
          count(*) FILTER (WHERE action = 'answer'
                              AND reply_kind = 'ask_more') AS ask_more,
          count(*)                                       AS total
        FROM drafts
        """
    ) or {}

    outcomes = postgres.query_one(
        """
        SELECT
          count(*) FILTER (WHERE outcome = 'approve') AS clean,
          count(*) FILTER (WHERE outcome = 'edit')    AS edited,
          count(*) FILTER (WHERE outcome = 'reject')  AS rejected,
          count(*)                                    AS reviewed
        FROM reviews
        """
    ) or {}

    reviewed = outcomes.get("reviewed") or 0
    clean_rate = round(100 * (outcomes.get("clean") or 0) / reviewed) if reviewed else None

    corrected = postgres.query(
        """
        SELECT field, count(*) AS n
        FROM reviews, unnest(changed_fields) AS field
        GROUP BY field ORDER BY n DESC LIMIT 10
        """
    )

    latency = postgres.query_one(
        """
        SELECT round(avg(latency_ms)) AS avg_ms,
               round(max(latency_ms)) AS max_ms
        FROM drafts WHERE latency_ms IS NOT NULL
        """
    ) or {}

    waiting = postgres.query_one(
        """
        SELECT round(avg(extract(epoch FROM (r.created_at - d.created_at)) / 60)) AS avg_minutes
        FROM reviews r JOIN drafts d ON d.id = r.draft_id
        """
    ) or {}

    outbox = postgres.query_one(
        """
        SELECT count(*) FILTER (WHERE status = 'pending') AS pending,
               count(*) FILTER (WHERE status = 'sent')    AS sent,
               count(*) FILTER (WHERE status = 'failed')  AS failed
        FROM otrs_outbox
        """
    ) or {}

    by_queue = postgres.query(
        """
        SELECT queue, count(*) AS n FROM drafts
        GROUP BY queue ORDER BY n DESC
        """
    )

    corrections = postgres.query_one(
        """
        SELECT count(*) AS n FROM drafts
        WHERE jsonb_array_length(coalesce(evidence->'corrections', '[]'::jsonb)) > 0
        """
    ) or {}

    # Distribution of drafts by ticket type. The agent picks one of the
    # active type options per draft; this is the breakdown used for the
    # "Ticket types" donut. NULL/empty types are excluded -- they are
    # drafts that never reached the classification step.
    by_type = postgres.query(
        """
        SELECT type, count(*) AS n
        FROM drafts
        WHERE type IS NOT NULL AND type != ''
        GROUP BY type
        ORDER BY n DESC
        """
    )

    # Total tickets ever ingested. Headline number on the dashboard.
    tickets = postgres.query_one(
        "SELECT count(*) AS n FROM tickets"
    ) or {}

    # Daily ingest counts for the time-series chart. Counts tickets
    # per day for the last 14 days using otrs_created_at -- the
    # tickets table does not have a separate local created_at column,
    # but otrs_created_at is set when the ticket arrives in OTRS,
    # which is close enough to "when it was polled" (the poller
    # ingests tickets shortly after they arrive). generate_series
    # guarantees a row per day even on quiet days, so the line drops
    # to zero rather than skipping.
    ticket_history = postgres.query(
        """
        WITH days AS (
          SELECT generate_series(
            (current_date - interval '13 days')::timestamptz,
            current_date::timestamptz,
            interval '1 day'
          ) AS day
        ),
        ticket_counts AS (
          SELECT otrs_created_at::date AS day, count(*) AS n
          FROM tickets
          WHERE otrs_created_at >= current_date - interval '13 days'
          GROUP BY otrs_created_at::date
        )
        SELECT
          to_char(d.day, 'YYYY-MM-DD') AS day,
          coalesce(tc.n, 0) AS n
        FROM days d
        LEFT JOIN ticket_counts tc ON tc.day = d.day::date
        ORDER BY d.day
        """
    )

    return {
        "drafts": {k: jsonable(v) for k, v in counts.items()},
        "reviews": {**{k: jsonable(v) for k, v in outcomes.items()},
                    "clean_rate": clean_rate},
        "corrected_fields": _clean(corrected),
        "latency": {k: jsonable(v) for k, v in latency.items()},
        "waiting_minutes": jsonable(waiting.get("avg_minutes")),
        "outbox": {k: jsonable(v) for k, v in outbox.items()},
        "by_queue": _clean(by_queue),
        "by_type": _clean(by_type),
        "validator_corrections": jsonable(corrections.get("n")),
        "tickets_total": jsonable(tickets.get("n")),
        "ticket_history": _clean(ticket_history),
    }


# ---------------------------------------------------------------------------
# knowledge
# ---------------------------------------------------------------------------

def documents() -> list[dict]:
    return _clean(postgres.query(
        """
        SELECT d.id, d.filename, d.size_bytes, d.status, d.chunk_count,
               d.error, d.created_at, d.indexed_at, r.display_name AS uploaded_by
        FROM documents d LEFT JOIN reviewers r ON r.id = d.uploaded_by
        ORDER BY d.created_at DESC
        """
    ))


# ---------------------------------------------------------------------------
# the table browser
# ---------------------------------------------------------------------------

def table_index() -> list[dict]:
    """Every browsable table with its row count."""
    parts = " UNION ALL ".join(
        f"SELECT '{t}' AS name, count(*) AS rows FROM {t}" for t in TABLES
    )
    return _clean(postgres.query(f"SELECT * FROM ({parts}) x ORDER BY name"))


def columns(table: str) -> list[dict]:
    _check(table)
    return _clean(postgres.query(
        """
        SELECT column_name AS name, data_type AS type, is_nullable AS nullable
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        ORDER BY ordinal_position
        """,
        (table,),
    ))


def table_rows(table: str, *, limit: int = 50, offset: int = 0,
               search: str | None = None) -> dict[str, Any]:
    """A page of rows, newest first, with the bulky columns held back."""
    _check(table)
    hidden = set(BULKY.get(table, ()))
    names = [c["name"] for c in columns(table) if c["name"] not in hidden]
    select = ", ".join(f'"{n}"' for n in names)

    params: list[Any] = []
    where = ""
    if search:
        targets = SEARCHABLE.get(table, ())
        if targets:
            clauses = " OR ".join(f'"{c}"::text ILIKE %s' for c in targets)
            where = f"WHERE {clauses}"
            params += [f"%{search}%"] * len(targets)

    order = "id DESC" if any(c["name"] == "id" for c in columns(table)) else "1"
    params += [limit, offset]

    rows = postgres.query(
        f"SELECT {select}, count(*) OVER () AS _total FROM {table} "
        f"{where} ORDER BY {order} LIMIT %s OFFSET %s",
        params,
    )
    total = rows[0]["_total"] if rows else 0
    for r in rows:
        r.pop("_total", None)

    return {"rows": _clean(rows), "total": total,
            "columns": names, "hidden": sorted(hidden)}


def table_row(table: str, row_id: int) -> dict | None:
    """One row, everything on it.

    For audit_log, the raw `actor` column stores a role string ("reviewer",
    "agent", "system") -- it does not identify who. The reviews table holds
    the actual reviewer_id, joined through drafts. We LATERAL-join the
    latest review per draft so a row is never duplicated when a draft has
    more than one review (rejected then approved, etc.).

    Other tables go through the generic SELECT * path unchanged.
    """
    _check(table)

    if table == "audit_log":
        row = postgres.query_one(
            """
            SELECT al.*,
                   r.id             AS review_id,
                   r.outcome        AS review_outcome,
                   r.created_at     AS review_at,
                   rev.display_name AS reviewer_name,
                   rev.email        AS reviewer_email
            FROM audit_log al
            LEFT JOIN LATERAL (
              SELECT *
              FROM reviews r
              WHERE r.draft_id = al.draft_id
              ORDER BY r.id DESC
              LIMIT 1
            ) r ON true
            LEFT JOIN reviewers rev ON rev.id = r.reviewer_id
            WHERE al.id = %s
            """,
            (row_id,),
        )
    else:
        row = postgres.query_one(f"SELECT * FROM {table} WHERE id = %s", (row_id,))

    return {k: jsonable(v) for k, v in row.items()} if row else None


# ---------------------------------------------------------------------------
# the fingerprint the stream watches
# ---------------------------------------------------------------------------

def fingerprint() -> str:
    row = postgres.query_one(
        """
        SELECT
          (SELECT count(*) FROM drafts WHERE status = 'pending') AS pending,
          (SELECT max(id) FROM drafts)                            AS last_draft,
          (SELECT max(id) FROM reviews)                           AS last_review,
          (SELECT max(id) FROM documents)                         AS last_doc,
          (SELECT count(*) FROM documents)                        AS docs,
          (SELECT count(*) FROM documents WHERE status = 'pending') AS indexing,
          (SELECT count(*) FROM otrs_outbox WHERE status = 'pending') AS unsent,
          (SELECT max(updated_at) FROM agent_options)             AS options,
          -- Changes when a draft's checks finish, so the page lists it then.
          (SELECT count(*) FROM drafts
           WHERE status = 'pending' AND similar_status IS NOT NULL) AS ready
        """
    ) or {}
    return json.dumps(row, default=str, sort_keys=True)