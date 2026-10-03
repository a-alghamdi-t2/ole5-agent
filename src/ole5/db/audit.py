"""Append-only audit log recording decisions and contextual details 
   (model, prompt version, inputs, latency) for later troubleshooting. 
   It exists for console review and is not read by the active pipeline.
"""

from __future__ import annotations

import json
from typing import Any

from ole5.db import postgres
from ole5.logging import get_logger

log = get_logger(__name__)


def record(
    *,
    actor: str,
    action: str,
    ticket_id: int | None = None,
    draft_id: int | None = None,
    reasoning: str | None = None,
    evidence: dict[str, Any] | None = None,
    model: str | None = None,
    prompt_ver: str | None = None,
    latency_ms: int | None = None,
) -> None:
    """Writes a single log entry. It never raises exceptions, 
       ensuring that a failure in auditing does not result in a lost decision"""
    try:
        postgres.execute(
            """
            INSERT INTO audit_log (
                ticket_id, draft_id, actor, action, reasoning, evidence,
                model, prompt_ver, latency_ms
            ) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s)
            """,
            (
                ticket_id, draft_id, actor, action, reasoning,
                json.dumps(evidence or {}, ensure_ascii=False, default=str),
                model, prompt_ver, latency_ms,
            ),
        )
    except Exception:
        log.exception("audit write failed", extra={"actor": actor, "action": action})