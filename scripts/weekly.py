"""The weekly history update, by hand.

    python scripts/weekly.py check                   # log in, and count this week's closed tickets (reads only)
    python scripts/weekly.py one 2026022682000639    # one closed ticket into the history, step by step
    python scripts/weekly.py email 3 --to me@t2.sa   # send run 3's report to one address
    python scripts/weekly.py status                  # the runs so far
    python scripts/weekly.py preview --days 7        # the report for the last 7 days, changing nothing
    python scripts/weekly.py run --no-email          # a full run now: history, indexes, report; no email
    python scripts/weekly.py run                     # the same, and email the report
    python scripts/weekly.py show 3                  # the report of run 3, exactly as sent

The app runs this by itself every Friday at 04:00 Riyadh time. By hand, it
covers the time since the last completed run (or the last 7 days), and is
recorded as a run of its own, so the Friday run carries on from where it
stopped.

It reads OTRS and calls the embedding service and the journey model, so a run
takes as long as the week's tickets need: minutes for a normal week.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime, timedelta

from ole5.db import postgres
from ole5.history import weekly


def cmd_check(args: argparse.Namespace) -> None:
    """Which OTRS the history comes from, that we can log in, and what the
    search by close date finds for the last N days. Reads only."""
    from collections import Counter

    from ole5.config import get_settings

    s = get_settings()
    source = s.history_otrs_base_url or s.otrs_base_url
    user = s.history_otrs_user if s.history_otrs_base_url else s.otrs_user
    print(f"history is read from: {source}")
    print(f"as user:              {user}"
          + ("   (HISTORY_OTRS_*)" if s.history_otrs_base_url else "   (the same OTRS the agent uses)"))

    end = datetime.now(UTC)
    start = end - timedelta(days=args.days)
    with weekly._otrs() as otrs:
        ids = weekly.closed_between(otrs, start, end)
        print(f"\nclosed in the last {args.days} days, in the four queues and Junk: {len(ids)} tickets")
        queues, sample = Counter(), {}
        for tid in ids:
            # Extended: the close date is only among OTRS's extended fields.
            t = otrs._call("TicketGet", {"TicketID": tid, "Extended": 1})["Ticket"][0]
            queues[t.get("Queue")] += 1
            sample.setdefault(t.get("Queue"), t)      # one example per queue
        for q, n in queues.most_common():
            print(f"  {n:5}  {q}")
        if sample:
            print("\none example per queue:")
        for t in sample.values():
            print(f"  {t.get('TicketNumber')}  {t.get('Queue'):28} closed {t.get('Closed')}  "
                  f"{(t.get('Title') or '')[:50]}")
    print("\nNothing was stored or changed.")


def cmd_one(args: argparse.Namespace) -> None:
    """One closed ticket into the history, with every step shown."""
    with weekly._otrs() as otrs:
        tid = args.ticket
        if len(tid) > 10:        # a ticket number, not an OTRS id
            found = otrs._call("TicketSearch", {"TicketNumber": tid}).get("TicketID") or []
            if not found:
                sys.exit(f"no ticket {tid} on this OTRS")
            tid = str(found[0])
        print(f"OTRS ticket id {tid}")
        a = weekly.add_one(otrs, tid, weekly.boilerplate_loader())
    print(f"  stored:       closed ticket #{a['id']}, {a['ticket_number']}, queue {a['queue']}")
    print(f"  articles:     {a['articles']} in OTRS, {a['kept']} kept, {a['noise']} noise")
    print(f"  route:        {' -> '.join(a['path']) or 'no queue notifications recorded'}")
    row = postgres.query_one("SELECT search_text FROM closed_tickets WHERE id = %s", (a["id"],))
    text = " ".join((row["search_text"] or "").split())
    print(f"  search text:  {text[:200] or '(empty)'}")
    if a["junk"]:
        print("  journey:      none -- junk has nothing to tell")
    elif args.no_journey:
        print("  journey:      skipped (--no-journey)")
    else:
        print("  journey:      asking the model...", flush=True)
        print(f"                {weekly.journey_for(a['id'])}")
    print("\nThis ticket is now in the history. The indexes are rebuilt by the full run.")


def cmd_email(args: argparse.Namespace) -> None:
    row = postgres.query_one("SELECT week_key, window_start, window_end, report FROM weekly_runs "
                             "WHERE id = %s", (args.run,))
    if not row or not row["report"]:
        sys.exit(f"no report for run {args.run}")
    a, b = row["window_start"].astimezone(weekly.RIYADH), row["window_end"].astimezone(weekly.RIYADH)
    to = [x.strip() for x in args.to.split(",")] if args.to else None
    sent = weekly._email(f"Support agent weekly report: {a:%d %b} to {b:%d %b %Y}", row["report"], to)
    print(f"emailed to: {', '.join(sent) or 'nobody (no recipients, or DRY_RUN is on)'}")


def cmd_status(_: argparse.Namespace) -> None:
    rows = postgres.query(
        "SELECT id, week_key, status, window_start, window_end, summary, emailed_to, error, "
        "started_at, finished_at FROM weekly_runs ORDER BY id DESC LIMIT 15")
    if not rows:
        print("No runs yet. The first automatic one is on Friday at 04:00 Riyadh time.")
    for r in rows:
        s = r["summary"] or {}
        took = (r["finished_at"] - r["started_at"]).seconds // 60 if r["finished_at"] else None
        print(f"#{r['id']}  {r['week_key']:22} {r['status']:8} "
              f"{r['window_start'].astimezone(weekly.RIYADH):%d %b} -> "
              f"{r['window_end'].astimezone(weekly.RIYADH):%d %b %H:%M}  "
              f"added {s.get('added', '-')}, problems {s.get('failed', '-')}, "
              f"emailed {len(r['emailed_to'] or [])}"
              + (f", {took} min" if took is not None else ""))
        if r["error"]:
            print(f"      error: {r['error'][:200]}")
    nxt = weekly.due()
    print("\nDue now." if nxt else "\nNot due now: it runs Fridays from 04:00 Riyadh time.")


def cmd_preview(args: argparse.Namespace) -> None:
    """The report for a window, from what is already stored. Nothing changes:
    no OTRS, no indexes, no email."""
    end = datetime.now(UTC)
    start = end - timedelta(days=args.days)
    added = [{"ticket_number": r["ticket_number"], "queue": r["queue"] or "",
              "junk": "junk" in (r["queue"] or "").lower(), "id": r["id"]}
             for r in postgres.query(
                 "SELECT id, ticket_number, queue FROM closed_tickets "
                 "WHERE otrs_closed_at >= %s AND otrs_closed_at < %s", (start, end))]
    history = {"found": len(added), "added": added, "journeys": 0, "failed": []}
    g = weekly.gather(start, end, added)
    print(weekly.render(start, end, history, {}, g))
    print("\n(preview: built from what is stored; nothing was fetched, changed or sent)")


def cmd_run(args: argparse.Namespace) -> None:
    out = weekly.run(send=not args.no_email)
    if out.get("error"):
        sys.exit(f"failed: {out['error']}")
    print(f"run #{out.get('run')}: {out.get('added', 0)} closed tickets added, "
          f"{out.get('failed', 0)} problems, emailed {len(out.get('emailed') or [])}")
    row = postgres.query_one("SELECT report FROM weekly_runs WHERE id = %s", (out.get("run"),))
    if row and row["report"]:
        print("\n" + row["report"])


def cmd_show(args: argparse.Namespace) -> None:
    row = postgres.query_one("SELECT report FROM weekly_runs WHERE id = %s", (args.run,))
    if not row or not row["report"]:
        sys.exit(f"no report for run {args.run}")
    print(row["report"])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--days", type=int, default=7)
    o = sub.add_parser("one")
    o.add_argument("ticket", help="a ticket number, or an OTRS ticket id")
    o.add_argument("--no-journey", action="store_true")
    e = sub.add_parser("email")
    e.add_argument("run", type=int)
    e.add_argument("--to", help="comma-separated; default: WEEKLY_REPORT_RECIPIENTS")
    sub.add_parser("status")
    p = sub.add_parser("preview")
    p.add_argument("--days", type=int, default=7)
    r = sub.add_parser("run")
    r.add_argument("--no-email", action="store_true")
    s = sub.add_parser("show")
    s.add_argument("run", type=int)
    args = ap.parse_args()
    try:
        {"check": cmd_check, "one": cmd_one, "email": cmd_email, "status": cmd_status,
         "preview": cmd_preview, "run": cmd_run, "show": cmd_show}[args.cmd](args)
    finally:
        postgres.close_pool()


if __name__ == "__main__":
    main()
