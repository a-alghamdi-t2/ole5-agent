"""The journey of each closed ticket: its exact path, and what happened on it.

    python scripts/extract_journeys.py --check                     # do both models answer? one short call each
    python scripts/extract_journeys.py --show 2026061482000903     # the timeline the model reads; no model call
    python scripts/extract_journeys.py --try 2026061482000903      # one ticket through the model; prints, stores nothing
    python scripts/extract_journeys.py --limit 20                  # a batch, stored
    python scripts/extract_journeys.py --moved-only                # only tickets whose path changed queue
    python scripts/extract_journeys.py                             # everything not done yet
    python scripts/extract_journeys.py --redo --limit 20           # again, even if done
    python scripts/extract_journeys.py --redo-fallback             # the fallback's journeys, again with the main model
    python scripts/extract_journeys.py --pause 10                  # slower, if the rate limit is still hit
    python scripts/extract_journeys.py --stats

Reads ticket_timelines (scripts/export_timelines.py), so nothing here calls
OTRS. The model is JOURNEY_MODEL, falling back to JOURNEY_FALLBACK_MODEL; each
stored journey says which one wrote it.

Resumable: a ticket already done is skipped unless --redo.
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from ole5.config import get_settings
from ole5.db import postgres
from ole5.history import journey


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


def cmd_run(args: argparse.Namespace) -> None:
    sql = """
        SELECT t.id, t.ticket_number FROM ticket_timelines tl
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
    if args.limit:
        sql += " LIMIT %s"
        params = (args.limit,)
    rows = postgres.query(sql, params)
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--show", metavar="TICKET")
    ap.add_argument("--try", dest="try_", metavar="TICKET")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--moved-only", action="store_true")
    ap.add_argument("--redo-fallback", action="store_true",
                    help="only the journeys the fallback model wrote, again")
    ap.add_argument("--pause", type=float, default=5.0,
                    help="seconds between tickets, to stay under the rate limit (default 5)")
    ap.add_argument("--stats", action="store_true")
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
        else:
            cmd_run(args)
    finally:
        postgres.close_pool()


if __name__ == "__main__":
    main()
