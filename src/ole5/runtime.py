"""Background work, inside the server process.

Two loops: one polls the intake queue and drafts, one drains the outbox. Both
run on a worker thread -- deciding a ticket is a minute of model time and must
not touch the event loop -- and both survive their own failures, because a
knowledge base that is down for ten minutes should not stop the console.

Neither loop is safe to run twice. One poll deciding a ticket while another
decides the same one wastes model time and races on the one-pending-draft
index. That is why the server runs a single worker, and why --reload is for
development only.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

from starlette.concurrency import run_in_threadpool

from ole5.config import get_settings
from ole5.logging import get_logger

log = get_logger(__name__)


@dataclass
class LoopState:
    """What a loop has been doing, for /api/health."""

    name: str
    enabled: bool = False
    interval: float = 0.0
    runs: int = 0
    last_run_at: datetime | None = None
    last_error: str | None = None
    last_result: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "interval_seconds": self.interval,
            "runs": self.runs,
            "last_run_at": self.last_run_at.isoformat() if self.last_run_at else None,
            "last_error": self.last_error,
            "last_result": self.last_result,
        }


STATE: dict[str, LoopState] = {}
_TASKS: list[asyncio.Task] = []


async def _loop(state: LoopState, work: Callable[[], dict[str, Any]]) -> None:
    """Run `work` on a thread every `state.interval` seconds, forever.

    The interval is measured between finishing and starting again, not between
    starts: a pass that took four minutes should not immediately trigger the
    four that were due while it ran.
    """
    log.info("loop started", extra={"loop": state.name, "every": state.interval})
    try:
        while True:
            started = datetime.now(UTC)
            try:
                result = await run_in_threadpool(work)
                state.last_error = None
                state.last_result = result
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                state.last_error = f"{type(exc).__name__}: {exc}"[:500]
                log.exception("loop failed", extra={"loop": state.name})
            state.runs += 1
            state.last_run_at = started
            await asyncio.sleep(state.interval)
    except asyncio.CancelledError:
        log.info("loop stopped", extra={"loop": state.name})
        raise


def _poll_once() -> dict[str, Any]:
    from ole5.intake.poller import poll_once

    result = poll_once()
    return {"seen": result.seen, "drafted": result.drafted,
            "skipped": result.skipped, "failed": result.failed,
            "seconds": round(result.seconds, 1)}


def _send_once() -> dict[str, Any]:
    from ole5.review import approve as review

    sent, failed = review.send_pending()
    return {"sent": sent, "failed": failed}


def _weekly_once() -> dict[str, Any]:
    from ole5.history import weekly

    return weekly.maybe_run()


# How often the weekly loop looks at the clock. The run itself happens once a
# week; checking every five minutes means it starts within five of 04:00.
WEEKLY_CHECK_SECONDS = 300.0


def start() -> None:
    """Start whichever loops are configured. Called once, from the lifespan."""
    s = get_settings()

    poller = LoopState("poller", interval=float(s.otrs_poll_seconds))
    outbox = LoopState("outbox", interval=float(s.outbox_seconds))
    weekly = LoopState("weekly", interval=WEEKLY_CHECK_SECONDS)
    STATE["poller"] = poller
    STATE["outbox"] = outbox
    STATE["weekly"] = weekly

    if s.poller_enabled and s.otrs_configured:
        poller.enabled = True
        _TASKS.append(asyncio.create_task(_loop(poller, _poll_once), name="poller"))
    elif s.poller_enabled:
        log.warning("poller not started: OTRS is not configured")

    if s.outbox_enabled:
        outbox.enabled = True
        _TASKS.append(asyncio.create_task(_loop(outbox, _send_once), name="outbox"))

    # The weekly history update: Friday 04:00 Riyadh time. The loop only looks
    # at the clock; the week's row in weekly_runs keeps it to one run a week.
    if s.weekly_enabled:
        weekly.enabled = True
        _TASKS.append(asyncio.create_task(_loop(weekly, _weekly_once), name="weekly"))


async def stop() -> None:
    """Cancel the loops and wait for them. A pass in flight is allowed to be
    interrupted: a draft half-decided is simply not saved, and the next pass
    picks the ticket up again."""
    for task in _TASKS:
        task.cancel()
    if _TASKS:
        await asyncio.gather(*_TASKS, return_exceptions=True)
    _TASKS.clear()


def status() -> dict[str, Any]:
    return {name: state.as_dict() for name, state in STATE.items()}