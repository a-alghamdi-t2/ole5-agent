"""A new product's closed tickets, from OTRS into the history, all at once.

    python scripts/import_history.py count                      # how many, year by year; stores nothing
    python scripts/import_history.py run --limit 20             # a first small batch
    python scripts/import_history.py run                        # all of them
    python scripts/import_history.py run --since 2024-01-01     # only those closed since then
    python scripts/import_history.py values                     # the field values the history uses
    python scripts/import_history.py values --out values.txt    # the same, into a file
    python scripts/import_history.py values --since 2025-10-01  # only tickets closed since then: what is in use now

The tickets closed in HISTORY_QUEUES, read from the history OTRS
(HISTORY_OTRS_BASE_URL, or OTRS_BASE_URL when that is unset). Each is stored
as the Friday run stores the week's: the ticket and the articles somebody
wrote. Read-only against OTRS, and resumable: a ticket already stored is
skipped, so a stopped run carries on where it left off.

Then, in this order:

    python scripts/search_text.py boilerplate   # learn the repeated lines (signatures, disclaimers)
    python scripts/search_text.py fill          # the text tickets are compared on
    python scripts/export_timelines.py          # each ticket's cleaned timeline (reads OTRS again)
    python scripts/extract_journeys.py          # what happened on each ticket (the journey model)
    python scripts/extract_scopes.py            # what each team did on each ticket
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

from support_agent.config import get_settings
from support_agent.db import postgres
from support_agent.history import weekly
from support_agent.history import store as history
from support_agent.intake.contracts import IncomingTicket

PAUSE = 0.4          # between reads, as the other exports do, to spare their API
FIELDS = ("service", "queue", "type", "subtype", "priority", "sla", "state")


EARLIEST = datetime(2000, 1, 1, tzinfo=UTC)   # without --since: everything
MAX_HITS = 10000                               # the most one OTRS search returns


def years(since: datetime) -> list[tuple[datetime, datetime]]:
    """Calendar years from `since` to now."""
    out, start, now = [], since, datetime.now(UTC)
    while start < now:
        end = datetime(start.year + 1, 1, 1, tzinfo=UTC)
        out.append((start, min(end, now)))
        start = end
    return out


def months(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    out = []
    while start < end:
        nxt = datetime(start.year + (start.month == 12), start.month % 12 + 1, 1, tzinfo=UTC)
        out.append((start, min(nxt, end)))
        start = nxt
    return out


def closed_in(otrs, start: datetime, end: datetime) -> list[str]:
    """The ticket ids closed in the window. A year is one search; a year with
    more than one search can return is searched month by month instead."""
    ids = weekly.closed_between(otrs, start, end)
    time.sleep(PAUSE)
    if len(ids) < MAX_HITS:
        return ids
    out: list[str] = []
    for a, b in months(start, end):
        part = weekly.closed_between(otrs, a, b)
        if len(part) >= MAX_HITS:
            print(f"  {a:%Y-%m}: more than {MAX_HITS:,} in one month; some are missing")
        out += part
        time.sleep(PAUSE)
    return out


def _since(text: str | None) -> datetime:
    if not text:
        return EARLIEST
    try:
        return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=UTC)
    except ValueError:
        sys.exit(f"--since {text!r}: write the date as 2024-01-01")


def _header() -> None:
    s = get_settings()
    print(f"product   {s.product_name}")
    print(f"OTRS      {s.history_otrs_base_url or s.otrs_base_url}")
    print(f"queues    {', '.join(s.history_queue_list)}\n")


def cmd_count(args: argparse.Namespace) -> None:
    _header()
    total = 0
    with weekly._otrs() as otrs:
        for start, end in years(_since(args.since)):
            ids = closed_in(otrs, start, end)
            total += len(ids)
            if ids or total:          # nothing printed before the first ticket
                print(f"  {start:%Y}  {len(ids):7,}", flush=True)
    stored = postgres.query_one("SELECT count(*) AS n FROM closed_tickets")["n"]
    print(f"\n{total:,} closed tickets in OTRS; {stored:,} already stored here")


def cmd_run(args: argparse.Namespace) -> None:
    _header()
    have = {str(r["otrs_ticket_id"]) for r in
            postgres.query("SELECT otrs_ticket_id FROM closed_tickets")}
    stored = skipped = failed = 0
    with weekly._otrs() as otrs:
        for start, end in years(_since(args.since)):
            ids = closed_in(otrs, start, end)
            if not ids:
                continue
            new = [i for i in ids if i not in have]
            skipped += len(ids) - len(new)
            print(f"{start:%Y}: {len(ids)} closed, {len(new)} to store", flush=True)
            for tid in new:
                if args.limit and stored >= args.limit:
                    break
                try:
                    raw = otrs._call("TicketGet", {"TicketID": tid, "AllArticles": 1,
                                                   "DynamicFields": 1, "Extended": 1})
                    payload = raw["Ticket"][0]
                    saved = history.save(IncomingTicket.model_validate(payload), payload)
                except Exception as exc:
                    failed += 1
                    print(f"  OTRS id {tid} FAILED {type(exc).__name__}: {str(exc)[:120]}")
                    continue
                have.add(tid)
                stored += 1
                print(f"  {saved.ticket_number}  {(payload.get('Queue') or '')[:40]:40} "
                      f"{saved.kept} kept, {saved.dropped} noise", flush=True)
                time.sleep(PAUSE)
            if args.limit and stored >= args.limit:
                print(f"\nstopped at --limit {args.limit}")
                break
    print(f"\n{stored} stored, {skipped} were already here, {failed} failed")
    if failed:
        print("Run the same command again to retry the failed ones.")


def cmd_values(args: argparse.Namespace) -> None:
    """Every value each field takes in the stored history, most used first:
    what the Configuration page should offer."""
    lines: list[str] = []
    where, params = "", ()
    if args.since:
        where, params = "WHERE otrs_closed_at >= %s", (_since(args.since),)
    n = postgres.query_one(f"SELECT count(*) AS n FROM closed_tickets {where}", params)["n"]
    lines.append(f"{n} closed tickets" + (f" closed since {args.since}" if args.since else "") + "\n")
    for field in FIELDS:
        rows = postgres.query(
            f"SELECT coalesce({field}, '(empty)') AS v, count(*) AS n FROM closed_tickets {where} "
            f"GROUP BY 1 ORDER BY 2 DESC, 1", params)
        lines.append(field.upper())
        lines += [f"  {r['n']:6}  {r['v']}" for r in rows]
        lines.append("")
    text = "\n".join(lines)
    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(f"written to {args.out}")
    else:
        print(text)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("count")
    c.add_argument("--since", help="2024-01-01; default: everything")
    r = sub.add_parser("run")
    r.add_argument("--since", help="2024-01-01; default: everything")
    r.add_argument("--limit", type=int)
    v = sub.add_parser("values")
    v.add_argument("--out")
    v.add_argument("--since", help="only tickets closed since this date, 2025-10-01")
    args = ap.parse_args()
    try:
        {"count": cmd_count, "run": cmd_run, "values": cmd_values}[args.cmd](args)
    finally:
        postgres.close_pool()


if __name__ == "__main__":
    main()
