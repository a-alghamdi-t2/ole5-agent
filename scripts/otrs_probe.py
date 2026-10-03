"""Exercise the client against staging.

Usage: python scripts/otrs_probe.py [--create] [--write TICKET_ID]

Read-only by default. --create makes one test ticket in the intake queue;
--write posts a note and moves that ticket, which is what an approved draft
will do.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from ole5.clients.otrs import OtrsClient, OtrsError  # noqa: E402
from ole5.config import get_settings  # noqa: E402
from ole5.logging import setup_logging  # noqa: E402
from ole5.db import postgres  # noqa: E402
from ole5.orchestrator import note as note_builder  # noqa: E402
from ole5.orchestrator.contracts import Decision  # noqa: E402
BODY = """السلام عليكم،

ما هو أقصى حجم لرفع المرفقات في OLE 5؟

شكرا لكم
"""


def main() -> int:
    setup_logging()
    s = get_settings()
    print(f"base: {s.otrs_base_url}/{s.otrs_webservice}")
    print(f"user: {s.otrs_user}\n")

    with OtrsClient() as otrs:
        try:
            waiting = otrs.intake()
            print(f"intake ({s.otrs_intake_queue}, new): {len(waiting)} waiting")
            if waiting:
                print(f"  {waiting[:10]}")
        except OtrsError as exc:
            print(f"  FAILED {exc}")
            return 1

        if waiting:
            ticket = otrs.get(waiting[0])
            print(f"\nTicketGet {waiting[0]}")
            print(f"  {ticket.TicketNumber}  {ticket.Queue}  {ticket.State}  "
                  f"{ticket.Type}  {ticket.Priority}")
            print(f"  requester {ticket.requester_email}, "
                  f"{len(ticket.Article)} article(s)")
            print(f"  service={ticket.Service!r} subtype={ticket.dynamic('SubType')!r}")

        if "--create" in sys.argv:
            print("\nTicketCreate")
            ticket_id, number = otrs.create(
                title="اختبار وكيل OLE5 - حجم المرفقات",
                queue=s.otrs_intake_queue,
                customer_user="a.alghamdi@t2.sa",
                sender="a.alghamdi@t2.sa",
                body=BODY,
            )
            print(f"  {number} (TicketID {ticket_id})")
            fresh = otrs.get(ticket_id)
            print(f"  reads back {fresh.Queue} / {fresh.State} / "
                  f"{len(fresh.Article)} article(s)")

        if "--write" in sys.argv:
            target = sys.argv[sys.argv.index("--write") + 1]
            print(f"\nTicketUpdate {target}: note plus queue move")
            article_id = otrs.apply_decision(
                target,
                note_subject="OLE5 agent - routing recommendation",
                note_body=("Posted by the OLE5 agent client.\n\n"
                           "Note, queue move and classification in one call."),
                queue="Ole5 New::Product Support",
                state="Waiting for Concerned Department",
                type_="Issue / Problem",
                priority="1. Low",
                subtype="OLE5::Reset Password::No Password received",
                conclusions="Issue - account locked after failed sign-in attempts",
            )
            after = otrs.get(target)
            print(f"  article {article_id}")
            print(f"  now {after.Queue} / {after.State} / "
                  f"subtype={after.dynamic('SubType')!r}")
        if "--note" in sys.argv:
            args = sys.argv[sys.argv.index("--note") + 1:]
            draft_id, target = int(args[0]), args[1]

            row = postgres.query_one("SELECT * FROM drafts WHERE id = %s",
                                     (draft_id,))
            decision = Decision.model_validate({
                k: row[k] for k in (
                    "action", "reply_kind", "reply_body", "queue", "next_state",
                    "type", "subtype", "conclusions", "priority", "sla",
                    "service", "issue_type", "summary", "collected", "missing",
                    "note", "confidence", "reasoning")
            })

            print(f"\nposting draft {draft_id} to ticket {target}")
            article_id = otrs.apply_decision(
                target,
                note_subject=note_builder.subject(decision),
                note_body=note_builder.render(decision, draft_id=draft_id,
                                              evidence=row["evidence"]),
                queue=decision.queue,
                state=decision.next_state,
                type_=decision.type,
                priority=decision.priority,
                subtype=decision.subtype,
                conclusions=decision.conclusions,
            )
            print(f"  article {article_id}")

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        postgres.close_pool()