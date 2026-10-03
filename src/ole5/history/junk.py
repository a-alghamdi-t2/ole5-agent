"""Does this ticket look like the junk the team has already thrown away?

A separate index from the precedent one (ole5.history.precedent): a different
question, asked first. Junk lives in its own Qdrant tenant, built from the same
cleaned search_text, so thousands of junk tickets can never crowd out the
precedents a reviewer needs. The plumbing is precedent's, reused.

Two signals, kept apart:

    score    how close the nearest junk tickets are, from the index
    sender   whether this sender's domain appears in junk and nowhere else,
             counted from the tickets themselves

Neither closes anything. The check is context: it lets the agent propose Junk
instead of routing spam to a team, and it shows the reviewer why. A person
approves every one.

Junk is the queue, not the type: 5,000 tickets sit in the Junk queue and only
4,213 of them carry type Junk.
"""

from __future__ import annotations

import json
import re
import hashlib
from dataclasses import dataclass, field

from ole5.config import get_settings
from ole5.db import postgres
from ole5.history.precedent import MESSAGE_CHARS, MIN_TEXT, _rag_settings, _tenant
from ole5.logging import get_logger

log = get_logger(__name__)

TENANT = "ole5-junk"
DOC_ID = "junk-tickets"

# Two definitions, for two jobs. Written without a % so they can sit in a
# query that also takes parameters: psycopg reads %j in "ILIKE '%junk%'" as a
# placeholder.
#
# IS_JUNK: everything in the Junk queue -- the team's rule is that anything
# there is junk. Kept out of the precedents and out of the "real" side of
# every comparison.
#
# IS_TRUE_JUNK: what the junk index learns from -- Junk tickets checked and
# kept, not a repeat of junk already learned (see check_pending). A ticket not
# checked yet is not learned from.
IS_JUNK = "position('junk' in lower(coalesce(queue, ''))) > 0"
IS_TRUE_JUNK = f"({IS_JUNK} AND junk_check = 'kept')"


@dataclass
class Match:
    ticket_number: str
    text: str
    score: float


@dataclass
class Verdict:
    """What the check found. score is the best match's, 0.0 with none."""

    score: float = 0.0
    real_score: float = 0.0     # the best match among real tickets, for comparison
    matches: list[Match] = field(default_factory=list)
    sender_junk: int = 0        # junk tickets from this sender's domain
    sender_real: int = 0        # real tickets from the same domain
    looks_like_junk: bool = False
    reason: str = ""


# ---------------------------------------------------------------------------
# the index
# ---------------------------------------------------------------------------

def wanted() -> dict[str, dict]:
    """What the junk index should hold: ticket number -> its entry. The kept
    Junk tickets with text. One description for the full build and for sync."""
    rows = postgres.query(
        f"""
        SELECT ticket_number, title, search_text FROM closed_tickets
        WHERE {IS_TRUE_JUNK} AND length(coalesce(search_text, '')) >= %s
        """,
        (MIN_TEXT,),
    )
    return {r["ticket_number"]: {
        "page_content": r["search_text"][:MESSAGE_CHARS],
        "metadata": {
            # The same shape as precedent: one entry per ticket, its number the
            # chunk_id, so the same ticket always gets the same id.
            "file_hash": DOC_ID,
            "chunk_id": r["ticket_number"],
            "document_name": f"junk ticket {r['ticket_number']}",
            "source_file": r["ticket_number"],
            "ticket_number": r["ticket_number"],
            "title": r["title"] or "",
        },
    } for r in rows}


def sync(refresh: set[str] | None = None) -> dict:
    """Add the kept tickets missing from the index, remove repeats (and
    anything else that should not be there), re-add `refresh`. No rebuild."""
    from ole5.history import index_sync

    return index_sync.sync(TENANT, DOC_ID, wanted(), refresh)


def compare() -> dict:
    """How far the junk index is from what it should hold. Changes nothing."""
    from ole5.history import index_sync

    return index_sync.compare(TENANT, set(wanted()))


def build_index() -> int:
    """Rebuild the whole junk index. Only needed when every entry's text
    changes at once; otherwise sync() keeps it current."""
    from ole5.history import index_sync

    data = list(wanted().values())
    if not data:
        return 0
    client, embedder, writer, rag_settings = index_sync._parts()
    try:
        writer.delete_documents([DOC_ID], TENANT, client, settings=rag_settings)
    except Exception:
        log.debug("nothing to clear")
    for i in range(0, len(data), index_sync.BATCH):
        writer.ingest(data[i:i + index_sync.BATCH], user_id=TENANT, client=client,
                      embedder=embedder, doc_id=DOC_ID, settings=rag_settings)
    log.info("junk indexed", extra={"tickets": len(data)})
    return len(data)


def index_size() -> int:
    """How many junk chunks are indexed. A count in Qdrant, no embedding."""
    try:
        from qdrant_client import QdrantClient
        from qdrant_client.http import models as qm

        from ole5.knowledge.ingest import CHUNK_COLLECTION

        return QdrantClient(url=get_settings().qdrant_url).count(
            collection_name=CHUNK_COLLECTION,
            count_filter=qm.Filter(must=[
                qm.FieldCondition(key="group_id", match=qm.MatchValue(value=TENANT)),
            ]),
            exact=True,
        ).count
    except Exception:
        log.exception("could not count the junk index")
        return 0


def search(text: str, limit: int = 3, exclude: str | None = None) -> list[Match]:
    """The closest junk tickets to this text. Empty when the index is empty
    or the search fails -- a ticket is never called junk by accident."""
    if not (text or "").strip():
        return []
    try:
        hits = _tenant(TENANT).search(text)
    except Exception:
        log.exception("junk search failed")
        return []

    out = []
    for hit in hits:
        meta = getattr(hit, "metadata", None) or {}
        number = meta.get("ticket_number", "?")
        if exclude and number == exclude:
            continue
        out.append(Match(number, (getattr(hit, "text", "") or "")[:300],
                         float(getattr(hit, "score", 0) or 0)))
        if len(out) >= limit:
            break
    return out


# ---------------------------------------------------------------------------
# which Junk tickets to learn from
# ---------------------------------------------------------------------------
#
# Messages sent many times -- reminder notices, surveys -- need learning once.
# So each Junk ticket is checked once, oldest first, and the result stored:
#   repeat  same fingerprint as a ticket already kept
#   kept    otherwise: learned from
# No search and no model: a fingerprint comparison in the database.

_URL = re.compile(r"https?://\S+|www\.\S+|\S+@\S+")
_DIGITS = re.compile(r"[0-9\u0660-\u0669\u06f0-\u06f9]+")
_NONWORD = re.compile(r"[^\w]+")


def fingerprint(text: str | None) -> str | None:
    """The message without what changes between copies of it: numbers (case
    and reference numbers, dates), links and addresses, punctuation, case and
    Arabic spelling variants. Two copies of one notice share a fingerprint."""
    from ole5.notify.urgent import normalise

    t = _URL.sub(" ", text or "")
    t = normalise(t)
    t = _DIGITS.sub(" ", t)
    t = " ".join(_NONWORD.sub(" ", t).replace("_", " ").split())
    if len(t) < 10:
        return None
    return hashlib.sha1(t.encode("utf-8")).hexdigest()


def check_one(row: dict) -> tuple[str, str | None, float | None, str | None]:
    """(verdict, ref, score, fingerprint) for one Junk ticket."""
    fp = fingerprint(row["search_text"])
    if fp:
        twin = postgres.query_one(
            "SELECT ticket_number FROM closed_tickets WHERE junk_check = 'kept' "
            "AND junk_fingerprint = %s LIMIT 1", (fp,))
        if twin:
            return ("repeat", twin["ticket_number"], None, fp)
    return ("kept", None, None, fp)


def check_pending(limit: int | None = None, progress=None) -> dict:
    """Check the Junk tickets not checked yet, oldest first, so the first copy
    of a repeated message is the one kept. Each result is stored as it is made,
    so a stopped run resumes where it left off."""
    sql = (f"SELECT id, ticket_number, search_text FROM closed_tickets "
           f"WHERE {IS_JUNK} AND junk_check IS NULL "
           f"ORDER BY otrs_closed_at NULLS FIRST, id")
    rows = postgres.query(sql + (" LIMIT %s" if limit else ""), (limit,) if limit else ())
    counts = {"kept": 0, "repeat": 0}
    for i, row in enumerate(rows, 1):
        verdict, ref, score, fp = check_one(row)
        postgres.execute(
            "UPDATE closed_tickets SET junk_check = %s, junk_check_ref = %s, "
            "junk_check_score = %s, junk_fingerprint = %s, junk_checked_at = now() "
            "WHERE id = %s", (verdict, ref, score, fp, row["id"]))
        counts[verdict] += 1
        if progress:
            progress(i, len(rows), row["ticket_number"], verdict, ref, score)
    return {"checked": len(rows), **counts}


# ---------------------------------------------------------------------------
# the sender
# ---------------------------------------------------------------------------

def sender_counts(domain: str | None) -> tuple[int, int]:
    """(junk, real) closed tickets from this domain."""
    if not domain:
        return (0, 0)
    row = postgres.query_one(
        f"""
        SELECT count(*) FILTER (WHERE {IS_TRUE_JUNK}) AS junk,
               count(*) FILTER (WHERE NOT ({IS_JUNK})) AS real
        FROM closed_tickets
        WHERE split_part(lower(coalesce(requester_email, '')), '@', 2) = %s
        """,
        (domain.lower(),),
    )
    return (row["junk"], row["real"]) if row else (0, 0)


# ---------------------------------------------------------------------------
# the check
# ---------------------------------------------------------------------------

def real_score(text: str, exclude: str | None = None) -> float:
    """How close the nearest real (precedent) ticket is. The same reranker
    scores both indexes, so the two numbers can be compared directly."""
    from ole5.history import precedent

    precedent.exclude_ticket(exclude)
    try:
        hits = precedent.search(text, limit=1)
    finally:
        precedent.exclude_ticket(None)
    return hits[0].score if hits else 0.0


def decide(junk_score: float, real_score: float) -> str | None:
    """Which text rule a pair of scores meets: "duplicate", "closer", or None.

    One function for the live check and for calibration, so the threshold is
    measured on exactly the rule it is applied with.

      duplicate  junk_score reaches junk_threshold and beats real_score. The
                 second half matters: the same message is sometimes junked
                 once and handled properly another time, and then it is a
                 near-duplicate of both. That is not evidence of junk.
      closer     junk_score reaches junk_floor and beats real_score by
                 junk_margin: reworded spam of a known kind.
    """
    s = get_settings()
    if junk_score >= s.junk_threshold and junk_score > real_score:
        return "duplicate"
    if junk_score >= s.junk_floor and junk_score - real_score >= s.junk_margin:
        return "closer"
    return None


def check(text: str, requester_email: str | None = None, *,
          exclude: str | None = None) -> Verdict:
    """Does this ticket look like junk? Never raises.

    Three ways to be flagged, any one enough:

      near-duplicate  its best junk match reaches junk_threshold -- spam we
                      have seen almost word for word
      closer to junk  its best junk match reaches junk_floor and beats its
                      best real match by junk_margin -- the same kind of spam,
                      reworded. A real ticket that resembles junk nearly always
                      resembles a real ticket more, so it is not flagged.
      sender          its domain has sent enough junk and nothing real
    """
    s = get_settings()
    try:
        matches = search(text, exclude=exclude)
        score = matches[0].score if matches else 0.0
        real = real_score(text, exclude=exclude) if matches else 0.0
        domain = (requester_email or "").rsplit("@", 1)[-1].strip().lower() or None
        junk_n, real_n = sender_counts(domain)

        rule = decide(score, real) if matches else None
        by_text = rule == "duplicate"
        by_margin = rule == "closer"
        # A domain that has only ever sent junk, and enough of it to mean
        # something: one junk ticket from a domain proves nothing.
        by_sender = junk_n >= s.junk_sender_min and real_n == 0

        reasons = []
        if by_text:
            reasons.append(f"resembles junk ticket {matches[0].ticket_number} "
                           f"(score {score:.2f})")
        if by_margin:
            reasons.append(f"closer to junk ticket {matches[0].ticket_number} "
                           f"({score:.2f}) than to any real ticket ({real:.2f})")
        if by_sender:
            reasons.append(f"{domain} has sent {junk_n} junk tickets and no real ones")

        return Verdict(score=score, real_score=real, matches=matches, sender_junk=junk_n,
                       sender_real=real_n, looks_like_junk=bool(reasons),
                       reason="; ".join(reasons))
    except Exception:
        log.exception("junk check failed")
        return Verdict()


def attach(draft_id: int, ticket_id: int) -> Verdict:
    """Check a new draft's ticket and store the result. Never raises: a draft
    without a junk check is still a draft."""
    v = Verdict()
    try:
        row = postgres.query_one(
            "SELECT search_text, requester_email FROM tickets WHERE id = %s",
            (ticket_id,))
        if row is None:
            return v
        v = check(row["search_text"], row["requester_email"])
        postgres.execute(
            """
            INSERT INTO draft_junk (draft_id, score, matches, sender_junk,
                                    sender_real, flagged, reason, threshold)
            VALUES (%s, %s, %s::jsonb, %s, %s, %s, %s, %s)
            ON CONFLICT (draft_id) DO UPDATE SET
                score = EXCLUDED.score, matches = EXCLUDED.matches,
                sender_junk = EXCLUDED.sender_junk, sender_real = EXCLUDED.sender_real,
                flagged = EXCLUDED.flagged, reason = EXCLUDED.reason,
                threshold = EXCLUDED.threshold, created_at = now()
            """,
            (draft_id, v.score,
             json.dumps([{"ticket_number": m.ticket_number, "score": m.score,
                          "text": m.text} for m in v.matches], ensure_ascii=False),
             v.sender_junk, v.sender_real, v.looks_like_junk, v.reason,
             get_settings().junk_threshold),
        )
        if v.looks_like_junk:
            log.info("draft flagged as junk", extra={"draft": draft_id,
                                                     "score": round(v.score, 2)})
    except Exception:
        log.exception("junk check failed", extra={"draft": draft_id})
    return v


def for_draft(draft_id: int) -> dict | None:
    """The stored check, for the review page. None when there is none."""
    try:
        return postgres.query_one(
            "SELECT score, matches, sender_junk, sender_real, flagged, reason "
            "FROM draft_junk WHERE draft_id = %s", (draft_id,))
    except Exception:
        log.exception("could not read the junk check", extra={"draft": draft_id})
        return None
