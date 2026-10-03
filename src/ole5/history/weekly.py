"""The weekly history update, and the report that goes with it.

Every Friday at 04:00 Riyadh time:

  1. Ask OTRS for every ticket closed since the last run in the four Ole5
     queues and Junk -- all of them, handled by the agent or not.
  2. For each: store it (closed_tickets), build its cleaned timeline and search
     text, and, unless it is junk, extract its journey.
  3. Rebuild the similar-tickets index and the junk index, so the new tickets
     are matched against from the next draft on.
  4. Write a report of the week -- what the agent drafted, what reviewers
     changed and why, how the tickets it handled ended up -- and email it.

The report is built from specific tickets, not rates: every count sits beside
the list it counts, so each line can be checked in OTRS. Rates come last, as a
summary of those lists.

Nothing new is recorded when a draft is reviewed: the reviews table already
holds the reviewer's final values, what they changed and why, and the draft
holds what the agent proposed. The report joins those to the ticket's outcome
once OTRS has closed it.
"""

from __future__ import annotations

import json
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from ole5.config import get_settings
from ole5.db import audit, postgres
from ole5.logging import get_logger

log = get_logger(__name__)

RIYADH = ZoneInfo("Asia/Riyadh")
RUN_WEEKDAY = 4          # Friday (Monday is 0)
RUN_HOUR = 4             # 04:00 Riyadh time

HISTORY_QUEUES = [
    "Ole5 New::Operations", "Ole5 New::Product", "Ole5 New::Product Support",
    "Ole5 New::Customer Success", "Junk",
]
SHORT = {q: q.split("::")[-1] for q in HISTORY_QUEUES}


# ---------------------------------------------------------------------------
# when
# ---------------------------------------------------------------------------

def week_key(when: datetime) -> str:
    iso = when.astimezone(RIYADH).isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def due(now: datetime | None = None) -> str | None:
    """This week's key if it is Friday 04:00 or later in Riyadh and the week
    has not been run yet. Later on Friday counts too: if the app was down at
    04:00, the run happens when it is back, the same day."""
    local = (now or datetime.now(UTC)).astimezone(RIYADH)
    if local.weekday() != RUN_WEEKDAY or local.hour < RUN_HOUR:
        return None
    key = week_key(local)
    done = postgres.query_one("SELECT 1 AS x FROM weekly_runs WHERE week_key = %s", (key,))
    return None if done else key


def _window_start(end: datetime) -> datetime:
    """From where the last completed run stopped, or a week back."""
    row = postgres.query_one(
        "SELECT max(window_end) AS t FROM weekly_runs WHERE status = 'done'")
    return row["t"] if row and row["t"] else end - timedelta(days=7)


def maybe_run() -> dict:
    """Called by the app every few minutes: runs the week's update when due."""
    s = get_settings()
    if not s.weekly_enabled:
        return {"due": False, "enabled": False}
    key = due()
    if not key:
        return {"due": False}
    return run(key)


# ---------------------------------------------------------------------------
# the history
# ---------------------------------------------------------------------------

def _otrs():
    from ole5.clients.otrs import OtrsClient

    s = get_settings()
    if s.history_otrs_base_url:
        return OtrsClient(base_url=s.history_otrs_base_url, user=s.history_otrs_user,
                          password=(s.history_otrs_password.get_secret_value()
                                    if s.history_otrs_password else None))
    return OtrsClient()


def closed_between(otrs, start: datetime, end: datetime) -> list[str]:
    """TicketIDs closed in the window, in the history queues. OTRS compares
    in its own local time, which is Riyadh's."""
    fmt = "%Y-%m-%d %H:%M:%S"
    data = otrs._call("TicketSearch", {
        "Queues": HISTORY_QUEUES,
        "StateType": ["closed"],
        "TicketCloseTimeNewerDate": start.astimezone(RIYADH).strftime(fmt),
        "TicketCloseTimeOlderDate": end.astimezone(RIYADH).strftime(fmt),
        "Limit": 10000,
    })
    return [str(i) for i in (data.get("TicketID") or [])]


def _save_timeline(closed_ticket_id: int, tl) -> None:
    from ole5.history import timeline

    postgres.execute(
        """
        INSERT INTO ticket_timelines (closed_ticket_id, entries, queue_path, article_count,
                                      kept_count, notification_count, noise_count,
                                      empty_count, clean_ver)
        VALUES (%s, %s::jsonb, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (closed_ticket_id) DO UPDATE SET
            entries = EXCLUDED.entries, queue_path = EXCLUDED.queue_path,
            article_count = EXCLUDED.article_count, kept_count = EXCLUDED.kept_count,
            notification_count = EXCLUDED.notification_count,
            noise_count = EXCLUDED.noise_count, empty_count = EXCLUDED.empty_count,
            clean_ver = EXCLUDED.clean_ver, fetched_at = now()
        """,
        (closed_ticket_id, json.dumps(tl.entries, ensure_ascii=False), tl.queue_path,
         tl.article_count, len(tl.entries), tl.notification_count, tl.noise_count,
         tl.empty_count, timeline.CLEAN_VER),
    )


def _fill_search_text(closed_ticket_id: int, boilerplate_for) -> None:
    """The first article, cleaned the same way as the rest of the history."""
    from ole5.intake import search_text

    row = postgres.query_one(
        """
        SELECT a.sender_address, a.body_raw FROM closed_articles a
        WHERE a.closed_ticket_id = %s ORDER BY a.article_no NULLS LAST, a.id LIMIT 1
        """, (closed_ticket_id,))
    if row is None:
        return
    text = search_text.build(row["body_raw"] or "", boilerplate_for(row["sender_address"]),
                             sender=row["sender_address"])
    postgres.execute("UPDATE closed_tickets SET search_text = %s, search_ver = %s WHERE id = %s",
                     (text, search_text.SEARCH_VER, closed_ticket_id))


def boilerplate_loader():
    """The boilerplate per sender, loaded once each."""
    from ole5.intake import search_text

    cache: dict = {}

    def bp(sender):
        key = (sender or "").strip().lower()
        if key not in cache:
            cache[key] = search_text.load_boilerplate(sender)
        return cache[key]
    return bp


def add_one(otrs, otrs_ticket_id: str, bp) -> dict:
    """One closed ticket into the history: stored, timeline, search text. The
    same steps for the Friday run and for `scripts/weekly.py one`."""
    from ole5.history import timeline
    from ole5.history import store as history
    from ole5.intake.contracts import IncomingTicket

    raw = otrs._call("TicketGet", {"TicketID": str(otrs_ticket_id), "AllArticles": 1,
                                   "DynamicFields": 1, "Extended": 1})
    payload = raw["Ticket"][0]
    ticket = IncomingTicket.model_validate(payload)
    stored = history.save(ticket, payload)
    cid = stored.ticket_id
    tl = timeline.build(payload.get("Article") or [], bp)
    _save_timeline(cid, tl)
    _fill_search_text(cid, bp)
    return {"id": cid, "ticket_number": stored.ticket_number,
            "queue": payload.get("Queue") or "",
            "junk": "junk" in (payload.get("Queue") or "").lower(),
            "articles": tl.article_count, "kept": stored.kept, "noise": stored.dropped,
            "path": tl.queue_path}


def journey_for(closed_ticket_id: int) -> str:
    """Extract and store one ticket's journey; returns its summary."""
    from ole5.history import journey

    ticket, tl = journey.load(closed_ticket_id)
    result, meta = journey.extract(ticket, tl)
    journey.save(closed_ticket_id, tl["queue_path"] or [], result, meta)
    return result.summary


def add_to_history(start: datetime, end: datetime) -> dict:
    """Bring the tickets closed in the window into the history."""
    bp = boilerplate_loader()
    added: list[dict] = []
    failed: list[dict] = []
    with _otrs() as otrs:
        ids = closed_between(otrs, start, end)
        log.info("weekly: tickets closed in the window", extra={"count": len(ids)})
        for tid in ids:
            try:
                added.append(add_one(otrs, tid, bp))
            except Exception as exc:
                failed.append({"otrs_id": tid, "step": "export",
                               "error": f"{type(exc).__name__}: {exc}"[:300]})
                log.exception("weekly: export failed", extra={"otrs_id": tid})
            time.sleep(0.4)      # as the manual exports do, to spare their API

    # Journeys for the real tickets. Junk has nothing to tell.
    journeys = 0
    for a in (x for x in added if not x["junk"]):
        try:
            journey_for(a["id"])
            journeys += 1
        except Exception as exc:
            failed.append({"ticket_number": a["ticket_number"], "step": "journey",
                           "error": f"{type(exc).__name__}: {exc}"[:300]})
            log.exception("weekly: journey failed", extra={"ticket": a["ticket_number"]})

    return {"found": len(ids), "added": added, "journeys": journeys, "failed": failed}


def sync_indexes(added: list[dict]) -> dict:
    """Bring both indexes in line with the history: add the week's tickets,
    remove what should not be there, correct anything a past run left wrong.
    Only the week's tickets are embedded -- no rebuild. Both indexes are
    synced every week, even a week with nothing new, so drift is corrected."""
    from ole5.history import junk, precedent

    out: dict = {}
    real = {a["ticket_number"] for a in added if not a["junk"]}
    junk_numbers = {a["ticket_number"] for a in added if a["junk"]}
    try:
        out["similar"] = precedent.sync(refresh=real)
    except Exception as exc:
        out["similar_error"] = f"{type(exc).__name__}: {exc}"[:300]
        log.exception("weekly: similar index sync failed")
    try:
        # The week's Junk tickets are checked first: repeats of junk already
        # learned are not learned again.
        out["junk_checked"] = junk.check_pending()
    except Exception as exc:
        out["junk_check_error"] = f"{type(exc).__name__}: {exc}"[:300]
        log.exception("weekly: junk check failed")
    try:
        out["junk"] = junk.sync(refresh=junk_numbers)
    except Exception as exc:
        out["junk_error"] = f"{type(exc).__name__}: {exc}"[:300]
        log.exception("weekly: junk index sync failed")
    return out


# ---------------------------------------------------------------------------
# the report
# ---------------------------------------------------------------------------

def _pct(n: int, d: int) -> str:
    return f"{round(100 * n / d)}%" if d else "-"


def gather(start: datetime, end: datetime, added: list[dict]) -> dict:
    """Everything the report says, as data: lists of tickets, not only counts."""
    drafted = postgres.query(
        """
        SELECT d.id, t.ticket_number, t.title,
               coalesce(d.reply_kind::text, d.action::text) AS decision, d.queue
        FROM drafts d JOIN tickets t ON t.id = d.ticket_id
        WHERE d.created_at >= %s AND d.created_at < %s ORDER BY d.id
        """, (start, end))

    reviews = postgres.query(
        """
        SELECT r.outcome::text AS outcome, r.changed_fields, r.changes, r.justification,
               r.final, r.created_at, coalesce(v.display_name, v.email) AS reviewer,
               d.id AS draft_id, coalesce(d.reply_kind::text, d.action::text) AS decision,
               d.queue AS agent_queue, t.ticket_number, t.title
        FROM reviews r JOIN drafts d ON d.id = r.draft_id JOIN tickets t ON t.id = d.ticket_id
        LEFT JOIN reviewers v ON v.id = r.reviewer_id
        WHERE r.created_at >= %s AND r.created_at < %s ORDER BY r.created_at
        """, (start, end))

    urgent = postgres.query(
        """
        SELECT t.ticket_number, t.title, u.keywords, u.by_agent, u.agent_reason,
               (SELECT count(*) FROM urgent_emails e WHERE e.draft_id = d.id AND e.status = 'sent') AS emailed
        FROM draft_urgent u JOIN drafts d ON d.id = u.draft_id JOIN tickets t ON t.id = d.ticket_id
        WHERE d.created_at >= %s AND d.created_at < %s ORDER BY d.id
        """, (start, end))

    junk_flags = postgres.query(
        """
        SELECT t.ticket_number, t.title, j.reason, r.outcome::text AS outcome,
               r.final->>'queue' AS sent_queue
        FROM draft_junk j JOIN drafts d ON d.id = j.draft_id JOIN tickets t ON t.id = d.ticket_id
        LEFT JOIN reviews r ON r.draft_id = d.id
        WHERE j.flagged AND d.created_at >= %s AND d.created_at < %s ORDER BY d.id
        """, (start, end))

    # Tickets the agent drafted that OTRS has now closed: the outcome.
    numbers = [a["ticket_number"] for a in added]
    outcomes = postgres.query(
        """
        SELECT DISTINCT ON (t.ticket_number)
               t.ticket_number, t.title, d.queue AS agent_queue,
               r.final->>'queue' AS sent_queue, r.outcome::text AS outcome,
               c.queue AS closed_queue, coalesce(tl.queue_path, '{}') AS path
        FROM tickets t
        JOIN drafts d ON d.ticket_id = t.id
        JOIN reviews r ON r.draft_id = d.id
        JOIN closed_tickets c ON c.ticket_number = t.ticket_number
        LEFT JOIN ticket_timelines tl ON tl.closed_ticket_id = c.id
        WHERE t.ticket_number = ANY(%s) AND r.outcome::text <> 'reject'
        ORDER BY t.ticket_number, r.created_at DESC
        """, (numbers,)) if numbers else []

    waiting = postgres.query(
        """
        SELECT DISTINCT ON (t.ticket_number) t.ticket_number, t.title,
               r.final->>'queue' AS sent_queue, r.created_at
        FROM reviews r JOIN drafts d ON d.id = r.draft_id JOIN tickets t ON t.id = d.ticket_id
        WHERE r.outcome::text <> 'reject'
          AND NOT EXISTS (SELECT 1 FROM closed_tickets c WHERE c.ticket_number = t.ticket_number)
        ORDER BY t.ticket_number, r.created_at DESC
        """)

    sizes = postgres.query_one(
        """
        SELECT count(*) FILTER (WHERE position('junk' in lower(coalesce(queue,''))) = 0) AS real,
               count(*) FILTER (WHERE position('junk' in lower(coalesce(queue,''))) > 0) AS junk
        FROM closed_tickets
        """)
    return {"drafted": drafted, "reviews": reviews, "urgent": urgent,
            "junk_flags": junk_flags, "outcomes": outcomes, "waiting": waiting,
            "sizes": sizes}


def _path_text(row: dict) -> str:
    path = [SHORT.get(q, q.split("::")[-1]) for q in (row["path"] or [])
            if q in SHORT]            # the four Ole5 queues and Junk only
    closed = SHORT.get(row["closed_queue"] or "", row["closed_queue"] or "?")
    if len(path) > 1:
        return " -> ".join(path) + f", closed in {closed}"
    return f"closed in {closed}"


def _stayed(row: dict) -> bool:
    """Did the ticket close in the queue it was sent to, without moving on?"""
    sent = row["sent_queue"] or row["agent_queue"]
    path = [q for q in (row["path"] or []) if q in SHORT]
    return row["closed_queue"] == sent and all(q == sent for q in path[1:] or [sent])


def render(start: datetime, end: datetime, history: dict, indexes: dict, g: dict) -> str:
    a, b = start.astimezone(RIYADH), end.astimezone(RIYADH)
    L: list[str] = []
    w = L.append

    w(f"SUPPORT AGENT -- WEEKLY REPORT")
    w(f"{a:%d %b %Y %H:%M} to {b:%d %b %Y %H:%M} (Riyadh time)")
    w("")

    # ---- the agent's week
    reviews = g["reviews"]
    as_drafted = [r for r in reviews if r["outcome"] == "approve"]
    edited = [r for r in reviews if r["outcome"] == "edit"]
    rejected = [r for r in reviews if r["outcome"] == "reject"]
    kinds = Counter(d["decision"] for d in g["drafted"])
    w("1. WHAT THE AGENT DID")
    w(f"   Drafts made: {len(g['drafted'])}"
      + (f"  ({', '.join(f'{n} {k}' for k, n in kinds.most_common())})" if kinds else ""))
    w(f"   Reviewed: {len(reviews)}  --  approved as drafted {len(as_drafted)}, "
      f"approved with changes {len(edited)}, rejected {len(rejected)}")
    w("")

    # ---- corrections, each one
    w(f"2. WHAT REVIEWERS CHANGED ({len(edited)})")
    if not edited:
        w("   Nothing: every approval went out as the agent drafted it.")
    for r in edited:
        w(f"   {r['ticket_number']}  {(r['title'] or '')[:70]}")
        changes = r["changes"] or {}
        for field in r["changed_fields"] or []:
            ch = changes.get(field) or {}
            if isinstance(ch, dict) and ("from" in ch or "to" in ch):
                w(f"      {field}: {ch.get('from')}  ->  {ch.get('to')}")
            else:
                w(f"      {field}: changed")
        if r["justification"]:
            w(f"      why: {r['justification'][:200]}")
        w(f"      reviewer: {r['reviewer'] or '?'}")
    fields = Counter(f for r in edited for f in (r["changed_fields"] or []))
    if fields:
        w("   Most changed: " + ", ".join(f"{f} ({n})" for f, n in fields.most_common()))
    w("")

    w(f"3. REJECTED ({len(rejected)})")
    if not rejected:
        w("   None.")
    for r in rejected:
        w(f"   {r['ticket_number']}  {(r['title'] or '')[:70]}")
        w(f"      agent's decision: {r['decision']}, {SHORT.get(r['agent_queue'], r['agent_queue'])}")
        w(f"      why rejected: {(r['justification'] or '-')[:200]}  ({r['reviewer'] or '?'})")
    w("")

    # ---- outcomes
    out = g["outcomes"]
    stayed = [o for o in out if _stayed(o)]
    moved = [o for o in out if not _stayed(o)]
    w(f"4. HOW THE AGENT'S TICKETS ENDED UP -- closed this week ({len(out)})")
    if not out:
        w("   None of the tickets the agent drafted was closed this week.")
    for o in out:
        agent = SHORT.get(o["agent_queue"], o["agent_queue"])
        sent = SHORT.get(o["sent_queue"] or o["agent_queue"], o["sent_queue"] or "?")
        mark = "OK  " if _stayed(o) else "MOVED"
        who = f"agent chose {agent}" + (f", reviewer sent it to {sent}" if sent != agent else "")
        w(f"   {mark} {o['ticket_number']}  {who}; {_path_text(o)}")
    if out:
        agreed = [o for o in out if (o["sent_queue"] or o["agent_queue"]) == o["agent_queue"]]
        w(f"   Closed where it was sent: {len(stayed)} of {len(out)}. "
          f"Moved on to another team first: {len(moved)}.")
        w(f"   Reviewer kept the agent's queue: {len(agreed)} of {len(out)}.")
    w("")

    waiting = g["waiting"]
    w(f"5. STILL OPEN -- approved but not yet closed in OTRS ({len(waiting)})")
    for r in waiting[:15]:
        age = (end - r["created_at"]).days
        w(f"   {r['ticket_number']}  sent to {SHORT.get(r['sent_queue'], r['sent_queue'] or '?')}, "
          f"{age} day{'s' if age != 1 else ''} ago")
    if len(waiting) > 15:
        w(f"   ... and {len(waiting) - 15} more.")
    w("")

    # ---- the flags
    w(f"6. URGENT ({len(g['urgent'])})")
    for u in g["urgent"]:
        why = []
        if u["keywords"]:
            why.append("keyword: " + ", ".join(u["keywords"]))
        if u["by_agent"]:
            why.append("agent: " + (u["agent_reason"] or "no reason given")[:120])
        w(f"   {u['ticket_number']}  {(u['title'] or '')[:60]}")
        w(f"      {'; '.join(why)}  --  emailed {u['emailed']} recipient(s)")
    if not g["urgent"]:
        w("   None.")
    w("")

    jf = g["junk_flags"]
    w(f"7. FLAGGED AS JUNK ({len(jf)})")
    for j in jf:
        if j["outcome"] is None:
            verdict = "not reviewed yet"
        elif j["outcome"] == "reject":
            verdict = "draft rejected"
        else:
            verdict = ("reviewer agreed: sent to Junk" if j["sent_queue"] == "Junk"
                       else f"reviewer disagreed: sent to {SHORT.get(j['sent_queue'], j['sent_queue'])}")
        w(f"   {j['ticket_number']}  {(j['title'] or '')[:60]}  --  {verdict}")
    if not jf:
        w("   None.")
    w("")

    # ---- the history
    added = history["added"]
    per_queue = Counter(SHORT.get(x["queue"], x["queue"]) for x in added)
    w("8. HISTORY UPDATED")
    w(f"   Closed tickets added: {len(added)} of {history['found']} found in OTRS"
      + (f"  ({', '.join(f'{q} {n}' for q, n in per_queue.most_common())})" if per_queue else ""))
    w(f"   Journeys written: {history['journeys']}")
    def synced(label: str, x: dict) -> None:
        w(f"   {label}: {x['added']} added, {x['refreshed']} refreshed, "
          f"{x['removed']} removed -- now {x['total']} tickets")
    if "similar" in indexes:
        synced("Similar-tickets index", indexes["similar"])
    if "junk_checked" in indexes:
        jc = indexes["junk_checked"]
        w(f"   Junk checked: {jc['checked']} -- kept {jc['kept']}, repeats {jc['repeat']}")
    if "junk" in indexes:
        synced("Junk index", indexes["junk"])
    w(f"   History now holds {g['sizes']['real']} real and {g['sizes']['junk']} junk tickets.")
    w("")

    problems = list(history["failed"])
    for k in ("similar_error", "junk_error", "junk_check_error"):
        if k in indexes:
            problems.append({"step": k.replace("_error", "").replace("_", " "), "error": indexes[k]})
    w(f"9. PROBLEMS ({len(problems)})")
    for p in problems:
        who = p.get("ticket_number") or p.get("otrs_id") or ""
        w(f"   {p['step']} {who}: {p['error']}")
    if not problems:
        w("   None.")
    w("")

    # ---- the summary, last: rates of the lists above
    w("SUMMARY")
    if reviews:
        w(f"   Approved without changes: {_pct(len(as_drafted), len(reviews))} "
          f"({len(as_drafted)} of {len(reviews)} reviewed)")
    if out:
        w(f"   Closed where they were sent: {_pct(len(stayed), len(out))} "
          f"({len(stayed)} of {len(out)} closed)")
    judged = [j for j in jf if j["outcome"] and j["outcome"] != "reject"]
    if judged:
        agree = [j for j in judged if j["sent_queue"] == "Junk"]
        w(f"   Junk flags the reviewer agreed with: {_pct(len(agree), len(judged))} "
          f"({len(agree)} of {len(judged)})")
    if not (reviews or out or judged):
        w("   Nothing to measure this week.")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# the run
# ---------------------------------------------------------------------------

def _email(subject: str, body: str, to: list[str] | None = None) -> list[str]:
    from ole5.notify import mail

    sent = []
    listed = get_settings().weekly_report_recipients or ""
    for address in (to or [x.strip() for x in listed.split(",") if x.strip()]):
        try:
            if mail.deliver(mail.compose(address, subject, body)):
                sent.append(address)
        except Exception:
            log.exception("weekly report email failed", extra={"to": address})
    return sent


def run(key: str | None = None, *, send: bool = True,
        start: datetime | None = None, end: datetime | None = None) -> dict:
    """One weekly update. Claims the week first, so it cannot run twice."""
    end = end or datetime.now(UTC)
    start = start or _window_start(end)
    key = key or f"manual-{end.astimezone(RIYADH):%Y%m%d-%H%M%S}"

    claimed = postgres.query_one(
        """
        INSERT INTO weekly_runs (week_key, status, window_start, window_end)
        VALUES (%s, 'running', %s, %s) ON CONFLICT (week_key) DO NOTHING RETURNING id
        """, (key, start, end))
    if claimed is None:
        return {"due": False, "reason": f"{key} already run"}
    run_id = claimed["id"]
    log.info("weekly update started", extra={"week": key, "run": run_id})

    try:
        history = add_to_history(start, end)
        indexes = sync_indexes(history["added"])
        g = gather(start, end, history["added"])
        report = render(start, end, history, indexes, g)
        a, b = start.astimezone(RIYADH), end.astimezone(RIYADH)
        subject = f"Support agent weekly report: {a:%d %b} to {b:%d %b %Y}"
        emailed = _email(subject, report) if send else []
        summary = {"found": history["found"], "added": len(history["added"]),
                   "journeys": history["journeys"], "failed": len(history["failed"]),
                   "indexes": indexes, "reviews": len(g["reviews"]),
                   "closed_outcomes": len(g["outcomes"])}
        postgres.execute(
            """
            UPDATE weekly_runs SET status = 'done', summary = %s::jsonb, report = %s,
                   emailed_to = %s, finished_at = now() WHERE id = %s
            """, (json.dumps(summary, default=str), report, emailed, run_id))
        audit.record(actor="intake", action="weekly_update",
                     reasoning=f"{key}: {len(history['added'])} closed tickets added, "
                               f"{len(history['failed'])} problems, emailed {len(emailed)}",
                     evidence={"run": run_id, **summary})
        log.info("weekly update done", extra={"week": key, **summary})
        return {"due": True, "week": key, "run": run_id, **summary, "emailed": emailed}
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"[:1000]
        postgres.execute("UPDATE weekly_runs SET status = 'failed', error = %s, "
                         "finished_at = now() WHERE id = %s", (err, run_id))
        audit.record(actor="intake", action="weekly_update_failed", reasoning=err,
                     evidence={"run": run_id, "week": key})
        log.exception("weekly update failed", extra={"week": key})
        if send:
            _email(f"Support agent weekly update FAILED ({key})",
                   f"The weekly history update for {key} failed and the history was not "
                   f"updated.\n\n{err}\n\nIt will not retry by itself. Run it by hand with:\n"
                   f"python scripts/weekly.py run")
        return {"due": True, "week": key, "run": run_id, "error": err}
