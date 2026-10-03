"""Read the intake queue, decide what is waiting, hold the drafts.

Nothing here writes to OTRS. A poll produces drafts and stops; the decision
reaches them only when a human approves it.

Idempotent by their identifiers. A ticket already drafted is skipped rather
than decided again -- polling is cheap, deciding is a minute of model time.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from ole5.clients.otrs import OtrsClient, OtrsError
from ole5.db import postgres
from ole5.intake import context as context_builder, store as intake_store
from ole5.logging import get_logger
from ole5.history import junk, similar
from ole5.notify import urgent
from ole5.orchestrator import store as draft_store
from ole5.db import audit
from ole5.orchestrator.graph import DecisionFailed, run

log = get_logger(__name__)


@dataclass
class PollResult:
    seen: int = 0
    drafted: int = 0
    skipped: int = 0
    failed: int = 0
    seconds: float = 0.0
    tickets: list[str] = field(default_factory=list)


def _already_drafted(ticket_id: int, newest_article_id: int | None) -> bool:
    """True when a draft already covers this ticket as it stands.

    A draft is stale once an article arrives after the one it was built
    from -- the customer wrote again, and the reply may no longer fit.
    Then we decide again and the old draft is superseded.

    A rejected or approved draft counts as "covering" the ticket: we do
    NOT re-draft just because the previous draft was rejected. Re-drafting
    happens only when a new article arrives, which makes any older draft
    stale regardless of its status. Without this, every poll after a
    rejection would produce another draft for the same ticket.
    """
    # Take the latest draft of any status. The built_to_article_id
    # comparison below decides whether the draft is still current.
    row = postgres.query_one(
        """
        SELECT built_to_article_id FROM drafts
        WHERE ticket_id = %s
        ORDER BY id DESC
        LIMIT 1
        """,
        (ticket_id,),
    )
    if row is None:
        return False
    if newest_article_id is None:
        return True
    built_to = row["built_to_article_id"]
    return built_to is not None and built_to >= newest_article_id


def _newest_article(ticket_id: int) -> int | None:
    row = postgres.query_one(
        """
        SELECT id FROM articles WHERE ticket_id = %s
        ORDER BY article_no DESC NULLS LAST, id DESC LIMIT 1
        """,
        (ticket_id,),
    )
    return row["id"] if row else None


def poll_once(*, limit: int | None = None, decide: bool = True) -> PollResult:
    """One pass over the intake queue.

    With decide=False the tickets are mirrored and nothing is sent to the model
    -- useful for filling the database quickly, or when the knowledge base is
    unavailable.
    """
    started = time.perf_counter()
    result = PollResult()

    with OtrsClient() as otrs:
        try:
            ids = otrs.intake(limit=limit)
        except OtrsError as exc:
            log.error("poll failed", extra={"error": str(exc)})
            result.seconds = time.perf_counter() - started
            return result

        result.seen = len(ids)
        log.info("polled", extra={"waiting": len(ids)})

        for otrs_id in ids:
            try:
                ticket = otrs.get(otrs_id)
            except OtrsError as exc:
                log.error("could not read ticket", extra={"ticket": otrs_id,
                                                          "error": str(exc)})
                result.failed += 1
                continue

            stored = intake_store.upsert(ticket)
            result.tickets.append(stored.ticket_number)

            if not decide:
                result.skipped += 1
                continue

            newest = _newest_article(stored.ticket_id)
            if _already_drafted(stored.ticket_id, newest):
                log.debug("already drafted", extra={"ticket": stored.ticket_number})
                result.skipped += 1
                continue

            try:
                context = context_builder.build(stored.ticket_id)
                decision, retrievals, corrections, meta = run(context)
            except (DecisionFailed, LookupError) as exc:
                log.error("could not decide", extra={"ticket": stored.ticket_number,
                                                     "error": str(exc)})
                # What it did before failing: the searches and retries that
                # led here are the useful part of a failure.
                audit.record(actor="orchestrator", action="decision_failed",
                             ticket_id=stored.ticket_id, reasoning=str(exc)[:500],
                             evidence={"steps": getattr(exc, "steps", [])})
                result.failed += 1
                continue

            draft_id = draft_store.save(context, decision, retrievals,
                                        corrections, meta)
            # Three checks after the draft exists, each catching its own
            # failures. Urgent first: the email is the part someone is waiting
            # for. Similar last: it sets drafts.similar_status, and the review
            # page lists a draft only once that is set -- so a reviewer never
            # sees a draft whose urgency, junk flag and precedents are still
            # being worked out.
            # Every step the agent took for this draft: searches and what they
            # returned, nudges, what it proposed, what the validator changed.
            audit.record(actor="orchestrator", action="trace",
                         ticket_id=stored.ticket_id, draft_id=draft_id,
                         reasoning=_trace_summary(meta.get("trace") or []),
                         evidence={"steps": meta.get("trace") or []},
                         model=meta.get("model"), prompt_ver=meta.get("prompt_ver"),
                         latency_ms=meta.get("latency_ms"))

            was_urgent = urgent.attach(draft_id, stored.ticket_id, decision)
            junk_verdict = junk.attach(draft_id, stored.ticket_id)
            similar_count = similar.attach(draft_id, stored.ticket_id)
            # And what the three checks after it found.
            audit.record(actor="orchestrator", action="checks",
                         ticket_id=stored.ticket_id, draft_id=draft_id,
                         reasoning=(f"urgent: {'yes' if was_urgent else 'no'}; "
                                    f"junk: {'flagged' if junk_verdict.looks_like_junk else 'no'}; "
                                    f"similar: {similar_count}"),
                         evidence={"urgent": was_urgent,
                                   "junk": {"flagged": junk_verdict.looks_like_junk,
                                            "junk_score": round(junk_verdict.score, 3),
                                            "real_score": round(junk_verdict.real_score, 3),
                                            "reason": junk_verdict.reason},
                                   "similar": similar_count})
            result.drafted += 1
            log.info("drafted", extra={"ticket": stored.ticket_number,
                                       "draft": draft_id,
                                       "action": decision.action.value})

    result.seconds = time.perf_counter() - started
    return result

def _trace_summary(steps: list) -> str:
    """One line for the audit log's list view: what the agent did, in order."""
    words = []
    for st in steps:
        kind = st.get("step")
        if kind == "search_knowledge_base":
            words.append(f"searched KB ({st.get('verdict') or 'error'})")
        elif kind == "nudge":
            words.append("nudged")
        elif kind == "retry":
            words.append("retried")
        elif kind == "validated" and st.get("corrections"):
            words.append(f"{len(st['corrections'])} corrected")
    return ", ".join(words) or "decided without searching"
