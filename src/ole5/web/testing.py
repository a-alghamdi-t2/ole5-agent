"""The Testing page: create a ticket in the intake queue from the console.

A temporary page for the team to try the agent without OTRS access. They type a
title and a message, and it arrives in the Support queue exactly like a
customer's email; the poller then drafts it as usual.

Off unless TESTING_PAGE=true, and it refuses to run against production OTRS
whatever the setting says. Creating a test ticket is a write to OTRS, so it is
for staging only.

To remove the page, delete this file, testing.js and testing.css, then the lines
marked "testing page" in app.py, config.py and index.html.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from ole5.clients.otrs import OtrsClient, OtrsError
from ole5.config import get_settings
from ole5.db import audit, postgres
from ole5.logging import get_logger
from ole5.web import auth

log = get_logger(__name__)
router = APIRouter(prefix="/api/testing")

# Hosts never written to from this page, whatever TESTING_PAGE says.
PRODUCTION_HOSTS = {"otrs.t2.sa"}

ACTION = "test_ticket_created"


class NewTicket(BaseModel):
    title: str = Field(min_length=1, max_length=250)
    body: str = Field(min_length=1, max_length=20000)


def _blocked() -> str | None:
    """Why the page is closed, or None when it may be used."""
    s = get_settings()
    if not s.testing_page:
        return "The Testing page is turned off (TESTING_PAGE in .env)."
    if not s.otrs_configured:
        return "OTRS is not configured."
    url = (s.otrs_base_url or "").strip().lower()
    if any(h in url for h in PRODUCTION_HOSTS):
        return "The app is pointed at production OTRS. Test tickets are only created on staging."
    # Fail closed: if the OTRS cannot be told apart, nothing is sent.
    if not _host():
        return f"Cannot tell which OTRS OTRS_BASE_URL points at ({s.otrs_base_url!r})."
    return None


def _host() -> str:
    """The OTRS host, read leniently: spaces, quotes and a missing https://
    in OTRS_BASE_URL are all forgiven."""
    url = (get_settings().otrs_base_url or "").strip().strip("'\"").strip()
    if url and "://" not in url:
        url = "https://" + url
    return (urlparse(url).hostname or "").lower()


@router.get("")
async def status(_: dict = Depends(auth.require)) -> dict:
    blocked = _blocked()
    return {"on": get_settings().testing_page, "enabled": blocked is None, "reason": blocked,
            "queue": get_settings().otrs_intake_queue, "otrs": _host()}


@router.get("/tickets")
async def recent(_: dict = Depends(auth.require)) -> list[dict]:
    """Test tickets sent from this page, newest first, with how far each got."""
    rows = await run_in_threadpool(postgres.query, """
        SELECT a.created_at, a.reasoning AS title,
               a.evidence->>'number' AS number, a.evidence->>'by' AS by,
               d.id AS draft_id, d.status::text AS draft_status,
               d.action::text AS action, d.queue
        FROM audit_log a
        LEFT JOIN tickets t ON t.ticket_number = a.evidence->>'number'
        LEFT JOIN LATERAL (
            SELECT * FROM drafts WHERE ticket_id = t.id ORDER BY id DESC LIMIT 1
        ) d ON true
        WHERE a.action = %s
        ORDER BY a.id DESC
        LIMIT 20
    """, (ACTION,))
    return [{**r, "created_at": r["created_at"].isoformat()} for r in rows]


@router.post("/tickets")
async def create(payload: NewTicket, reviewer: dict = Depends(auth.require)) -> dict:
    blocked = _blocked()
    if blocked:
        raise HTTPException(status_code=403, detail=blocked)

    title, body = payload.title.strip(), payload.body.strip()
    if not title or not body:
        raise HTTPException(status_code=422, detail="A title and a message are both needed.")

    s = get_settings()
    email = reviewer["email"]

    def send() -> tuple[str, str]:
        with OtrsClient() as otrs:
            return otrs.create(title=title, queue=s.otrs_intake_queue,
                               customer_user=email, body=body)

    try:
        ticket_id, number = await run_in_threadpool(send)
    except OtrsError as exc:
        log.warning("test ticket not created", extra={"error": str(exc)[:300]})
        raise HTTPException(status_code=502, detail=f"OTRS refused it: {exc}")

    await run_in_threadpool(lambda: audit.record(
        actor="reviewer", action=ACTION, reasoning=title,
        evidence={"by": email, "number": number, "otrs_ticket_id": ticket_id,
                  "queue": s.otrs_intake_queue, "otrs": _host()}))
    log.info("test ticket created", extra={"number": number, "by": email})
    return {"number": number, "otrs_ticket_id": ticket_id, "queue": s.otrs_intake_queue}
