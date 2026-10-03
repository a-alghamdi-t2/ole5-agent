"""What happened on a closed ticket from start to finish, and why it moved.

Two kinds of output, from two kinds of source:

    path     the exact queues, from ticket_timelines.queue_path -- OTRS's own
             queue notifications. Copied, never generated.
    steps, moves, summary
             written by a model after reading the stored timeline, with the
             path given to it as fact to explain, not to work out.

The model is journey_model (MiniMax M2.7), called through the same Groq client
and key as the agent. When it fails -- not served, an error, or no usable JSON
after a retry -- journey_fallback_model answers instead, and the row records
which model wrote it and why the fallback was needed.

Its own extraction, with its own prompt (prompts/extraction/journey.md).
"""

from __future__ import annotations

import json
import re
import time
from functools import lru_cache

from pydantic import BaseModel, Field

from ole5.config import get_settings
from ole5.db import postgres
from ole5.history import timeline
from ole5.logging import get_logger

log = get_logger(__name__)

# Found through PROMPTS_DIR, like the agent's own prompt: relative to the code
# it would point into the installed package inside the container, where there
# are no prompts.
def _prompt_path():
    return get_settings().prompts_dir / "extraction" / "journey.md"
PROMPT_VER = "journey-v3"

# A very long ticket keeps its beginning and its end -- the problem and the
# resolution -- and every queue arrival, and says how much was left out.
TIMELINE_CHARS = 60000
KEEP_FIRST, KEEP_LAST = 10, 20


class JourneyFailed(RuntimeError):
    pass


class Step(BaseModel):
    article: int | None = None
    queue: str | None = None
    what: str


class Move(BaseModel):
    from_: str = Field(alias="from")
    to: str
    article: int | None = None
    why: str = "not stated"

    model_config = {"populate_by_name": True}


class Journey(BaseModel):
    steps: list[Step] = Field(default_factory=list)
    moves: list[Move] = Field(default_factory=list)
    summary: str = ""


# ---------------------------------------------------------------------------
# the input
# ---------------------------------------------------------------------------

def load(closed_ticket_id: int) -> tuple[dict, dict]:
    ticket = postgres.query_one("SELECT * FROM closed_tickets WHERE id = %s",
                                (closed_ticket_id,))
    tl = postgres.query_one("SELECT entries, queue_path FROM ticket_timelines "
                            "WHERE closed_ticket_id = %s", (closed_ticket_id,))
    if ticket is None or tl is None:
        raise JourneyFailed(f"no timeline for closed ticket {closed_ticket_id}")
    return ticket, tl


def render(ticket: dict, tl: dict) -> str:
    """The stored timeline as the model reads it, trimmed if very long."""
    entries = tl["entries"] or []
    text = timeline.render(ticket, entries, tl["queue_path"] or [], names=False)
    if len(text) <= TIMELINE_CHARS or len(entries) <= KEEP_FIRST + KEEP_LAST:
        return text
    head, middle, tail = entries[:KEEP_FIRST], entries[KEEP_FIRST:-KEEP_LAST], entries[-KEEP_LAST:]
    moves = [e for e in middle if e.get("kind") == "queue"]
    gap = {"no": None, "at": "", "kind": "note", "from": None, "subject": "",
           "text": f"… {len(middle) - len(moves)} messages in the middle left out …"}
    return timeline.render(ticket, head + [gap] + moves + tail, tl["queue_path"] or [],
                           names=False)


# ---------------------------------------------------------------------------
# the model
# ---------------------------------------------------------------------------

@lru_cache(maxsize=1)
def _prompt() -> str:
    return _prompt_path().read_text(encoding="utf-8").replace(
        "{language}", get_settings().journey_language)


@lru_cache(maxsize=1)
def _client():
    from groq import Groq

    s = get_settings()
    if not s.groq_api_key:
        raise JourneyFailed("GROQ_API_KEY is not set")
    return Groq(api_key=s.groq_api_key.get_secret_value(), timeout=s.journey_timeout)


_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)


def _json_of(text: str) -> dict:
    """The JSON object in a reply. A reasoning model may put its thinking in
    <think> tags or wrap the object in a code fence."""
    text = _THINK.sub("", text or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no JSON object in the reply")
    return json.loads(text[start:end + 1])


def ask(model: str, messages: list[dict]) -> str:
    reply = _client().chat.completions.create(
        model=model, messages=messages, temperature=0.2, max_tokens=8192)
    return reply.choices[0].message.content or ""


# A rate limit is not a failure of the model: Groq is asking us to slow down,
# and usually says for how long. Waiting keeps the journey with the main model
# instead of handing it to the fallback.
_RATE = re.compile(r"rate.?limit|429|too many requests", re.I)
_WAIT = re.compile(r"try again in\s*(?:(\d+)m)?\s*([\d.]+)\s*(ms|s)", re.I)
RATE_RETRIES = 8
RATE_DEFAULT_WAIT = 20.0


def _ask_patiently(model: str, messages: list[dict]) -> str:
    for attempt in range(RATE_RETRIES + 1):
        try:
            return ask(model, messages)
        except Exception as exc:
            text = str(exc)
            if not _RATE.search(text) or attempt == RATE_RETRIES:
                raise
            m = _WAIT.search(text)
            if m:
                wait = (int(m.group(1) or 0) * 60 + float(m.group(2))
                        / (1000 if m.group(3).lower() == "ms" else 1))
            else:
                wait = RATE_DEFAULT_WAIT
            wait = min(max(wait + 1, 2), 120)
            log.warning("rate limited, waiting", extra={"model": model, "seconds": round(wait)})
            print(f"      rate limited on {model.split('/')[-1]}, waiting {wait:.0f}s", flush=True)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def _with(model: str, text: str) -> Journey:
    """One model, two tries. Raises when it cannot produce a usable journey."""
    messages = [{"role": "system", "content": _prompt()},
                {"role": "user", "content": text}]
    last = None
    for attempt in (1, 2):
        try:
            journey = Journey.model_validate(_json_of(_ask_patiently(model, messages)))
            if not journey.steps or not journey.summary.strip():
                raise ValueError("steps and summary must not be empty")
            return journey
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            log.warning("journey attempt failed",
                        extra={"model": model, "attempt": attempt, "error": last[:200]})
            # An API error (model not served, rate limit) will not improve by
            # asking again with a note about JSON; go to the fallback.
            if "json" not in last.lower() and "must not be empty" not in last \
                    and "validation" not in last.lower():
                break
            messages.append({"role": "user", "content":
                             f"That was not usable ({last[:200]}). "
                             "Reply with the JSON object only."})
    raise JourneyFailed(last or "no usable reply")


def _arrivals(tl: dict) -> list[tuple[int, str]]:
    """(message number, queue) for each arrival, in order, as recorded."""
    out = []
    for e in tl["entries"] or []:
        if e.get("kind") == "queue" and str(e.get("no") or "").isdigit():
            out.append((int(e["no"]), e["queue"]))
    return out


def _queues_from_timeline(journey: Journey, tl: dict) -> Journey:
    """The queue each step happened in, and the message each move happened at,
    looked up from the recorded arrivals instead of taken from the model. The
    model guesses these -- a queue the ticket never visited, or none at all --
    while the timeline has them exactly. Before the first recorded arrival the
    queue is unknown and stays empty."""
    arrivals = _arrivals(tl)
    for step in journey.steps:
        if step.article is None:
            continue
        before = [q for no, q in arrivals if no < step.article]
        step.queue = before[-1] if before else None
    # Collapsed the same way the path is, so the n-th move is the n-th change.
    changes, last = [], None
    for no, q in arrivals:
        if q != last:
            changes.append(no)
            last = q
    for move, no in zip(journey.moves, changes[1:]):
        move.article = no
    return journey


def _in_order(journey: Journey) -> Journey:
    """Steps in message order. A step without a message number stays after
    the step before it."""
    keyed, last = [], 0
    for i, step in enumerate(journey.steps):
        last = step.article if step.article is not None else last
        keyed.append((last, i, step))
    journey.steps = [s for _, _, s in sorted(keyed)]
    return journey


def _align(journey: Journey, route: list[str]) -> Journey:
    """Hold the moves to the recorded path: one per step of the route, in
    order, with its queues exactly as recorded. A reason the model gave is
    kept when its queues match; anything else becomes "not stated"."""
    given = {(m.from_, m.to): m for m in journey.moves}
    journey.moves = [
        Move(**{"from": frm, "to": to,
                "article": given[(frm, to)].article if (frm, to) in given else None,
                "why": (given[(frm, to)].why.strip() or "not stated")
                       if (frm, to) in given else "not stated"})
        for frm, to in zip(route, route[1:])
    ]
    return journey


def extract(ticket: dict, tl: dict) -> tuple[Journey, dict]:
    """One ticket's journey, from the main model or, failing that, the fallback."""
    s = get_settings()
    text = render(ticket, tl)
    started = time.perf_counter()
    fallback_reason = None
    try:
        journey, model = _with(s.journey_model, text), s.journey_model
    except JourneyFailed as exc:
        fallback_reason = str(exc)[:500]
        if not s.journey_fallback_model or s.journey_fallback_model == s.journey_model:
            raise
        journey, model = _with(s.journey_fallback_model, text), s.journey_fallback_model
    meta = {"model": model, "fallback_reason": fallback_reason, "prompt_ver": PROMPT_VER,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "input_chars": len(text)}
    journey = _align(_in_order(journey), tl["queue_path"] or [])
    return _queues_from_timeline(journey, tl), meta


def save(closed_ticket_id: int, route: list[str], journey: Journey, meta: dict) -> int:
    row = postgres.query_one(
        """
        INSERT INTO ticket_journeys (closed_ticket_id, path, steps, moves, summary, model,
                                     fallback_reason, prompt_ver, input_chars, latency_ms)
        VALUES (%s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (closed_ticket_id) DO UPDATE SET
            path = EXCLUDED.path, steps = EXCLUDED.steps, moves = EXCLUDED.moves,
            summary = EXCLUDED.summary, model = EXCLUDED.model,
            fallback_reason = EXCLUDED.fallback_reason, prompt_ver = EXCLUDED.prompt_ver,
            input_chars = EXCLUDED.input_chars, latency_ms = EXCLUDED.latency_ms,
            created_at = now()
        RETURNING id
        """,
        (closed_ticket_id, route,
         json.dumps([x.model_dump() for x in journey.steps], ensure_ascii=False),
         json.dumps([m.model_dump(by_alias=True) for m in journey.moves], ensure_ascii=False),
         journey.summary, meta["model"], meta["fallback_reason"], meta["prompt_ver"],
         meta["input_chars"], meta["latency_ms"]),
    )
    return row["id"]
