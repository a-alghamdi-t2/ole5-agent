"""The values the agent may choose from, as the support team has set them.

These live in agent_options and are edited from the console's Configuration
page, so a new SLA or queue does not need a release. This table is the only
place they are defined.

If the table cannot be read and nothing is cached, reading raises
OptionsUnavailable rather than guessing. A decision cannot be stored without the
database anyway, so failing the ticket -- it is retried on the next poll -- is
the same outcome with a clearer reason.

Read often, written rarely. Reads are cached and re-checked against a cheap
stamp query every few seconds; a write through this module clears the cache at
once, so the console sees its own change immediately.

A value is never deleted, only disabled -- drafts, reviews and the audit log
still name it. Locked values are the ones the prompt or the validator refers to
by name; they can be described but not disabled.
"""

from __future__ import annotations

import threading
import time
from dataclasses import asdict, dataclass

from ole5.db import audit, postgres
from ole5.logging import get_logger

log = get_logger(__name__)

KINDS: tuple[str, ...] = (
    "service", "queue", "next_state", "type", "subtype", "priority", "sla",
)

# What each kind is called on screen and in the prompt.
LABELS: dict[str, str] = {
    "service": "Service",
    "queue": "Queue",
    "next_state": "Next state",
    "type": "Type",
    "subtype": "SubType",
    "priority": "Priority",
    "sla": "SLA",
}

MAX_VALUE = 200
MAX_DESCRIPTION = 500

# How long a cached read is trusted before the stamp is checked again. One
# decision reads the lists a dozen times; this keeps that to one query.
RECHECK_SECONDS = 5.0


class OptionError(ValueError):
    """A change that cannot be made. The message is shown to the reviewer."""


class OptionsUnavailable(RuntimeError):
    """The lists could not be read, or a kind has no active value."""


@dataclass(frozen=True)
class Option:
    id: int | None
    kind: str
    value: str
    description: str | None
    active: bool
    locked: bool
    position: int


# ---------------------------------------------------------------------------
# the cache
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_cache: dict[str, list[Option]] | None = None
_stamp: str | None = None
_checked_at = 0.0


def _read_stamp() -> str:
    row = postgres.query_one(
        "SELECT count(*) AS n, max(updated_at) AS t FROM agent_options"
    ) or {}
    return f"{row.get('n')}|{row.get('t')}"


def _read_all() -> dict[str, list[Option]]:
    rows = postgres.query(
        """
        SELECT id, kind, value, description, active, locked, position
        FROM agent_options
        ORDER BY kind, position, id
        """
    )
    out: dict[str, list[Option]] = {k: [] for k in KINDS}
    for r in rows:
        if r["kind"] in out:
            out[r["kind"]].append(Option(**r))
    return out


def _load() -> dict[str, list[Option]]:
    global _cache, _stamp, _checked_at
    with _lock:
        now = time.monotonic()
        if _cache is not None and now - _checked_at < RECHECK_SECONDS:
            return _cache
        try:
            stamp = _read_stamp()
            if _cache is None or stamp != _stamp:
                _cache = _read_all()
                _stamp = stamp
            _checked_at = now
        except Exception as exc:
            # Keep deciding with what we last knew. With nothing cached there
            # is nothing to decide with.
            log.exception("could not read agent_options")
            if _cache is None:
                raise OptionsUnavailable(
                    f"the option lists could not be read: {type(exc).__name__}"
                ) from exc
            _checked_at = now
        return _cache


def invalidate() -> None:
    """Forget the cache. Called after every write through this module."""
    global _cache, _stamp, _checked_at
    with _lock:
        _cache, _stamp, _checked_at = None, None, 0.0


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def _check_kind(kind: str) -> str:
    if kind not in KINDS:
        raise OptionError(f"unknown option kind {kind!r}")
    return kind


def described(kind: str) -> list[Option]:
    """The active options of one kind, in order, with their descriptions."""
    _check_kind(kind)
    found = [o for o in _load().get(kind, []) if o.active]
    if not found:
        # Cannot happen through the console, which refuses to disable the last
        # value of a kind. Means the migration has not run.
        raise OptionsUnavailable(f"no active {LABELS[kind]} options; "
                                 "has migration 0002_agent_options been applied?")
    return found


def active(kind: str) -> tuple[str, ...]:
    """The active values of one kind. What the agent and the dropdowns offer."""
    return tuple(o.value for o in described(kind))


def all_active() -> dict[str, list[str]]:
    return {kind: list(active(kind)) for kind in KINDS}


def everything() -> list[dict]:
    """Every option of every kind, disabled ones included. For the config page."""
    data = _load()
    return [asdict(o) for kind in KINDS for o in data.get(kind, [])]


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------

def _who(reviewer_id: int) -> str | None:
    """For the audit trail: the actor column says a reviewer acted, not which."""
    row = postgres.query_one(
        "SELECT display_name, email FROM reviewers WHERE id = %s", (reviewer_id,)
    )
    return (row["display_name"] or row["email"]) if row else None


def _clean_value(value: str | None) -> str:
    v = (value or "").strip()
    if not v:
        raise OptionError("the value is empty")
    if len(v) > MAX_VALUE:
        raise OptionError(f"the value is longer than {MAX_VALUE} characters")
    if "\n" in v or "\r" in v or "\t" in v:
        raise OptionError("the value must be on one line")
    return v


def _clean_description(text: str | None) -> str:
    """Required: the description is what tells the agent when to choose the
    value, and it is the only place that says so."""
    t = (text or "").strip()
    if not t:
        raise OptionError("a description is required: say when the agent should choose this")
    if len(t) > MAX_DESCRIPTION:
        raise OptionError(f"the description is longer than {MAX_DESCRIPTION} characters")
    return t


def add(kind: str, value: str, description: str | None,
        reviewer_id: int) -> dict:
    """Add a value. It is active at once and the agent can choose it on the
    next ticket."""
    _check_kind(kind)
    value = _clean_value(value)
    description = _clean_description(description)

    existing = postgres.query_one(
        "SELECT id, value, active FROM agent_options "
        "WHERE kind = %s AND lower(value) = lower(%s)",
        (kind, value),
    )
    if existing:
        if existing["active"]:
            raise OptionError(f"{existing['value']!r} is already an option")
        raise OptionError(
            f"{existing['value']!r} exists but is disabled; enable it instead"
        )

    row = postgres.query_one(
        """
        INSERT INTO agent_options (kind, value, description, position, created_by)
        VALUES (%s, %s, %s,
                (SELECT coalesce(max(position), 0) + 1
                 FROM agent_options WHERE kind = %s),
                %s)
        RETURNING id, kind, value, description, active, locked, position
        """,
        (kind, value, description, kind, reviewer_id),
    )
    invalidate()

    audit.record(actor="reviewer", action="option_added",
                 reasoning=f"{LABELS[kind]}: {value}",
                 evidence={"option": row, "reviewer": _who(reviewer_id)})
    log.info("option added", extra={"kind": kind, "value": value})
    return row


def update(option_id: int, reviewer_id: int, *,
           active_flag: bool | None = None,
           description: str | None = None,
           set_description: bool = False) -> dict:
    """Enable, disable, or describe a value. The value itself cannot be
    renamed: a rename would silently change what old drafts mean. Disable it
    and add the new one instead."""
    row = postgres.query_one(
        "SELECT id, kind, value, description, active, locked "
        "FROM agent_options WHERE id = %s",
        (option_id,),
    )
    if row is None:
        raise OptionError("no such option")

    changes: dict = {}

    if not set_description and not (row["description"] or "").strip():
        raise OptionError(f"{row['value']!r} has no description yet; add one first")

    if active_flag is not None and active_flag != row["active"]:
        if not active_flag:
            if row["locked"]:
                raise OptionError(
                    f"{row['value']!r} is used by the agent's rules and "
                    "cannot be disabled"
                )
            remaining = postgres.query_one(
                "SELECT count(*) AS n FROM agent_options "
                "WHERE kind = %s AND active AND id <> %s",
                (row["kind"], option_id),
            )
            if not remaining or not remaining["n"]:
                raise OptionError(f"that would leave no {LABELS[row['kind']]} to choose")
        changes["active"] = {"from": row["active"], "to": active_flag}

    if set_description:
        new = _clean_description(description)
        if new != row["description"]:
            changes["description"] = {"from": row["description"], "to": new}

    if not changes:
        return {**row}

    updated = postgres.query_one(
        """
        UPDATE agent_options
        SET active = %s, description = %s
        WHERE id = %s
        RETURNING id, kind, value, description, active, locked, position
        """,
        (changes.get("active", {}).get("to", row["active"]),
         changes["description"]["to"] if "description" in changes else row["description"],
         option_id),
    )
    invalidate()

    if "active" in changes:
        action = "option_enabled" if active_flag else "option_disabled"
    else:
        action = "option_described"
    audit.record(actor="reviewer", action=action,
                 reasoning=f"{LABELS[row['kind']]}: {row['value']}",
                 evidence={"option_id": option_id, "changes": changes,
                           "reviewer": _who(reviewer_id)})
    log.info("option updated", extra={"option": option_id, "action": action})
    return updated


# ---------------------------------------------------------------------------
# how to choose each field
# ---------------------------------------------------------------------------
#
# The guidance about a field rather than any one value -- "judge priority from
# the impact, not the tone" -- kept beside the values on the Configuration
# page and written into the prompt above each list. Required, like the values'
# descriptions.

MAX_GUIDANCE = 2000


def guidance() -> dict[str, str]:
    """Each field's "how to choose", by kind. Empty when the table is missing
    (before the migration) -- the prompt then lists values alone."""
    try:
        rows = postgres.query("SELECT kind, guidance FROM agent_option_kinds")
    except Exception:
        log.exception("could not read field guidance")
        return {}
    return {r["kind"]: r["guidance"] for r in rows}


def set_guidance(kind: str, text: str | None, reviewer_id: int) -> dict:
    _check_kind(kind)
    t = (text or "").strip()
    if not t:
        raise OptionError("the guidance is required: say how the agent should choose this field")
    if len(t) > MAX_GUIDANCE:
        raise OptionError(f"the guidance is longer than {MAX_GUIDANCE} characters")
    before = guidance().get(kind)
    if before == t:
        return {"kind": kind, "guidance": t}
    postgres.execute(
        """
        INSERT INTO agent_option_kinds (kind, guidance, updated_by) VALUES (%s, %s, %s)
        ON CONFLICT (kind) DO UPDATE SET guidance = EXCLUDED.guidance,
                                         updated_by = EXCLUDED.updated_by
        """,
        (kind, t, reviewer_id),
    )
    audit.record(actor="reviewer", action="option_guidance_changed",
                 reasoning=f"{LABELS[kind]}: how to choose",
                 evidence={"kind": kind, "from": before, "to": t,
                           "reviewer": _who(reviewer_id)})
    log.info("field guidance changed", extra={"kind": kind})
    return {"kind": kind, "guidance": t}
