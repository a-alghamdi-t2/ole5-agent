"""Storing a decision as a draft.

One live draft per ticket, enforced by a partial unique index rather than by
code. Writing a new one supersedes any pending one first, with a conditional
update: if a reviewer approved it in the meantime the update touches nothing,
and the approval stands.

Every search the orchestrator ran is kept in the evidence column, not just the
one that decided it -- a search that found nothing is part of why we routed.
"""

from __future__ import annotations

import json

from ole5.config import get_settings
from ole5.db import audit, postgres
from ole5.intake.context import TicketContext
from ole5.knowledge.contracts import Retrieval, strongest
from ole5.logging import get_logger
from ole5.orchestrator.contracts import Decision

log = get_logger(__name__)


def _evidence_json(retrievals: list[Retrieval], corrections: list[str]) -> str:
    s = get_settings()
    return json.dumps(
        {
            "verdict": strongest(retrievals).value if retrievals else "not_in_kb",
            "searches": [r.as_json() for r in retrievals],
            "corrections": corrections,
            # The settings that produced this. A draft made at pool 10 is not
            # comparable with one made at 30, and stop_reason 'max_search_rounds'
            # means we capped it, not that the retriever gave up.
            "settings": {
                "max_rounds": s.kb_max_rounds,
                "candidate_pool": s.kb_candidate_pool,
                "top_k": s.kb_top_k,
            },
        },
        ensure_ascii=False,
        default=str,
    )


def save(
    context: TicketContext,
    decision: Decision,
    retrievals: list[Retrieval],
    corrections: list[str],
    meta: dict,
) -> int:
    """Write the draft. Returns its id."""
    evidence = _evidence_json(retrievals, corrections)

    # The newest article this draft was built from. A later one supersedes it.
    built_to = postgres.query_one(
        """
        SELECT id FROM articles
        WHERE ticket_id = %s
        ORDER BY article_no DESC NULLS LAST, id DESC
        LIMIT 1
        """,
        (context.ticket_id,),
    )

    with postgres.connection() as conn:
        with conn.cursor() as cur:
            # Conditional: only a pending draft can be superseded. If a reviewer
            # approved one a moment ago, this updates nothing and the approval
            # keeps its place in history.
            cur.execute(
                """
                UPDATE drafts SET status = 'superseded'
                WHERE ticket_id = %s AND status = 'pending'
                """,
                (context.ticket_id,),
            )
            superseded = cur.rowcount

            cur.execute(
                """
                INSERT INTO drafts (
                    ticket_id, status, action, reply_kind, reply_body,
                    queue, queue_reason, next_state, type, subtype, conclusions,
                    priority, sla,
                    service, issue_type, summary, collected, missing, note,
                    confidence, reasoning, evidence, model, prompt_ver,
                    latency_ms, built_to_article_id
                ) VALUES (
                    %s, 'pending', %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s,
                    %s, %s, %s, %s::jsonb, %s::jsonb, %s,
                    %s, %s, %s::jsonb, %s, %s,
                    %s, %s
                )
                RETURNING id
                """,
                (
                    context.ticket_id,
                    decision.action.value,
                    decision.reply_kind.value if decision.reply_kind else None,
                    decision.reply_body,
                    decision.queue, decision.queue_reason, decision.next_state,
                    decision.type, decision.subtype, decision.conclusions,
                    decision.priority, decision.sla,
                    decision.service, decision.issue_type, decision.summary,
                    json.dumps(decision.collected, ensure_ascii=False),
                    json.dumps(decision.missing, ensure_ascii=False),
                    decision.note,
                    decision.confidence.value, decision.reasoning, evidence,
                    meta.get("model"), meta.get("prompt_ver"),
                    meta.get("latency_ms"),
                    built_to["id"] if built_to else None,
                ),
            )
            draft_id = cur.fetchone()["id"]

    # What the draft does, as the review page shows it: for a reply, whether
    # it answers or asks for more (reply_kind); otherwise the action (route).
    # Recording only the action logged every ask_more as "answer".
    audit.record(
        actor="orchestrator",
        action=(decision.reply_kind.value if decision.reply_kind
                else decision.action.value),
        ticket_id=context.ticket_id,
        draft_id=draft_id,
        reasoning=decision.reasoning,
        evidence=json.loads(evidence),
        model=meta.get("model"),
        prompt_ver=meta.get("prompt_ver"),
        latency_ms=meta.get("latency_ms"),
    )

    log.info(
        "draft saved",
        extra={"ticket": context.ticket_number, "draft": draft_id,
               "action": decision.action.value, "superseded": superseded},
    )
    return draft_id