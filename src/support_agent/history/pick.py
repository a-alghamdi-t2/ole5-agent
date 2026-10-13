"""Which closed tickets are worth a model's time, when not all of them are.

Journeys and team-scope evidence cost two model calls a ticket. For a product
with thousands of closed tickets, this picks the N that teach the most, from
the stored timelines alone (no model, no OTRS), so the same N come out every
time and the journeys and the scopes are written for the same tickets.

A ticket is worth more when:
    it moved between teams        the handovers, and why, are what the team
                                  scopes are made of (+3)
    people actually worked on it  replies and internal notes, up to six (+0.5 each)
    the customer got an answer    something to learn the fix from (+2)
    it is recent                  how the teams work now (up to +1)
and worth less when:
    it is an internal ticket      raised by staff, not a customer's problem (-2)
    almost nothing happened       one message or none (-3)
    it repeats a title            the tenth "delivery report" teaches little the
                                  first did not: each repeat of the same title
                                  past the third loses 1.5 more

Picked in this order:
    1. every ticket that moved between teams: few, and the most telling
    2. each team's own best, up to `floor`, so a small team (one customer's
       queue, say) is not crowded out by the busiest one
    3. the best of everything left, until there are N
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime

from support_agent.db import postgres
from support_agent.history import scopes

_DIGITS = re.compile(r"[0-9٠-٩]+")
_SPACE = re.compile(r"\s+")


def _title_key(title: str | None) -> str:
    """The title without numbers and case: copies of one notice share it."""
    t = _DIGITS.sub("#", (title or "").lower())
    return _SPACE.sub(" ", re.sub(r"^(re|fw|fwd)\s*:\s*", "", t)).strip()[:80]


def candidates(since: datetime | None) -> list[dict]:
    """Every real closed ticket with a timeline, with what the score needs."""
    where = "AND t.otrs_closed_at >= %s" if since else ""
    return postgres.query(
        f"""
        SELECT t.id, t.ticket_number, t.title, t.queue, t.type, t.otrs_closed_at,
               tl.queue_path,
               count(*) FILTER (WHERE e->>'kind' = 'reply') AS replies,
               count(*) FILTER (WHERE e->>'kind' = 'note')  AS notes,
               count(*) FILTER (WHERE e->>'kind' <> 'queue') AS messages
        FROM ticket_timelines tl
        JOIN closed_tickets t ON t.id = tl.closed_ticket_id
        CROSS JOIN LATERAL jsonb_array_elements(tl.entries) e
        WHERE position('junk' in lower(coalesce(t.queue, ''))) = 0 {where}
        GROUP BY t.id, tl.queue_path
        """, (since,) if since else ())


def moved(r: dict) -> bool:
    """Passed between two of the product's teams. Arriving from the intake
    queue is not a move between teams."""
    teams = scopes.team_map()
    return len({teams[q] for q in (r["queue_path"] or []) if q in teams}) > 1


def score(r: dict, oldest: datetime, newest: datetime) -> float:
    s = 0.0
    if moved(r):
        s += 3
    s += 0.5 * min(r["replies"] + r["notes"], 6)
    if r["replies"]:
        s += 2
    if (r["type"] or "").lower().startswith("internal"):
        s -= 2
    if r["messages"] <= 1:
        s -= 3
    span = (newest - oldest).total_seconds()
    if span > 0 and r["otrs_closed_at"]:
        s += (r["otrs_closed_at"] - oldest).total_seconds() / span
    return s


def best(n: int, since: datetime | None = None, floor: int = 100) -> tuple[list[dict], dict]:
    """The n tickets worth the most, most valuable first, and a summary."""
    rows = candidates(since)
    if not rows:
        return [], {"candidates": 0}
    dates = [r["otrs_closed_at"] for r in rows if r["otrs_closed_at"]]
    oldest, newest = min(dates), max(dates)
    for r in rows:
        r["score"] = score(r, oldest, newest)
    rows.sort(key=lambda r: r["score"], reverse=True)

    # Repeats: past the third ticket with the same title, each loses more.
    seen: Counter = Counter()
    for r in rows:
        key = _title_key(r["title"])
        seen[key] += 1
        if key and seen[key] > 3:
            r["score"] -= 1.5 * (seen[key] - 3)
    rows.sort(key=lambda r: r["score"], reverse=True)

    picked: dict[int, dict] = {}
    for r in rows:
        if len(picked) >= n:
            break
        if moved(r):
            picked[r["id"]] = r
    teams = scopes.team_map()
    per_team: dict[str, int] = {}
    for queue, key in teams.items():
        got = 0
        for r in rows:
            if got >= floor or len(picked) >= n:
                break
            if r["queue"] == queue or queue in (r["queue_path"] or []):
                got += 1
                picked.setdefault(r["id"], r)
        per_team[key] = per_team.get(key, 0) + got
    for r in rows:
        if len(picked) >= n:
            break
        picked.setdefault(r["id"], r)

    chosen = sorted(picked.values(), key=lambda r: r["score"], reverse=True)
    involved = Counter()
    for r in chosen:
        for queue, key in teams.items():
            if r["queue"] == queue or queue in (r["queue_path"] or []):
                involved[key] += 1
    summary = {
        "candidates": len(rows),
        "picked": len(chosen),
        "moved": sum(1 for r in chosen if moved(r)),
        "answered": sum(1 for r in chosen if r["replies"]),
        "internal": sum(1 for r in chosen if (r["type"] or "").lower().startswith("internal")),
        "per_team": dict(involved),
        "lowest_score": round(chosen[-1]["score"], 2) if chosen else None,
    }
    return chosen, summary


def print_summary(summary: dict) -> None:
    print(f"picked {summary['picked']} of {summary['candidates']} tickets: "
          f"{summary['moved']} moved between teams, {summary['answered']} answered the customer, "
          f"{summary['internal']} internal")
    for key, n in sorted(summary.get("per_team", {}).items(), key=lambda kv: -kv[1]):
        print(f"  {key:24} {n}")
    print()
