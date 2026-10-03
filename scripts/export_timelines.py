"""Fetch closed tickets from OTRS again and store each as a cleaned timeline.

    python scripts/export_timelines.py --show 2026080282001065   # one ticket's timeline, from OTRS; stores nothing
    python scripts/export_timelines.py --sample 20               # 20 random tickets, summary line each; stores nothing
    python scripts/export_timelines.py --limit 50                # a batch, stored
    python scripts/export_timelines.py                           # every ticket not done yet
    python scripts/export_timelines.py --redo                    # again, even if done
    python scripts/export_timelines.py --stats                   # what is stored, most common paths
    python scripts/export_timelines.py --show 2026080282001065 --dropped   # also what was dropped as empty
    python scripts/export_timelines.py --empties 10              # what was dropped, on the 10 tickets that lost most

For every ticket in closed_tickets: fetches all its articles, keeps what people
wrote (customer messages, replies, internal notes) and the queue notifications
(reduced to the queue each names), drops generated noise, cleans each message,
and stores the lot in ticket_timelines with the exact queue path. The input
for the journey step. closed_articles is not touched.

Read-only against OTRS. Resumable: a ticket already stored is skipped unless
--redo, so a stopped run carries on where it left off.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter, defaultdict

from ole5.clients.otrs import OtrsClient, OtrsError
from ole5.db import postgres
from ole5.history import timeline
from ole5.intake.search_text import Boilerplate, domain_of

PAUSE = 0.4          # between reads, as the export does, so we do not lean on their API


# ---------------------------------------------------------------------------
# the boilerplate table, loaded once
# ---------------------------------------------------------------------------

def _boilerplate_loader():
    internal: set[str] = set()
    domains: dict[str, set[str]] = defaultdict(set)
    senders: dict[str, set[str]] = defaultdict(set)
    for r in postgres.query("SELECT scope, key, line_key FROM boilerplate_lines"):
        if r["scope"] == "internal":
            internal.add(r["line_key"])
        elif r["scope"] == "domain":
            domains[r["key"]].add(r["line_key"])
        else:
            senders[r["key"]].add(r["line_key"])
    cache: dict[str | None, Boilerplate] = {}

    def for_sender(address: str | None) -> Boilerplate:
        key = (address or "").strip().lower()
        if key not in cache:
            cache[key] = Boilerplate(frozenset(
                internal | senders.get(key, set()) | domains.get(domain_of(key) or "", set())))
        return cache[key]
    return for_sender


def _fetch(otrs: OtrsClient, otrs_ticket_id: str) -> list[dict]:
    raw = otrs._call("TicketGet", {"TicketID": otrs_ticket_id, "AllArticles": 1})
    return (raw.get("Ticket") or [{}])[0].get("Article") or []


def _route(tl: timeline.Timeline) -> str:
    return " → ".join(q.split("::")[-1] for q in tl.queue_path) or "(no queue notifications)"


def _save(closed_ticket_id: int, tl: timeline.Timeline) -> None:
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


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def _print_dropped(tl: timeline.Timeline, width: int = 160) -> None:
    for d in tl.dropped:
        before = " ".join(d["before"].split())
        print(f"  [{d['no']}] {d['kind']:8} {str(d['from'] or '')[:22]:22} "
              f"{before[:width] or '(nothing written above the quote)'}")


def cmd_show(number: str, dropped: bool = False) -> None:
    ticket = postgres.query_one("SELECT * FROM closed_tickets WHERE ticket_number = %s",
                                (number,))
    if ticket is None:
        sys.exit(f"no closed ticket {number}")
    with OtrsClient() as otrs:
        arts = _fetch(otrs, ticket["otrs_ticket_id"])
    tl = timeline.build(arts, _boilerplate_loader())
    text = timeline.render(ticket, tl.entries, tl.queue_path)
    print(text)
    print(f"--- {tl.article_count} articles in OTRS: {len(tl.entries) - tl.notification_count} "
          f"messages kept, {tl.notification_count} queue notifications, {tl.noise_count} noise, "
          f"{tl.empty_count} empty after cleaning. {len(text):,} characters "
          f"(about {len(text) // 3:,} tokens). Nothing stored.")
    if dropped:
        print(f"\nDROPPED AS EMPTY ({len(tl.dropped)}), with what the message said before cleaning:")
        _print_dropped(tl)


def cmd_empties(n: int) -> None:
    """What was dropped as empty on the tickets that lost the most messages."""
    rows = postgres.query(
        """
        SELECT t.ticket_number, t.otrs_ticket_id, tl.empty_count
        FROM ticket_timelines tl JOIN closed_tickets t ON t.id = tl.closed_ticket_id
        ORDER BY tl.empty_count DESC LIMIT %s
        """, (n,))
    bp = _boilerplate_loader()
    with OtrsClient() as otrs:
        for r in rows:
            try:
                tl = timeline.build(_fetch(otrs, r["otrs_ticket_id"]), bp)
            except OtrsError as exc:
                print(f"{r['ticket_number']}  FAILED {str(exc)[:70]}")
                continue
            print(f"\n{r['ticket_number']}  ({len(tl.dropped)} dropped as empty)")
            _print_dropped(tl, width=120)
            time.sleep(PAUSE)
    print("\nEach line is what the message said before cleaning. Anything that "
          "describes the problem or a decision was dropped wrongly.")


def cmd_sample(n: int) -> None:
    rows = postgres.query("SELECT id, otrs_ticket_id, ticket_number, queue FROM closed_tickets "
                          "ORDER BY random() LIMIT %s", (n,))
    bp = _boilerplate_loader()
    with_path = 0
    with OtrsClient() as otrs:
        for r in rows:
            try:
                tl = timeline.build(_fetch(otrs, r["otrs_ticket_id"]), bp)
            except OtrsError as exc:
                print(f"  {r['ticket_number']}  FAILED {str(exc)[:70]}")
                continue
            with_path += bool(tl.queue_path)
            kept = len(tl.entries) - tl.notification_count
            print(f"  {r['ticket_number']}  {tl.article_count:4} articles → {kept:3} kept, "
                  f"{tl.notification_count:2} notif, {tl.noise_count:4} noise   {_route(tl)}")
            time.sleep(PAUSE)
    print(f"\n{with_path} of {len(rows)} have queue notifications. Nothing stored.")


def cmd_run(args: argparse.Namespace) -> None:
    sql = """
        SELECT t.id, t.otrs_ticket_id, t.ticket_number FROM closed_tickets t
        LEFT JOIN ticket_timelines tl ON tl.closed_ticket_id = t.id
    """
    if not args.redo:
        sql += " WHERE tl.closed_ticket_id IS NULL"
    sql += " ORDER BY t.id"
    params: tuple = ()
    if args.limit:
        sql += " LIMIT %s"
        params = (args.limit,)
    rows = postgres.query(sql, params)
    print(f"{len(rows)} tickets to fetch\n")

    bp = _boilerplate_loader()
    done = failed = 0
    with OtrsClient() as otrs:
        for i, r in enumerate(rows, 1):
            try:
                tl = timeline.build(_fetch(otrs, r["otrs_ticket_id"]), bp)
                _save(r["id"], tl)
            except (OtrsError, KeyError, ValueError) as exc:
                failed += 1
                print(f"  [{i}/{len(rows)}] {r['ticket_number']} FAILED {str(exc)[:70]}")
                continue
            done += 1
            print(f"  [{i}/{len(rows)}] {r['ticket_number']}  "
                  f"{len(tl.entries) - tl.notification_count} kept  {_route(tl)}")
            time.sleep(PAUSE)
    print(f"\n{done} stored, {failed} failed")


def cmd_stats() -> None:
    r = postgres.query_one(
        """
        SELECT (SELECT count(*) FROM closed_tickets) AS tickets, count(*) AS stored,
               count(*) FILTER (WHERE cardinality(queue_path) > 0) AS with_path,
               count(*) FILTER (WHERE cardinality(queue_path) > 1) AS moved,
               sum(kept_count - notification_count) AS messages,
               sum(noise_count) AS noise, sum(empty_count) AS empty
        FROM ticket_timelines
        """
    )
    print(f"{r['stored']} of {r['tickets']} closed tickets stored")
    print(f"  {r['with_path']} have a queue path, {r['moved']} of them changed queue")
    print(f"  {r['messages']} messages kept, {r['noise']} noise dropped, "
          f"{r['empty']} empty after cleaning\n")
    routes = Counter(" → ".join(q.split("::")[-1] for q in row["queue_path"])
                     for row in postgres.query(
                         "SELECT queue_path FROM ticket_timelines WHERE cardinality(queue_path) > 0"))
    print("most common paths:")
    for route, n in routes.most_common(20):
        print(f"  {n:5}  {route}")


def _say_source() -> None:
    """Which OTRS this run reads from. The stored ticket ids belong to the
    instance the history was exported from; read another and nothing matches."""
    from ole5.config import get_settings
    print(f"reading from OTRS: {get_settings().otrs_base_url}\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--show", metavar="TICKET")
    ap.add_argument("--dropped", action="store_true",
                    help="with --show: also list what was dropped as empty")
    ap.add_argument("--empties", type=int, metavar="N",
                    help="what was dropped on the N tickets with the most empties")
    ap.add_argument("--sample", type=int)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()
    try:
        if not args.stats:
            _say_source()
        if args.show:
            cmd_show(args.show, args.dropped)
        elif args.empties:
            cmd_empties(args.empties)
        elif args.sample:
            cmd_sample(args.sample)
        elif args.stats:
            cmd_stats()
        else:
            cmd_run(args)
    finally:
        postgres.close_pool()


if __name__ == "__main__":
    main()
