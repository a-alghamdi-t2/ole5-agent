"""The journey of each closed ticket: its exact path, and what happened on it.

    python scripts/extract_journeys.py --check                     # do both models answer? one short call each
    python scripts/extract_journeys.py --show 2026061482000903     # the timeline the model reads; no model call
    python scripts/extract_journeys.py --try 2026061482000903      # one ticket through the model; prints, stores nothing
    python scripts/extract_journeys.py --limit 20                  # a batch, stored
    python scripts/extract_journeys.py --moved-only                # only tickets whose path changed queue
    python scripts/extract_journeys.py --per-queue 150             # up to 150 most recent per closing queue
    python scripts/extract_journeys.py --best 2500 --since 2025-01-01 --show-pick   # which 2500 are worth most; no model call
    python scripts/extract_journeys.py --best 2500 --since 2025-01-01               # those, stored (support_agent/history/pick.py)
    python scripts/extract_journeys.py                             # everything not done yet
    python scripts/extract_journeys.py --redo --limit 20           # again, even if done
    python scripts/extract_journeys.py --redo-fallback             # the fallback's journeys, again with the main model
    python scripts/extract_journeys.py --pause 10                  # slower, if the rate limit is still hit
    python scripts/extract_journeys.py --stats
    python scripts/extract_journeys.py --export journeys          # one text file per queue, for writing the knowledge base

Reads ticket_timelines (scripts/export_timelines.py), so nothing here calls
OTRS. The model is JOURNEY_MODEL, falling back to JOURNEY_FALLBACK_MODEL; each
stored journey says which one wrote it.

Resumable: a ticket already done is skipped unless --redo.
"""

from __future__ import annotations

import argparse
import json
import sys
import re
import time
from pathlib import Path

from support_agent.config import get_settings
from support_agent.db import postgres
from support_agent.history import journey


def _id_of(number: str) -> int:
    row = postgres.query_one("SELECT id FROM closed_tickets WHERE ticket_number = %s", (number,))
    if row is None:
        sys.exit(f"no closed ticket {number}")
    return row["id"]


def cmd_check() -> None:
    s = get_settings()
    for label, model in (("main", s.journey_model), ("fallback", s.journey_fallback_model)):
        try:
            reply = journey.ask(model, [{"role": "user", "content":
                                         'Reply with exactly this JSON: {"ok": true}'}])
            print(f"  {label:9} {model:32} answers: {reply.strip()[:60]!r}")
        except Exception as exc:
            print(f"  {label:9} {model:32} FAILED: {type(exc).__name__}: {str(exc)[:150]}")


def cmd_show(number: str) -> None:
    ticket, tl = journey.load(_id_of(number))
    text = journey.render(ticket, tl)
    print(text)
    print(f"--- {len(text):,} characters (about {len(text) // 3:,} tokens)")


def cmd_try(number: str) -> None:
    ticket, tl = journey.load(_id_of(number))
    result, meta = journey.extract(ticket, tl)
    print(json.dumps({"path": tl["queue_path"],
                      "steps": [x.model_dump() for x in result.steps],
                      "moves": [m.model_dump(by_alias=True) for m in result.moves],
                      "summary": result.summary}, ensure_ascii=False, indent=2))
    fell = f", after the main model failed: {meta['fallback_reason'][:120]}" \
        if meta["fallback_reason"] else ""
    print(f"\nwritten by {meta['model']}{fell}")
    print(f"{meta['latency_ms'] / 1000:.1f}s, {meta['input_chars']:,} characters in. Nothing stored.")


def _best(args: argparse.Namespace) -> list[dict]:
    """The --best N tickets (history/pick.py), those not done yet unless --redo."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    from support_agent.history import pick

    since = (datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=ZoneInfo("Asia/Riyadh"))
             if args.since else None)
    chosen, summary = pick.best(args.best, since)
    pick.print_summary(summary)
    if args.show_pick:
        return []
    if not args.redo:
        done = {r["id"] for r in postgres.query("SELECT DISTINCT closed_ticket_id AS id FROM ticket_journeys")}
        chosen = [r for r in chosen if r["id"] not in done]
    return chosen[:args.limit] if args.limit else chosen


def cmd_run(args: argparse.Namespace) -> None:
    sql = """
        SELECT t.id, t.ticket_number, t.queue FROM ticket_timelines tl
        JOIN closed_tickets t ON t.id = tl.closed_ticket_id
        LEFT JOIN ticket_journeys j ON j.closed_ticket_id = t.id
        WHERE jsonb_array_length(tl.entries) > 0
    """
    if args.redo_fallback:
        sql += " AND j.fallback_reason IS NOT NULL"
    elif not args.redo:
        sql += " AND j.id IS NULL"
    if args.moved_only:
        sql += " AND cardinality(tl.queue_path) > 1"
    sql += " ORDER BY t.otrs_closed_at DESC NULLS LAST"
    params: tuple = ()
    if args.limit and not args.per_queue:
        sql += " LIMIT %s"
        params = (args.limit,)
    rows = _best(args) if args.best else postgres.query(sql, params)
    if args.best and args.show_pick:
        return
    if args.per_queue:
        # Up to N of each closing queue's most recent; junk has nothing to tell.
        seen: dict[str, int] = {}
        kept = []
        for r in rows:
            q = r["queue"] or ""
            if "junk" in q.lower() or seen.get(q, 0) >= args.per_queue:
                continue
            seen[q] = seen.get(q, 0) + 1
            kept.append(r)
        for q, n in sorted(seen.items()):
            print(f"  {q:40} {n} tickets")
        rows = kept[:args.limit] if args.limit else kept
    print(f"{len(rows)} tickets to extract\n")

    done = failed = fell_back = 0
    for i, r in enumerate(rows, 1):
        try:
            ticket, tl = journey.load(r["id"])
            result, meta = journey.extract(ticket, tl)
            journey.save(r["id"], tl["queue_path"] or [], result, meta)
        except journey.JourneyFailed as exc:
            failed += 1
            print(f"  [{i}/{len(rows)}] {r['ticket_number']} FAILED {str(exc)[:90]}")
            if "GROQ_API_KEY" in str(exc):
                break
            continue
        done += 1
        fell_back += bool(meta["fallback_reason"])
        if meta["fallback_reason"]:
            print(f"      main model failed: {meta['fallback_reason'][:140]}")
        route = " → ".join(q.split("::")[-1] for q in tl["queue_path"] or []) or "-"
        print(f"  [{i}/{len(rows)}] {r['ticket_number']}  {len(result.steps)} steps  "
              f"{meta['latency_ms'] / 1000:.0f}s  {meta['model'].split('/')[-1]:16}  {route}",
              flush=True)
        time.sleep(args.pause)
    print(f"\n{done} extracted ({fell_back} by the fallback), {failed} failed")


def cmd_stats() -> None:
    r = postgres.query_one(
        """
        SELECT (SELECT count(*) FROM ticket_timelines) AS timelines, count(*) AS journeys,
               count(*) FILTER (WHERE cardinality(path) > 1) AS moved,
               count(*) FILTER (WHERE fallback_reason IS NOT NULL) AS fell_back,
               round(avg(latency_ms) / 1000.0, 1) AS avg_s,
               count(*) FILTER (WHERE moves::text LIKE '%%not stated%%') AS unexplained
        FROM ticket_journeys
        """
    )
    print(f"{r['journeys']} of {r['timelines']} timelines have a journey "
          f"({r['moved']} changed queue), average {r['avg_s']}s")
    print(f"  {r['fell_back']} written by the fallback model")
    print(f"  {r['unexplained']} with a move the messages do not explain\n")
    for m in postgres.query("SELECT model, count(*) AS n FROM ticket_journeys GROUP BY 1 ORDER BY 2 DESC"):
        print(f"  {m['n']:5}  {m['model']}")


def cmd_export(folder: str) -> None:
    """One text file per queue the tickets were closed in: each ticket's
    summary and steps, for a person (or Claude) to turn into knowledge base
    documents. Junk is left out."""
    out = Path(folder)
    out.mkdir(parents=True, exist_ok=True)
    rows = postgres.query(
        """
        SELECT c.ticket_number, c.title, c.queue, c.subtype, j.path, j.steps, j.summary
        FROM ticket_journeys j JOIN closed_tickets c ON c.id = j.closed_ticket_id
        WHERE position('junk' in lower(coalesce(c.queue, ''))) = 0
        ORDER BY c.queue, c.otrs_closed_at DESC NULLS LAST
        """)
    by_queue: dict[str, list[dict]] = {}
    for r in rows:
        by_queue.setdefault(r["queue"] or "(no queue)", []).append(r)
    for queue, tickets in by_queue.items():
        lines = [
            f"# Closed in {queue}",
            "",
            f"{len(tickets)} tickets: what the customer asked, what was done, and",
            "how it ended. Written per ticket by a model from the ticket's history.",
            "",
        ]
        for r in tickets:
            lines.append(f"[{r['ticket_number']}] {r['title'] or ''}"
                         + (f"  ({r['subtype']})" if r["subtype"] else ""))
            if r["path"] and len(r["path"]) > 1:
                lines.append("  PATH: " + " -> ".join(r["path"]))
            if r["summary"]:
                lines.append(f"  SUMMARY: {r['summary']}")
            for step in r["steps"] or []:
                lines.append(f"    - {step.get('what', '')}")
            lines.append("")
        name = re.sub(r"[^0-9a-z]+", "_", queue.lower()).strip("_")
        path = out / f"{name}.txt"
        path.write_text("\n".join(lines), encoding="utf-8")
        print(f"  {path}  {len(tickets)} tickets, {path.stat().st_size // 1024} KB")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--show", metavar="TICKET")
    ap.add_argument("--try", dest="try_", metavar="TICKET")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--best", type=int, metavar="N",
                    help="the N tickets worth the most (support_agent/history/pick.py)")
    ap.add_argument("--since", metavar="DATE", help="with --best: closed on or after, 2025-01-01")
    ap.add_argument("--show-pick", action="store_true",
                    help="with --best: show what would be picked, call no model")
    ap.add_argument("--moved-only", action="store_true")
    ap.add_argument("--per-queue", type=int, metavar="N",
                    help="up to N of each closing queue's most recent tickets")
    ap.add_argument("--redo-fallback", action="store_true",
                    help="only the journeys the fallback model wrote, again")
    ap.add_argument("--pause", type=float, default=5.0,
                    help="seconds between tickets, to stay under the rate limit (default 5)")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--export", metavar="FOLDER")
    args = ap.parse_args()
    try:
        if args.check:
            cmd_check()
        elif args.show:
            cmd_show(args.show)
        elif args.try_:
            cmd_try(args.try_)
        elif args.stats:
            cmd_stats()
        elif args.export:
            cmd_export(args.export)
        else:
            cmd_run(args)
    finally:
        postgres.close_pool()


if __name__ == "__main__":
    main()
