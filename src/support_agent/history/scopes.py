"""What each team did on a closed ticket: evidence for the team scopes.

For every closed ticket, the model reads its stored timeline and writes, for
each of the product's teams (TEAM_QUEUES), the kind of work the team did (did) and what
it handed on to another team and why (passed_on). Collected across all
tickets, that is the raw material for a description of each team's
responsibilities -- which a person writes from it, and the team edits.

Which teams took part is not the model's call. It comes from the records: the
queue path OTRS logged, and the queue the ticket was closed in. The model is
told, and anything it writes for a team not on that list is discarded here.
Queues that are not a team's are not counted.

Same machinery as the journeys (support_agent.history.journey): the stored timeline,
the main model with its fallback, and waiting out rate limits.
"""

from __future__ import annotations

import time
from functools import lru_cache

from pydantic import BaseModel

from support_agent.config import get_settings
from support_agent.db import postgres
from support_agent.history import journey
from support_agent.logging import get_logger

log = get_logger(__name__)

# Found through PROMPTS_DIR, like the agent's own prompt: relative to the code
# it would point into the installed package inside the container, where there
# are no prompts.
def _prompt_path():
    return get_settings().prompts_dir / "extraction" / "scopes.md"
PROMPT_VER = "scopes-v1"

def team_map() -> dict[str, str]:
    """The teams, by the queue that is theirs (TEAM_QUEUES). Nothing else counts."""
    return get_settings().team_queue_map


def team_keys() -> tuple[str, ...]:
    return tuple(dict.fromkeys(team_map().values()))


class TeamWork(BaseModel):
    did: str | None = None
    passed_on: str | None = None


def _scopes(data: object) -> dict[str, TeamWork]:
    """The model's reply, one entry per team key. Keys for anything else are
    ignored; a team it left out or set to null has no work."""
    if not isinstance(data, dict):
        raise ValueError(f"validation error: expected a JSON object, got {type(data).__name__}")
    out: dict[str, TeamWork] = {}
    for key in team_keys():
        value = data.get(key)
        out[key] = TeamWork() if value is None else TeamWork.model_validate(value)
    return out


class ScopesFailed(RuntimeError):
    pass


def involved(ticket: dict, tl: dict) -> list[str]:
    """The teams the records show took part, in the order they appear."""
    seen: list[str] = []
    for q in list(tl.get("queue_path") or []) + [ticket.get("queue") or ""]:
        team = team_map().get(q)
        if team and team not in seen:
            seen.append(team)
    return seen


@lru_cache(maxsize=1)
def _prompt() -> str:
    return _prompt_path().read_text(encoding="utf-8")


def _text(ticket: dict, tl: dict, teams: list[str]) -> str:
    names = ", ".join(teams) if teams else f"none of the {len(team_keys())}"
    return f"Teams involved (from the records): {names}\n\n" + journey.render(ticket, tl)


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return None if not value or value.lower() in ("null", "none", "n/a", "-") else value


def _with(model: str, text: str) -> dict[str, TeamWork]:
    messages = [{"role": "system", "content": _prompt()},
                {"role": "user", "content": text}]
    last = None
    for attempt in (1, 2):
        try:
            return _scopes(journey._json_of(journey._ask_patiently(model, messages)))
        except Exception as exc:
            last = f"{type(exc).__name__}: {exc}"
            log.warning("scopes attempt failed",
                        extra={"model": model, "attempt": attempt, "error": last[:200]})
            if "json" not in last.lower() and "validation" not in last.lower():
                break
            messages.append({"role": "user", "content":
                             f"That was not usable ({last[:200]}). Reply with the JSON object only."})
    raise ScopesFailed(last or "no usable reply")


def extract(ticket: dict, tl: dict) -> tuple[dict[str, TeamWork], list[str], dict]:
    """Each team's work on one ticket, held to the teams the records show."""
    s = get_settings()
    teams = involved(ticket, tl)
    text = _text(ticket, tl, teams)
    started = time.perf_counter()
    fallback_reason = None
    try:
        result, model = _with(s.journey_model, text), s.journey_model
    except ScopesFailed as exc:
        fallback_reason = str(exc)[:500]
        if not s.journey_fallback_model or s.journey_fallback_model == s.journey_model:
            raise
        result, model = _with(s.journey_fallback_model, text), s.journey_fallback_model

    work: dict[str, TeamWork] = {}
    for key in team_keys():
        w = result.get(key) or TeamWork()
        if key in teams:
            work[key] = TeamWork(did=_clean(w.did), passed_on=_clean(w.passed_on))
        else:
            # Written for a team the records do not show: not evidence.
            if _clean(w.did) or _clean(w.passed_on):
                log.info("scope dropped for uninvolved team",
                         extra={"team": key, "ticket": ticket.get("ticket_number")})
            work[key] = TeamWork()
    meta = {"model": model, "fallback_reason": fallback_reason, "prompt_ver": PROMPT_VER,
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "input_chars": len(text)}
    return work, teams, meta


def save(closed_ticket_id: int, work: dict[str, TeamWork], teams: list[str], meta: dict) -> None:
    rows = [(closed_ticket_id, key, key in teams, work[key].did, work[key].passed_on,
             meta["model"], meta["fallback_reason"], meta["prompt_ver"]) for key in team_keys()]
    with postgres.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                """
                INSERT INTO ticket_scopes (closed_ticket_id, team, involved, did, passed_on,
                                           model, fallback_reason, prompt_ver)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (closed_ticket_id, team) DO UPDATE SET
                    involved = EXCLUDED.involved, did = EXCLUDED.did,
                    passed_on = EXCLUDED.passed_on, model = EXCLUDED.model,
                    fallback_reason = EXCLUDED.fallback_reason,
                    prompt_ver = EXCLUDED.prompt_ver, created_at = now()
                """,
                rows,
            )
