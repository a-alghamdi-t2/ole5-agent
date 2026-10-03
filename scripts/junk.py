"""The junk index: is a new ticket the kind of thing the team throws away?

    python scripts/junk.py status                  # how much junk is stored and indexed
    python scripts/junk.py dedupe                  # check Junk tickets once: kept, or a repeat
    python scripts/junk.py sync                    # bring the index in line: add kept, remove repeats/copies
    python scripts/junk.py build                   # full rebuild -- only when the cleaning changes
    python scripts/junk.py calibrate               # measure, then choose JUNK_THRESHOLD
    python scripts/junk.py check --ticket 2026092382000024   # try one ticket, live or closed
    python scripts/junk.py senders                 # domains that only ever sent junk
    python scripts/junk.py roc --sample 300        # the margin by ROC, tested on held-out tickets (hours; resumable)
    python scripts/junk.py roc --report-only       # the same analysis again from the saved scores

calibrate is the important one. It scores a sample of junk tickets and a
sample of real ones against the index -- each junk ticket without itself -- and
prints what each threshold would catch and what it would wrongly flag. Pick the
threshold from that table and put it in .env as JUNK_THRESHOLD.

A false positive here marks a real customer's ticket as spam, so the table is
worth reading before choosing.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

from ole5.config import get_settings
from ole5.db import postgres
from ole5.history import junk


def cmd_status(_: argparse.Namespace) -> None:
    r = postgres.query_one(
        f"""
        SELECT count(*) FILTER (WHERE {junk.IS_JUNK}) AS junk,
               count(*) FILTER (WHERE {junk.IS_TRUE_JUNK}
                    AND length(coalesce(search_text, '')) >= %s) AS ready,
               count(*) FILTER (WHERE NOT ({junk.IS_JUNK})) AS real
        FROM closed_tickets
        """, (junk.MIN_TEXT,))
    s = get_settings()
    print(f"junk queue: {r['junk']} tickets, {r['ready']} kept after the check -- the ones indexed")
    print(f"real tickets:        {r['real']}")
    print(f"indexed:             {junk.index_size()} chunks")
    try:
        c = junk.compare()
        state = "in step" if not (c["missing"] or c["extra"] or c["doubled"]) else \
            f"{c['missing']} missing, {c['extra']} to remove, {c['doubled']} held twice -- run sync"
        print(f"index vs checks:     should hold {c['should_hold']}; {state}")
    except Exception as exc:
        print(f"index vs checks:     could not compare ({type(exc).__name__})")
    _breakdown()
    print(f"threshold:           {s.junk_threshold} "
          f"(sender rule: {s.junk_sender_min}+ junk and no real)")


def cmd_dedupe(args: argparse.Namespace) -> None:
    """Check every Junk ticket not checked yet: kept, or a repeat of junk
    already kept. Resumable; oldest first. No search, no model: instant."""
    import time as _t

    left = postgres.query_one(
        f"SELECT count(*) AS n FROM closed_tickets WHERE {junk.IS_JUNK} AND junk_check IS NULL")["n"]
    print(f"{left} Junk tickets to check" + (f" (this run: up to {args.limit})" if args.limit else ""))
    print("A fingerprint comparison in the database: no search, no model.\n")
    started = _t.monotonic()
    searched = {"n": 0}

    def progress(i, n, number, verdict, ref, score):
        if i % 500 == 0 or i == n:
            print(f"  {i}/{n} checked", flush=True)

    out = junk.check_pending(args.limit, progress)
    print(f"\nchecked {out['checked']}: kept {out['kept']}, repeats {out['repeat']}")
    _breakdown()
    print("\nNext: python scripts/junk.py sync   (removes repeats, adds kept ones)")


def _breakdown() -> None:
    rows = postgres.query(
        f"SELECT coalesce(junk_check, 'not checked') AS c, count(*) AS n FROM closed_tickets "
        f"WHERE {junk.IS_JUNK} GROUP BY 1 ORDER BY 2 DESC")
    print("\nJunk queue, by check:")
    for r in rows:
        print(f"  {r['n']:6}  {r['c']}")


def cmd_sync(_: argparse.Namespace) -> None:
    """Bring the junk index in line with the checks: add kept tickets it lacks,
    remove repeats. Only additions are embedded."""
    before = junk.compare()
    print(f"before: {before['indexed']} indexed, should hold {before['should_hold']} "
          f"-- {before['missing']} missing, {before['extra']} to remove")
    out = junk.sync()
    print(f"synced: {out['added']} added, {out['removed']} removed -- now {out['total']} tickets")


def cmd_build(_: argparse.Namespace) -> None:
    print("indexing junk tickets (this calls the embedding service)...")
    print(f"indexed {junk.build_index()} tickets")


def _sample(is_junk: bool, n: int) -> list[dict]:
    return postgres.query(
        f"""
        SELECT ticket_number, search_text, requester_email FROM closed_tickets
        WHERE {junk.IS_TRUE_JUNK if is_junk else 'NOT (' + junk.IS_JUNK + ')'}
          AND length(coalesce(search_text, '')) >= %s
        ORDER BY random() LIMIT %s
        """, (junk.MIN_TEXT, n))


def cmd_calibrate(args: argparse.Namespace) -> None:
    if junk.index_size() == 0:
        sys.exit("The junk index is empty. Run build first.")

    print(f"scoring {args.sample} junk and {args.sample} real tickets "
          "against the junk index...\n")
    # Each ticket scored against both indexes, without itself: a ticket is in
    # its own index and would match itself at 1.0.
    junk_pairs, real_pairs, worst = [], [], []
    for row in _sample(True, args.sample):
        hits = junk.search(row["search_text"], exclude=row["ticket_number"])
        j = hits[0].score if hits else 0.0
        junk_pairs.append((j, junk.real_score(row["search_text"]) if hits else 0.0))
    for row in _sample(False, args.sample):
        hits = junk.search(row["search_text"])
        j = hits[0].score if hits else 0.0
        r = junk.real_score(row["search_text"], exclude=row["ticket_number"]) if hits else 0.0
        real_pairs.append((j, r))
        worst.append((j - r, j, r, row["ticket_number"],
                      " ".join((row["search_text"] or "").split())[:80]))
    junk_scores = [j for j, _ in junk_pairs]
    real_scores = [j for j, _ in real_pairs]

    def spread(name: str, xs: list[float]) -> None:
        xs = sorted(xs)
        if not xs:
            print(f"{name}: nothing scored")
            return
        pick = lambda q: xs[min(len(xs) - 1, int(len(xs) * q))]   # noqa: E731
        print(f"{name}: lowest {xs[0]:.2f}  10% {pick(.1):.2f}  median {pick(.5):.2f}  "
              f"90% {pick(.9):.2f}  highest {xs[-1]:.2f}")

    spread("junk tickets", junk_scores)
    spread("real tickets", real_scores)

    print("\nthreshold   junk caught        real wrongly flagged")
    for t in [x / 100 for x in range(40, 100, 5)]:
        caught = sum(1 for s in junk_scores if s >= t)
        wrong = sum(1 for s in real_scores if s >= t)
        print(f"   {t:.2f}     {caught:4}/{len(junk_scores):<4} "
              f"({100 * caught / max(len(junk_scores), 1):3.0f}%)   "
              f"{wrong:4}/{len(real_scores):<4} "
              f"({100 * wrong / max(len(real_scores), 1):3.0f}%)")

    s = get_settings()
    # The floor and the margin together: for each pair, how much junk is
    # caught and how many real tickets are wrongly flagged. The near-duplicate
    # rule (junk >= threshold and ahead of real) applies in every cell.
    floors = (0.10, 0.15, 0.20, 0.25, 0.30)
    margins = (0.05, 0.10, 0.15, 0.20, 0.25)

    def flagged(j: float, r: float, floor: float, m: float) -> bool:
        if j >= s.junk_threshold and j > r:
            return True
        return j >= floor and j - r >= m

    print(f"\nnear-duplicates: junk >= {s.junk_threshold} and ahead of real, in every cell")
    print("each cell: junk caught % / real wrongly flagged (count)\n")
    print("floor \\ margin " + "".join(f"{m:>12.2f}" for m in margins))
    for f in floors:
        cells = []
        for m in margins:
            caught = sum(1 for j, r in junk_pairs if flagged(j, r, f, m))
            wrong = sum(1 for j, r in real_pairs if flagged(j, r, f, m))
            cells.append(f"{100 * caught / max(len(junk_pairs), 1):>5.0f}% / {wrong:<3}")
        mark = "  <- current floor" if abs(f - s.junk_floor) < 1e-9 else ""
        print(f"   {f:.2f}        " + "".join(f"{c:>12}" for c in cells) + mark)
    print(f"\ncurrent margin: {s.junk_margin}. Pick the cell with the most junk "
          "caught and 0 real flagged.")

    print("\nThe real tickets closest to being flagged (junk score minus real score):")
    for gap, j, r, number, text in sorted(worst, reverse=True)[:8]:
        rule = junk.decide(j, r) or "not flagged"
        print(f"  junk {j:.2f}  real {r:.2f}  {rule:11}  {number}  {text}")
    print("\nPick a threshold that catches most junk with none of these, and "
          "put it in .env as JUNK_THRESHOLD. Nothing was stored.")


# ---------------------------------------------------------------------------
# ROC: choosing the margin properly
# ---------------------------------------------------------------------------
#
# The live rule flags a ticket when it is a near-duplicate of junk (junk score
# >= JUNK_THRESHOLD and ahead of real), or when its junk score reaches
# JUNK_FLOOR and beats its real score by JUNK_MARGIN. With the threshold and
# the floor fixed, the margin is the one free parameter -- and "junk score
# minus real score" is the score it cuts. So the ROC curve is the curve of
# that cut.
#
# Method: a labelled sample of true junk and real tickets, each scored against
# both indexes (without itself). Stratified split: 70% to choose the margin,
# 30% held out to test it. On the choosing part, the margin with the highest
# junk caught at a false-positive rate no higher than --target-fpr. On the
# held-out part, that margin's rates with 95% confidence intervals. Scores are
# saved as they are computed, so a stopped run resumes, and the analysis can
# be repeated from the saved scores with --report-only.

import csv
import json
import math
import random
import time as _time


def _roc_sample(n: int, seed: int) -> list[tuple[str, str, str, str]]:
    """(label, ticket_number, search_text, sender) -- the same tickets for the same seed."""
    out = []
    for label, where in (("junk", junk.IS_TRUE_JUNK), ("real", f"NOT ({junk.IS_JUNK})")):
        rows = postgres.query(
            f"""
            SELECT ticket_number, search_text, requester_email FROM closed_tickets
            WHERE {where} AND length(coalesce(search_text, '')) >= %s
            ORDER BY md5(ticket_number || %s) LIMIT %s
            """, (junk.MIN_TEXT, str(seed), n))
        out += [(label, r["ticket_number"], r["search_text"], r["requester_email"]) for r in rows]
    return out


def _load_scores(path: Path) -> dict[str, dict]:
    done = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                row = json.loads(line)
                done[row["ticket"]] = row
    return done


def _flag(j: float, r: float, floor: float, margin: float, threshold: float) -> bool:
    """The live rule (ole5.history.junk.decide), with the margin as a parameter."""
    if j >= threshold and j > r:
        return True
    return j >= floor and j - r >= margin


def _rates(rows: list[dict], floor: float, margin: float, threshold: float) -> tuple[int, int, int, int]:
    tp = sum(1 for x in rows if x["label"] == "junk" and _flag(x["j"], x["r"], floor, margin, threshold))
    fp = sum(1 for x in rows if x["label"] == "real" and _flag(x["j"], x["r"], floor, margin, threshold))
    return tp, sum(1 for x in rows if x["label"] == "junk"), fp, sum(1 for x in rows if x["label"] == "real")


def _wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% interval for a proportion -- sensible even at 0 or n, unlike +/- 2 SE."""
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (max(0.0, centre - half), min(1.0, centre + half))


def _curve(rows: list[dict], floor: float, threshold: float) -> list[tuple[float, float, float]]:
    """(margin, false-positive rate, true-positive rate), from strictest to loosest."""
    cuts = sorted({round(x["j"] - x["r"], 6) for x in rows}, reverse=True)
    margins = [math.inf] + cuts + [-math.inf]
    pts = []
    for m in margins:
        tp, pos, fp, neg = _rates(rows, floor, m, threshold)
        pts.append((m, fp / max(neg, 1), tp / max(pos, 1)))
    return pts


def _auc(rows: list[dict]) -> float:
    """AUC of the score the margin cuts, junk minus real: the chance a random
    junk ticket scores higher than a random real one (ties count half).

    Computed over every pair rather than as the area under the rule's curve:
    that curve does not run corner to corner -- the near-duplicate rule flags
    some tickets at any margin, and the floor stops others -- so its area is
    not an AUC."""
    pos = [x["j"] - x["r"] for x in rows if x["label"] == "junk"]
    neg = [x["j"] - x["r"] for x in rows if x["label"] == "real"]
    if not pos or not neg:
        return float("nan")
    wins = sum((1.0 if p > n else 0.5 if p == n else 0.0) for p in pos for n in neg)
    return wins / (len(pos) * len(neg))


def cmd_roc(args: argparse.Namespace) -> None:
    s = get_settings()
    floor, threshold = args.floor if args.floor is not None else s.junk_floor, s.junk_threshold
    path = Path(args.scores)

    # ---- scoring (resumable)
    if not args.report_only:
        if junk.index_size() == 0:
            sys.exit("The junk index is empty. Run build first.")
        sample = _roc_sample(args.sample, args.seed)
        done = _load_scores(path)
        todo = [t for t in sample if t[1] not in done]
        print(f"{len(sample)} tickets in the sample ({args.sample} of each), "
              f"{len(sample) - len(todo)} already scored, {len(todo)} to go.")
        if todo:
            print("Each takes two searches; the ETA settles after the first few.\n")
        started = _time.monotonic()
        with path.open("a", encoding="utf-8") as out:
            for i, (label, number, text, _sender) in enumerate(todo, 1):
                own = number   # never compared with itself, in either index
                hits = junk.search(text, exclude=own)
                j = hits[0].score if hits else 0.0
                r = junk.real_score(text, exclude=own)
                out.write(json.dumps({"ticket": number, "label": label, "j": j, "r": r}) + "\n")
                out.flush()
                if i % 10 == 0 or i == len(todo):
                    per = (_time.monotonic() - started) / i
                    left = per * (len(todo) - i)
                    print(f"  {i}/{len(todo)}  {per:.1f}s each, about {left / 60:.0f} min left", flush=True)

    rows = list(_load_scores(path).values())
    junk_rows = [x for x in rows if x["label"] == "junk"]
    real_rows = [x for x in rows if x["label"] == "real"]
    if len(junk_rows) < 20 or len(real_rows) < 20:
        sys.exit(f"Too few scored tickets to analyse ({len(junk_rows)} junk, {len(real_rows)} real).")

    # ---- stratified split: 70% to choose, 30% held out
    rng = random.Random(args.seed)
    rng.shuffle(junk_rows)
    rng.shuffle(real_rows)
    cut_j, cut_r = int(len(junk_rows) * 0.7), int(len(real_rows) * 0.7)
    choose = junk_rows[:cut_j] + real_rows[:cut_r]
    held = junk_rows[cut_j:] + real_rows[cut_r:]

    print(f"\n{len(junk_rows)} junk and {len(real_rows)} real tickets scored.")
    print(f"Choosing on {len(choose)}, testing on {len(held)} held out.")
    print(f"Fixed: near-duplicate threshold {threshold}, floor {floor}.\n")

    # ---- the curve, on everything and on the choosing part
    pts_all = _curve(rows, floor, threshold)
    pts_choose = _curve(choose, floor, threshold)
    print(f"AUC of junk-minus-real (all scored tickets): {_auc(rows):.3f}   "
          "(1.0 separates perfectly, 0.5 is a coin toss)")

    # The operating points worth seeing, from the choosing part.
    print("\nOn the choosing part -- margin, junk caught, real wrongly flagged:")
    shown = set()
    for target in (0.0, 0.005, 0.01, 0.02, 0.05, 0.10):
        ok = [p for p in pts_choose if p[1] <= target + 1e-12]
        if not ok:
            continue
        m, fpr, tpr = max(ok, key=lambda p: (p[2], -p[1]))
        if m in shown or not math.isfinite(m):
            continue
        shown.add(m)
        print(f"  margin {m:6.3f}   caught {100 * tpr:5.1f}%   wrongly flagged {100 * fpr:4.1f}%"
              f"   (best with at most {100 * target:.1f}% wrongly flagged)")

    # ---- the choice: highest detection within the target false-positive rate
    ok = [p for p in pts_choose if p[1] <= args.target_fpr + 1e-12 and math.isfinite(p[0])]
    if not ok:
        sys.exit(f"No margin keeps false positives at or under {args.target_fpr:.1%} on the choosing part.")
    margin, _, _ = max(ok, key=lambda p: (p[2], -p[1], p[0]))
    # Youden's J, for comparison: the point furthest from the diagonal.
    youden = max((p for p in pts_choose if math.isfinite(p[0])), key=lambda p: p[2] - p[1])

    # ---- test on the held-out part
    tp, pos, fp, neg = _rates(held, floor, margin, threshold)
    t_lo, t_hi = _wilson(tp, pos)
    f_lo, f_hi = _wilson(fp, neg)
    print(f"\nChosen margin: {margin:.3f}  "
          f"(most junk caught with at most {args.target_fpr:.1%} wrongly flagged)")
    print(f"  for comparison, Youden's J picks {youden[0]:.3f}")
    print(f"\nOn the {len(held)} held-out tickets it never saw:")
    print(f"  junk caught:          {tp}/{pos} = {100 * tp / max(pos, 1):.1f}%   "
          f"(95% interval {100 * t_lo:.1f}% to {100 * t_hi:.1f}%)")
    print(f"  real wrongly flagged: {fp}/{neg} = {100 * fp / max(neg, 1):.1f}%   "
          f"(95% interval {100 * f_lo:.1f}% to {100 * f_hi:.1f}%)")

    # ---- for plotting
    with open(args.curve, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["margin", "false_positive_rate", "true_positive_rate"])
        for m, fpr, tpr in pts_all:
            w.writerow(["inf" if m == math.inf else "-inf" if m == -math.inf else m, fpr, tpr])
    print(f"\nThe full curve is in {args.curve}, for plotting.")
    print(f"To use it: JUNK_MARGIN={margin:.3f} in .env"
          + (f" and JUNK_FLOOR={floor}" if args.floor is not None else "") + ", then restart.")


def cmd_check(args: argparse.Namespace) -> None:
    row = (postgres.query_one("SELECT ticket_number, search_text, requester_email "
                              "FROM tickets WHERE ticket_number = %s", (args.ticket,))
           or postgres.query_one("SELECT ticket_number, search_text, requester_email "
                                 "FROM closed_tickets WHERE ticket_number = %s",
                                 (args.ticket,)))
    if row is None:
        sys.exit(f"no ticket {args.ticket}")
    v = junk.check(row["search_text"], row["requester_email"],
                   exclude=row["ticket_number"])
    print(f"ticket {args.ticket}  ({row['requester_email']})")
    print(" ".join((row["search_text"] or "").split())[:300] or "(no text)")
    print(f"\nlooks like junk: {v.looks_like_junk}   {v.reason or '-'}")
    print(f"sender's domain: {v.sender_junk} junk, {v.sender_real} real")
    for m in v.matches:
        print(f"  {m.score:.2f}  {m.ticket_number}  {' '.join(m.text.split())[:90]}")


def cmd_senders(args: argparse.Namespace) -> None:
    rows = postgres.query(
        f"""
        SELECT split_part(lower(requester_email), '@', 2) AS domain,
               count(*) FILTER (WHERE {junk.IS_TRUE_JUNK}) AS junk,
               count(*) FILTER (WHERE NOT ({junk.IS_JUNK})) AS real
        FROM closed_tickets WHERE requester_email IS NOT NULL
        GROUP BY 1 HAVING count(*) FILTER (WHERE {junk.IS_TRUE_JUNK}) >= %s
        ORDER BY 2 DESC LIMIT %s
        """, (get_settings().junk_sender_min, args.limit))
    print(f"{'domain':40} {'junk':>6} {'real':>6}   only junk?")
    for r in rows:
        print(f"{r['domain'][:40]:40} {r['junk']:6} {r['real']:6}   "
              f"{'yes' if r['real'] == 0 else ''}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    sub.add_parser("build")
    sub.add_parser("sync", help="add what is missing, remove what should not be there")
    dd = sub.add_parser("dedupe", help="check Junk tickets: kept, or a repeat of one already kept")
    dd.add_argument("--limit", type=int, help="check at most this many, then stop")
    c = sub.add_parser("calibrate")
    c.add_argument("--sample", type=int, default=150)
    k = sub.add_parser("check")
    k.add_argument("--ticket", required=True)
    rc = sub.add_parser("roc", help="choose the margin by ROC, with a held-out test")
    rc.add_argument("--sample", type=int, default=300, help="tickets of each kind (default 300)")
    rc.add_argument("--seed", type=int, default=7, help="same seed, same tickets -- so it resumes")
    rc.add_argument("--target-fpr", type=float, default=0.01,
                    help="the most real tickets wrongly flagged you accept (default 0.01)")
    rc.add_argument("--floor", type=float, default=None, help="default: JUNK_FLOOR")
    rc.add_argument("--scores", default="junk_roc_scores.jsonl", help="where scores are saved")
    rc.add_argument("--curve", default="junk_roc_curve.csv", help="where the curve is written")
    rc.add_argument("--report-only", action="store_true", help="analyse saved scores, search nothing")
    s = sub.add_parser("senders")
    s.add_argument("--limit", type=int, default=30)
    args = ap.parse_args()
    try:
        {"status": cmd_status, "build": cmd_build, "sync": cmd_sync, "dedupe": cmd_dedupe, "calibrate": cmd_calibrate,
         "check": cmd_check, "senders": cmd_senders, "roc": cmd_roc}[args.cmd](args)
    finally:
        postgres.close_pool()


if __name__ == "__main__":
    main()
