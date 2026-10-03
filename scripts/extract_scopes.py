"""Evidence for the team scopes: what each Ole5 team did on each closed ticket.

    python scripts/extract_scopes.py --try 2026022682000639     # one ticket through the model; stores nothing
    python scripts/extract_scopes.py --limit 20                 # a batch, stored
    python scripts/extract_scopes.py                            # every ticket not done yet
    python scripts/extract_scopes.py --redo-fallback            # the fallback's results, again with the main model
    python scripts/extract_scopes.py --stats
    python scripts/extract_scopes.py --export scopes            # one text file per team, for writing the scopes

Reads ticket_timelines, so nothing here calls OTRS. Junk is skipped: it says
nothing about what a team does. Only the four Ole5 New teams are counted.

Resumable: a ticket already done is skipped unless --redo or --redo-fallback.
The model is JOURNEY_MODEL with JOURNEY_FALLBACK_MODEL, as for the journeys.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

from ole5.db import postgres
from ole5.history import journey, scopes

NOT_JUNK = "position('junk' in lower(coalesce(t.queue, ''))) = 0"

TITLES = {
    "operations": "Operations (Ole5 New::Operations)",
    "product": "Product (Ole5 New::Product)",
    "product_support": "Product Support (Ole5 New::Product Support)",
    "customer_success": "Customer Success (Ole5 New::Customer Success)",
}


def cmd_try(number: str) -> None:
    row = postgres.query_one("SELECT id FROM closed_tickets WHERE ticket_number = %s", (number,))
    if row is None:
        sys.exit(f"no closed ticket {number}")
    ticket, tl = journey.load(row["id"])
    work, teams, meta = scopes.extract(ticket, tl)
    print(f"teams involved (from the records): {', '.join(teams) or 'none'}\n")
    print(json.dumps({k: v.model_dump() for k, v in work.items()}, ensure_ascii=False, indent=2))
    fell = f", after the main model failed: {meta['fallback_reason'][:120]}" \
        if meta["fallback_reason"] else ""
    print(f"\nwritten by {meta['model']}{fell}, {meta['latency_ms'] / 1000:.1f}s. Nothing stored.")


def cmd_run(args: argparse.Namespace) -> None:
    sql = f"""
        SELECT t.id, t.ticket_number FROM ticket_timelines tl
        JOIN closed_tickets t ON t.id = tl.closed_ticket_id
        WHERE {NOT_JUNK} AND jsonb_array_length(tl.entries) > 0
    """
    if args.redo_fallback:
        sql += (" AND EXISTS (SELECT 1 FROM ticket_scopes s WHERE s.closed_ticket_id = t.id "
                "AND s.fallback_reason IS NOT NULL)")
    elif not args.redo:
        sql += " AND NOT EXISTS (SELECT 1 FROM ticket_scopes s WHERE s.closed_ticket_id = t.id)"
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
            work, teams, meta = scopes.extract(ticket, tl)
            scopes.save(r["id"], work, teams, meta)
        except (scopes.ScopesFailed, journey.JourneyFailed) as exc:
            failed += 1
            print(f"  [{i}/{len(rows)}] {r['ticket_number']} FAILED {str(exc)[:90]}")
            continue
        done += 1
        fell_back += bool(meta["fallback_reason"])
        said = [k for k, w in work.items() if w.did or w.passed_on]
        print(f"  [{i}/{len(rows)}] {r['ticket_number']}  "
              f"{meta['model'].split('/')[-1]:14} {', '.join(said) or '-'}", flush=True)
        if meta["fallback_reason"]:
            print(f"      main model failed: {meta['fallback_reason'][:140]}")
        time.sleep(args.pause)
    print(f"\n{done} extracted ({fell_back} by the fallback), {failed} failed")


def cmd_stats() -> None:
    total = postgres.query_one(
        f"SELECT count(*) AS n FROM closed_tickets t WHERE {NOT_JUNK}")["n"]
    done = postgres.query_one(
        "SELECT count(DISTINCT closed_ticket_id) AS n FROM ticket_scopes")["n"]
    print(f"{done} of {total} real closed tickets extracted\n")
    print(f"{'team':18} {'involved':>9} {'did':>6} {'passed_on':>10}")
    for r in postgres.query(
        """
        SELECT team, count(*) FILTER (WHERE involved) AS involved,
               count(did) AS did, count(passed_on) AS passed_on
        FROM ticket_scopes GROUP BY team ORDER BY team
        """
    ):
        print(f"{r['team']:18} {r['involved']:9} {r['did']:6} {r['passed_on']:10}")
    fb = postgres.query_one("SELECT count(DISTINCT closed_ticket_id) AS n FROM ticket_scopes "
                            "WHERE fallback_reason IS NOT NULL")["n"]
    print(f"\n{fb} tickets written by the fallback model")


def cmd_export(folder: str) -> None:
    """One text file per team: every line of evidence, with its ticket, for a
    person (or Claude) to turn into that team's scope."""
    out = Path(folder)
    out.mkdir(parents=True, exist_ok=True)
    for team, title in TITLES.items():
        rows = postgres.query(
            """
            SELECT c.ticket_number, c.subtype, s.did, s.passed_on
            FROM ticket_scopes s JOIN closed_tickets c ON c.id = s.closed_ticket_id
            WHERE s.team = %s AND (s.did IS NOT NULL OR s.passed_on IS NOT NULL)
            ORDER BY c.otrs_closed_at DESC NULLS LAST
            """, (team,))
        lines = [
            f"# {title}",
            "",
            f"Evidence from {len(rows)} closed tickets: what this team did on each, and",
            "what it passed to another team and why. Written per ticket by a model",
            "from the ticket's history; only tickets the records show this team took",
            "part in are included.",
            "",
        ]
        for r in rows:
            head = f"[{r['ticket_number']}]" + (f" {r['subtype']}" if r["subtype"] else "")
            lines.append(head)
            if r["did"]:
                lines.append(f"  DID: {r['did']}")
            if r["passed_on"]:
                lines.append(f"  PASSED ON: {r['passed_on']}")
        path = out / f"{team}.txt"
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"  {path}  {len(rows)} tickets, {path.stat().st_size // 1024} KB")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--try", dest="try_", metavar="TICKET")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--redo", action="store_true")
    ap.add_argument("--redo-fallback", action="store_true")
    ap.add_argument("--pause", type=float, default=5.0)
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--export", metavar="FOLDER")
    args = ap.parse_args()
    try:
        if args.try_:
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
