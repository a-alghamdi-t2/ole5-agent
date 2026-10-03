"""Build the text tickets are compared on.

    python scripts/search_text.py boilerplate   # learn repeated lines from history
    python scripts/search_text.py fill          # search_text for every ticket
    python scripts/search_text.py preview       # raw next to cleaned, a few samples
    python scripts/search_text.py preview --ticket 2026083182000403
    python scripts/search_text.py stats         # how much was removed, what broke
    python scripts/search_text.py audit         # removals that look like a real problem

Run boilerplate before fill: fill removes what boilerplate learned. Re-running
either is safe; boilerplate replaces the whole table and fill overwrites.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter, defaultdict

from ole5.db import postgres
from ole5.intake.search_text import (
    INTERNAL_DOMAINS, MIN_LINE, MIN_SEEN, MIN_SHARE, SEARCH_VER,
    Boilerplate, build, candidate_lines, domain_of, line_key,
)

# The first article of each closed ticket. Noise articles were dropped at
# export, so the lowest article_no is the customer's first message.
FIRST_CLOSED = """
    SELECT DISTINCT ON (t.id)
           t.id, t.ticket_number, a.sender_address, a.body_raw
    FROM closed_tickets t
    JOIN closed_articles a ON a.closed_ticket_id = t.id
    ORDER BY t.id, a.article_no NULLS LAST, a.id
"""

FIRST_LIVE = """
    SELECT DISTINCT ON (t.id)
           t.id, t.ticket_number, a.sender_address, a.body_raw
    FROM tickets t
    JOIN articles a ON a.ticket_id = t.id AND a.party = 'request'
    ORDER BY t.id, a.article_no NULLS LAST, a.id
"""

GARBLED = re.compile(r"\?{4,}")


def _keys(body: str) -> dict[str, str]:
    """Each distinct line of a body, keyed, counted once per ticket."""
    out: dict[str, str] = {}
    for line in candidate_lines(body):
        key = line_key(line)
        if len(key) >= MIN_LINE:
            out.setdefault(key, line)
    return out


def _learn(groups: dict[str, list[dict[str, str]]], scope: str) -> list[tuple]:
    """Lines frequent enough within one group to be boilerplate."""
    rows = []
    for key, bodies in groups.items():
        total = len(bodies)
        if total < MIN_SEEN:
            continue
        counts: Counter[str] = Counter()
        sample: dict[str, str] = {}
        for lines in bodies:
            counts.update(lines.keys())
            for k, v in lines.items():
                sample.setdefault(k, v)
        for k, n in counts.items():
            if n >= MIN_SEEN and n / total >= MIN_SHARE:
                rows.append((scope, key, k, sample[k][:1000], n, total))
    return rows


def cmd_boilerplate(_: argparse.Namespace) -> None:
    by_domain: dict[str, list] = defaultdict(list)
    by_sender: dict[str, list] = defaultdict(list)

    for r in postgres.query(FIRST_CLOSED):
        sender = (r["sender_address"] or "").strip().lower()
        domain = domain_of(sender)
        if not sender or domain in INTERNAL_DOMAINS:
            continue
        lines = _keys(r["body_raw"])
        by_sender[sender].append(lines)
        if domain:
            by_domain[domain].append(lines)

    # Every reply our own staff wrote, not only first articles: a signature is
    # learned from where it is written, and removed wherever it is quoted.
    by_staff: dict[str, list] = defaultdict(list)
    placeholders = ", ".join(["%s"] * len(INTERNAL_DOMAINS))
    for r in postgres.query(
        f"""
        SELECT lower(sender_address) AS sender, body_clean, body_raw
        FROM closed_articles
        WHERE split_part(lower(sender_address), '@', 2) IN ({placeholders})
        """,
        INTERNAL_DOMAINS,
    ):
        by_staff[r["sender"]].append(_keys(r["body_clean"] or r["body_raw"]))

    rows = (_learn(by_domain, "domain") + _learn(by_sender, "sender")
            + _learn(by_staff, "internal"))

    with postgres.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM boilerplate_lines")
            cur.executemany(
                """
                INSERT INTO boilerplate_lines
                    (scope, key, line_key, line, seen_in, out_of)
                VALUES (%s, %s, %s, %s, %s, %s)
                """,
                rows,
            )

    per_scope = Counter(r[0] for r in rows)
    print(f"learned {len(rows)} lines: " +
          ", ".join(f"{n} {s}" for s, n in sorted(per_scope.items())))
    print(f"from {len(by_domain)} domains, {len(by_sender)} senders, "
          f"{len(by_staff)} staff")


def _all_boilerplate() -> tuple[set[str], dict[str, set[str]], dict[str, set[str]]]:
    """The whole table in memory, so fill does one query rather than 1,600."""
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
    return internal, domains, senders


def _for(sender: str | None, table) -> Boilerplate:
    internal, domains, senders = table
    sender = (sender or "").strip().lower()
    return Boilerplate(frozenset(
        internal | senders.get(sender, set()) | domains.get(domain_of(sender) or "", set())
    ))


def cmd_fill(_: argparse.Namespace) -> None:
    table = _all_boilerplate()
    for name, sql, target in (("closed", FIRST_CLOSED, "closed_tickets"),
                              ("live", FIRST_LIVE, "tickets")):
        rows = postgres.query(sql)
        updates = [(build(r["body_raw"], _for(r["sender_address"], table), sender=r["sender_address"]),
                    SEARCH_VER, r["id"]) for r in rows]
        with postgres.connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(
                    f"UPDATE {target} SET search_text = %s, search_ver = %s WHERE id = %s",
                    updates,
                )
        empty = sum(1 for u in updates if not u[0])
        print(f"{name}: {len(updates)} tickets filled, {empty} came out empty")


def cmd_preview(args: argparse.Namespace) -> None:
    table = _all_boilerplate()
    sql = FIRST_CLOSED
    params: tuple = ()
    filtered = args.short or args.garbled
    if args.ticket:
        sql = f"SELECT * FROM ({FIRST_CLOSED}) x WHERE ticket_number = %s"
        params = (args.ticket,)
    elif filtered:
        # Every ticket, filtered after cleaning: the length is only known then.
        sql = f"SELECT * FROM ({FIRST_CLOSED}) x ORDER BY ticket_number"
    else:
        sql = f"SELECT * FROM ({FIRST_CLOSED}) x ORDER BY random() LIMIT %s"
        params = (args.samples,)

    shown = 0
    for r in postgres.query(sql, params):
        text = build(r["body_raw"], _for(r["sender_address"], table), sender=r["sender_address"])
        raw = r["body_raw"] or ""
        if args.short and len(text) >= 40:
            continue
        if args.garbled and not GARBLED.search(raw):
            continue
        if filtered and shown >= args.samples:
            break
        shown += 1

        print("=" * 80)
        print(f"ticket {r['ticket_number']}  ·  {r['sender_address']}  ·  "
              f"{len(raw)} -> {len(text)} chars")
        if args.raw or args.short:
            print("-" * 80, "\nRAW\n")
            print(raw)
        if args.garbled:
            # Only the damaged lines: enough to tell a disclaimer from the
            # customer's own words.
            print("-" * 80, "\nLINES WITH ????\n")
            for line in raw.splitlines():
                if GARBLED.search(line):
                    print("  " + line.strip()[:160])
        print("-" * 80, "\nSEARCH TEXT\n")
        print(text or "(empty)")
    print("=" * 80)


# A customer describing a problem. Not proof of a lost sentence, but where to look.
PROBLEM = re.compile(
    r"error|issue|problem|cannot|can't|can not|unable|not working|doesn't|does not|"
    r"didn't|fail|wrong|slow|missing|مشكل|خطأ|خلل|عطل|اشكال|إشكال|لا يمكن|لايمكن|"
    r"لا يعمل|لايعمل|تعذر|لا تظهر|لا يظهر|لم يتم|لا نستطيع|لانستطيع|بطء|بطئ",
    re.I)
# ...unless it is disclaimer or signature wording that happens to say "error".
NOT_CUSTOMER = re.compile(
    r"virus|فيروس|فايروس|liability|مسؤولية|مسئولية|intended|المقصود|recipient|"
    r"المرسل إليه|confidential|سرية|privileged|transmitted|disclose|الإفصاح",
    re.I)


def _suspicious(rule: str, text: str) -> bool:
    """Worth a human look.

    A dropped disclaimer is suspicious when it is short -- real ones run to
    hundreds of characters -- or when it describes a problem with little
    disclaimer wording around it. The disclaimer words cannot simply exclude a
    hit here: a customer writing about the "intended recipient" field uses them.
    """
    if rule == "disclaimer":
        return len(text) < 200 or (bool(PROBLEM.search(text))
                                   and len(NOT_CUSTOMER.findall(text)) <= 1)
    return bool(PROBLEM.search(text)) and not NOT_CUSTOMER.search(text)


def cmd_audit(args: argparse.Namespace) -> None:
    """What the bulk-removing rules took out that reads like a customer's problem."""
    table = _all_boilerplate()
    rows = postgres.query(FIRST_CLOSED)
    found: dict[str, list[tuple[str, str]]] = defaultdict(list)
    removed: Counter[str] = Counter()
    for r in rows:
        trace: list = []
        build(r["body_raw"], _for(r["sender_address"], table), trace, sender=r["sender_address"])
        for rule, text in trace:
            removed[rule] += 1
            if _suspicious(rule, text):
                found[rule].append((r["ticket_number"], text))

    print(f"{len(rows)} closed tickets\n")
    for rule in ("disclaimer", "after --", "staff quote"):
        hits = found.get(rule, [])
        tickets = len({t for t, _ in hits})
        print(f"{rule:12}  {removed[rule]:6} pieces removed, "
              f"{len(hits)} look like a problem, in {tickets} tickets")
        for t, text in hits[: args.samples]:
            print(f"    {t}  {text[:150]}")
        print()
    print("A hit is not proof of a lost sentence -- a staff reply saying "
          "'the issue is fixed' is expected under 'staff quote'. Judge the lines.")


def cmd_stats(_: argparse.Namespace) -> None:
    table = _all_boilerplate()
    rows = postgres.query(FIRST_CLOSED)
    before = after = empty = short = garbled = 0
    for r in rows:
        raw = r["body_raw"] or ""
        text = build(raw, _for(r["sender_address"], table), sender=r["sender_address"])
        before += len(raw)
        after += len(text)
        empty += not text
        short += 0 < len(text) < 40
        garbled += bool(GARBLED.search(raw))
    n = len(rows) or 1
    print(f"{len(rows)} closed tickets")
    print(f"  average length   {before // n} -> {after // n} chars "
          f"({round(100 * after / max(before, 1))}% kept)")
    print(f"  came out empty   {empty}")
    print(f"  under 40 chars   {short}   (worth a look: may be over-cleaned)")
    print(f"  raw has ????     {garbled}   (text lost at export, not recoverable here)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("boilerplate")
    sub.add_parser("fill")
    p = sub.add_parser("preview")
    p.add_argument("--samples", type=int, default=5)
    p.add_argument("--ticket")
    p.add_argument("--raw", action="store_true", help="also print the raw body")
    p.add_argument("--short", action="store_true",
                   help="only tickets whose text came out under 40 characters, with the raw body")
    p.add_argument("--garbled", action="store_true",
                   help="only tickets whose raw body has ????, with the damaged lines")
    sub.add_parser("stats")
    a = sub.add_parser("audit")
    a.add_argument("--samples", type=int, default=15)
    args = ap.parse_args()

    try:
        {"boilerplate": cmd_boilerplate, "fill": cmd_fill,
         "preview": cmd_preview, "stats": cmd_stats, "audit": cmd_audit}[args.cmd](args)
    finally:
        # Without this the pool's threads outlive main and print warnings.
        postgres.close_pool()


if __name__ == "__main__":
    sys.exit(main())