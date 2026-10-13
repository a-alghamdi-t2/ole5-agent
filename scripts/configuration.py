"""The Configuration page's values as a file: save them, or replace them all.

    python scripts/configuration.py export products/ole5/configuration.json
    python scripts/configuration.py load products/rich/configuration.json --yes

A new product's database starts with OLE5's values (migrations 0001 and 0010
put them there). Load the product's own file once, before the agent drafts
anything. Export is also a backup before large edits on the page.

The file, by hand or from an export:

    {
      "guidance": {"queue": "How to choose the queue ...", ...},
      "options": {
        "queue": [
          {"value": "RiCH::Operations", "description": "When ...", "locked": true},
          ...
        ],
        ...
      }
    }

All seven fields need guidance and at least one value: service, queue,
next_state, type, subtype, priority, sla. Every value needs a description.
locked (default false) marks a value the page will not let anyone disable;
FALLBACK_SERVICE, FALLBACK_QUEUE and FALLBACK_SLA must each be a locked value,
and so must "Default" (type), "Other" (subtype), "1. Low" (priority) and
"open" (next_state), which the validator falls back to as well. active
defaults to true.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from support_agent.config import get_settings
from support_agent.db import audit, postgres

KINDS = ("service", "queue", "next_state", "type", "subtype", "priority", "sla")
# What validate.py falls back to besides the product's three.
FIXED_FALLBACKS = {"type": "Default", "subtype": "Other", "priority": "1. Low",
                   "next_state": "open"}


def export(path: Path) -> None:
    guidance = {r["kind"]: r["guidance"] for r in postgres.query(
        "SELECT kind, guidance FROM agent_option_kinds ORDER BY kind")}
    options: dict[str, list[dict]] = {k: [] for k in KINDS}
    for r in postgres.query("SELECT kind, value, description, active, locked FROM agent_options "
                            "ORDER BY kind, position, id"):
        options.setdefault(r["kind"], []).append(
            {"value": r["value"], "description": r["description"],
             "locked": r["locked"], "active": r["active"]})
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"guidance": guidance, "options": options},
                               ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    n = sum(len(v) for v in options.values())
    print(f"saved {n} values and {len(guidance)} guidance texts to {path}")


def problems(data: dict) -> list[str]:
    """Everything wrong with the file, so it can be fixed in one go."""
    out: list[str] = []
    guidance, options = data.get("guidance") or {}, data.get("options") or {}
    s = get_settings()
    must_lock = {"service": s.fallback_service, "queue": s.fallback_queue,
                 "sla": s.fallback_sla, **FIXED_FALLBACKS}
    for kind in KINDS:
        if not str(guidance.get(kind) or "").strip():
            out.append(f"{kind}: no guidance")
        values = options.get(kind) or []
        if not values:
            out.append(f"{kind}: no values")
        seen = set()
        for i, o in enumerate(values, 1):
            value = str(o.get("value") or "").strip()
            if not value:
                out.append(f"{kind} #{i}: no value")
                continue
            if value.casefold() in seen:
                out.append(f"{kind}: {value!r} twice")
            seen.add(value.casefold())
            if not str(o.get("description") or "").strip():
                out.append(f"{kind}: {value!r} has no description")
        fallback = must_lock.get(kind)
        if fallback and not any(str(o.get("value")).strip() == fallback and o.get("locked")
                                and o.get("active", True) for o in values):
            out.append(f"{kind}: {fallback!r} must be a locked, active value "
                       f"(the validator falls back to it)")
    for kind in set(options) | set(guidance):
        if kind not in KINDS:
            out.append(f"{kind}: not one of the seven fields")
    return out


def load(path: Path, yes: bool) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    wrong = problems(data)
    if wrong:
        sys.exit("Not loaded. Fix these first:\n  " + "\n  ".join(wrong))
    current = postgres.query_one("SELECT count(*) AS n FROM agent_options")["n"]
    if not yes:
        sys.exit(f"This replaces all {current} values on the Configuration page with the "
                 f"{sum(len(data['options'][k]) for k in KINDS)} in {path}. "
                 "Run again with --yes to do it.")
    with postgres.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM agent_options")
            for kind in KINDS:
                cur.execute(
                    "INSERT INTO agent_option_kinds (kind, guidance) VALUES (%s, %s) "
                    "ON CONFLICT (kind) DO UPDATE SET guidance = EXCLUDED.guidance",
                    (kind, data["guidance"][kind].strip()))
                for position, o in enumerate(data["options"][kind], 1):
                    cur.execute(
                        "INSERT INTO agent_options (kind, value, description, active, locked, position) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (kind, o["value"].strip(), o["description"].strip(),
                         bool(o.get("active", True)), bool(o.get("locked", False)), position))
    audit.record(actor="reviewer", action="configuration_loaded", reasoning=str(path),
                 evidence={"replaced": current,
                           "loaded": {k: len(data["options"][k]) for k in KINDS}})
    print(f"loaded {path}: " + ", ".join(f"{k} {len(data['options'][k])}" for k in KINDS))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("export").add_argument("file", type=Path)
    p = sub.add_parser("load")
    p.add_argument("file", type=Path)
    p.add_argument("--yes", action="store_true", help="replace without asking")
    args = ap.parse_args()
    try:
        if args.cmd == "export":
            export(args.file)
        else:
            load(args.file, args.yes)
    finally:
        postgres.close_pool()


if __name__ == "__main__":
    main()
