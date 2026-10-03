"""The history index behind "similar past tickets".

    python scripts/similar.py status              # is the index built? how many drafts have matches?
    python scripts/similar.py sync                # bring the index in line: add what is missing, remove the rest
    python scripts/similar.py build               # full rebuild -- only when the cleaning changes
    python scripts/similar.py attach              # find matches for pending drafts that have none
    python scripts/similar.py query --ticket 2026092082000057   # look at the matches for any ticket

build replaces the whole index, so it is safe to re-run -- after a new
search_text version, or once more closed tickets have been exported.
It needs the embedding service at ds.t2.sa; while that is down it fails with
a timeout and changes nothing.

query takes a live ticket number or a closed one. A closed ticket is searched
without itself, which makes it the quickest way to judge match quality: pick a
closed ticket you know, and see whether its neighbours are the same problem.
"""

from __future__ import annotations

import argparse
import sys

from ole5.db import postgres
from ole5.history import precedent, similar


def cmd_status(_: argparse.Namespace) -> None:
    n = precedent.index_size(fresh=True)
    # Real closed tickets only: junk has its own index.
    ready = len(precedent.wanted())
    drafts = postgres.query_one(
        """
        SELECT count(*) FILTER (WHERE status = 'pending') AS pending,
               count(*) FILTER (WHERE status = 'pending' AND EXISTS
                   (SELECT 1 FROM draft_similar s WHERE s.draft_id = d.id)) AS with_matches
        FROM drafts d
        """
    )
    print(f"index:  {n} tickets indexed, {ready} real closed tickets to index")
    try:
        c = precedent.compare()
        state = "in step" if not (c["missing"] or c["extra"] or c["doubled"]) else \
            f"{c['missing']} missing, {c['extra']} to remove, {c['doubled']} held twice -- run sync"
        print(f"        should hold {c['should_hold']}; {state}")
    except Exception as exc:
        print(f"        could not compare ({type(exc).__name__})")
    print(f"drafts: {drafts['with_matches']} of {drafts['pending']} pending drafts have matches")
    for r in postgres.query(
        "SELECT coalesce(similar_status, 'not recorded') AS s, count(*) AS n "
        "FROM drafts WHERE status = 'pending' GROUP BY 1 ORDER BY 1"
    ):
        print(f"        {r['n']:4}  {r['s']}")
    if n == 0:
        print("\nThe index is empty. Run: python scripts/similar.py build")


def cmd_sync(_: argparse.Namespace) -> None:
    """Bring the similar-tickets index in line with the history. Only the
    tickets it lacks are embedded."""
    before = precedent.compare()
    print(f"before: {before['indexed']} indexed, should hold {before['should_hold']} "
          f"-- {before['missing']} missing, {before['extra']} to remove")
    out = precedent.sync()
    print(f"synced: {out['added']} added, {out['removed']} removed -- now {out['total']} tickets")


def cmd_build(_: argparse.Namespace) -> None:
    print("indexing closed tickets (this calls the embedding service)...")
    n = precedent.build_index()
    print(f"indexed {n} tickets")
    print("\nNext: python scripts/similar.py attach   (matches for drafts made before now)")


def cmd_attach(_: argparse.Namespace) -> None:
    if precedent.index_size(fresh=True) == 0:
        sys.exit("The index is empty. Run build first.")
    rows = postgres.query(
        """
        SELECT d.id, d.ticket_id, t.ticket_number FROM drafts d
        JOIN tickets t ON t.id = d.ticket_id
        WHERE d.status = 'pending'
          AND NOT EXISTS (SELECT 1 FROM draft_similar s WHERE s.draft_id = d.id)
        ORDER BY d.id
        """
    )
    total = 0
    for r in rows:
        n = similar.attach(r["id"], r["ticket_id"])
        total += n
        print(f"  draft {r['id']}  ticket {r['ticket_number']}: {n} matches")
    print(f"\n{len(rows)} drafts, {total} matches stored")


def cmd_query(args: argparse.Namespace) -> None:
    live = postgres.query_one("SELECT search_text FROM tickets WHERE ticket_number = %s",
                              (args.ticket,))
    closed = postgres.query_one("SELECT search_text FROM closed_tickets WHERE ticket_number = %s",
                                (args.ticket,))
    row = live or closed
    if row is None:
        sys.exit(f"no ticket {args.ticket}")
    text = row["search_text"] or ""
    print("=" * 80)
    print(f"ticket {args.ticket} ({'live' if live else 'closed'})\n")
    print(text[:800] or "(no search text)")
    print("=" * 80)

    if precedent.index_size(fresh=True) == 0:
        sys.exit("\nThe index is empty. Run build first.")
    matches = similar.find(text, limit=args.limit, exclude=None if live else args.ticket)
    if not matches:
        print("\nno matches")
        return
    for m in matches:
        full = postgres.query_one("SELECT search_text FROM closed_tickets WHERE id = %s",
                                  (m["id"],))["search_text"] or ""
        print(f"\n#{m['rank']}  {m['ticket_number']}  score {m['score']:.3f}  "
              f"{m['queue'] or '-'}  ·  {m['subtype'] or '-'}")
        print("    " + full[:300].replace("\n", "\n    "))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("build")
    sub.add_parser("sync")
    sub.add_parser("attach")
    q = sub.add_parser("query")
    q.add_argument("--ticket", required=True)
    q.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()
    try:
        {"status": cmd_status, "build": cmd_build, "sync": cmd_sync,
         "attach": cmd_attach, "query": cmd_query}[args.cmd](args)
    finally:
        postgres.close_pool()


if __name__ == "__main__":
    main()
