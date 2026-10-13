"""The Settings page: settings a super admin changes from the console.

.env keeps only what must be known before the database can be read, or that
nobody should be able to change from a browser: the database, the session
secret, the super admins, the folders and ports. Everything in FIELDS below
can be set on the page instead. A value saved there is a row in app_settings
and wins over .env; "Use .env" deletes the row and the .env value (or the
default) applies again. So a deployment with nothing saved behaves exactly as
before, and existing .env files keep working.

Passwords and keys are encrypted in the database with a key derived from
SESSION_SECRET, and never leave the server: the page shows whether one is set
and lets it be replaced, not read. Changing SESSION_SECRET makes them
unreadable; the page then says so and they are entered again.

Every change is checked as a whole before anything is written -- the same
validation .env goes through -- and recorded in the audit log (for a secret:
that it changed, not to what).
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import sys
from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr, ValidationError

from support_agent.config import Settings, env_settings, refresh_settings
from support_agent.db import postgres
from support_agent.logging import get_logger

log = get_logger(__name__)


class SettingsError(ValueError):
    pass


@dataclass(frozen=True)
class Field:
    key: str
    group: str
    label: str
    help: str
    kind: str = "text"        # text | list | pairs | bool | int | float | secret
    danger: str | None = None  # asked to confirm before saving this change


FIELDS: tuple[Field, ...] = (
    # ---- the product
    Field("product_name", "Product", "Product name",
          "In the OTRS note headings, the weekly report and what the agent is told."),
    Field("otrs_intake_queue", "Product", "Intake queue",
          "The OTRS queue new tickets are picked up from."),
    Field("own_services", "Product", "This product's Services", "Comma-separated "
          "Service values that are this product's. Set when two products read the same "
          "intake queue: drafts with another Service are listed apart, with Not ours. "
          "Empty: every draft is listed together.", "list"),
    Field("history_queues", "Product", "History queues",
          "Comma-separated. Whose closed tickets the Friday run adds to the history.", "list"),
    Field("team_queues", "Product", "Teams", "Comma-separated queue=key pairs: each team's "
          "queue and the short name its work is stored under.", "pairs"),
    Field("queue_prefix", "Product", "Queue prefix",
          "Left off queue names where the console shows them short."),
    Field("subtype_prefix", "Product", "SubType prefix",
          "Left off subtype names where the console shows them short."),
    Field("fallback_service", "Product", "Fallback Service",
          "Used when the agent returns a Service that is not on the Configuration page. "
          "Must be a locked value there."),
    Field("fallback_queue", "Product", "Fallback queue",
          "The same for the queue. Must be a locked value on the Configuration page."),
    Field("fallback_sla", "Product", "Fallback SLA",
          "The same for the SLA. Must be a locked value on the Configuration page."),

    # ---- switches
    Field("dry_run", "Switches", "Dry run", "On: nothing is written to OTRS. Drafts are "
          "still made and approved; only the write is skipped.", "bool",
          danger="Turning dry run off lets approvals write into OTRS."),
    Field("poller_enabled", "Switches", "Pick up new tickets",
          "Reads the intake queue and drafts new tickets.", "bool"),
    Field("outbox_enabled", "Switches", "Write approvals to OTRS",
          "Sends approved drafts to OTRS (still nothing while dry run is on).", "bool"),
    Field("weekly_enabled", "Switches", "Friday history update",
          "Adds the week's closed tickets to the history every Friday at 04:00.", "bool"),
    Field("testing_page", "Switches", "Testing page",
          "Shows the Testing page, which creates tickets on a staging OTRS. "
          "Refuses to work against production either way.", "bool"),

    # ---- OTRS
    Field("otrs_base_url", "OTRS", "OTRS address",
          "Where live tickets are read and approvals written."),
    Field("otrs_user", "OTRS", "OTRS user", ""),
    Field("otrs_password", "OTRS", "OTRS password", "", "secret"),
    Field("otrs_webservice", "OTRS", "OTRS web service", "The GenericInterface web service."),
    Field("otrs_poll_seconds", "OTRS", "Seconds between checks",
          "How often the intake queue is checked.", "int"),
    Field("history_otrs_base_url", "OTRS", "History OTRS address",
          "Where closed tickets are read for the history. Empty: the OTRS above."),
    Field("history_otrs_user", "OTRS", "History OTRS user", ""),
    Field("history_otrs_password", "OTRS", "History OTRS password", "", "secret"),

    # ---- AI
    Field("groq_api_key", "AI models", "Groq API key", "", "secret"),
    Field("groq_model", "AI models", "Agent model", "The model that drafts each ticket."),
    Field("journey_model", "AI models", "History model",
          "Writes the journeys and team-scope evidence from closed tickets."),
    Field("journey_fallback_model", "AI models", "History fallback model",
          "Used when the history model fails."),
    Field("journey_language", "AI models", "History language",
          "The language the journeys are written in."),

    # ---- indexes
    Field("ragent_tenant", "Indexes", "Knowledge base index", "The name in Qdrant."),
    Field("precedent_tenant", "Indexes", "Similar-tickets index", "The name in Qdrant."),
    Field("junk_tenant", "Indexes", "Junk index",
          "The name in Qdrant. Products can share one."),
    Field("junk_sync", "Indexes", "Keep the junk index up to date",
          "Off where another product keeps a shared junk index; this one only reads it.",
          "bool"),

    # ---- checks
    Field("similar_min_score", "Checks", "Similar tickets: minimum score",
          "Past tickets scoring below this are not shown.", "float"),
    Field("junk_threshold", "Checks", "Junk: near-duplicate score", "", "float"),
    Field("junk_floor", "Checks", "Junk: closer-to-junk floor", "", "float"),
    Field("junk_margin", "Checks", "Junk: closer-to-junk margin", "", "float"),
    Field("junk_sender_min", "Checks", "Junk: sender rule",
          "Junk tickets a domain must have sent, with no real ones.", "int"),

    # ---- email
    Field("smtp_host", "Email", "Mail server", ""),
    Field("smtp_port", "Email", "Mail server port", "", "int"),
    Field("mail_username", "Email", "Mail user", ""),
    Field("mail_password", "Email", "Mail password", "", "secret"),
    Field("mail_from", "Email", "Sent from", ""),
    Field("console_url", "Email", "Console address",
          "The link put in urgent emails and the weekly report."),
    Field("weekly_report_recipients", "Email", "Weekly report recipients",
          "Comma-separated addresses.", "list"),
    Field("registration_allowlist", "Email", "Who may sign up",
          "Comma-separated addresses and/or domains such as @t2.sa.", "list"),
)
BY_KEY = {f.key: f for f in FIELDS}
GROUPS = tuple(dict.fromkeys(f.group for f in FIELDS))

# Built once per process from settings these depend on; rebuilt after a save
# that changes any of them.
_CACHED = (
    ("support_agent.history.journey", "_client", {"groq_api_key", "journey_timeout"}),
    ("support_agent.history.journey", "_prompt", {"journey_language"}),
    ("support_agent.knowledge.retriever", "_tenant", {"groq_api_key", "ragent_tenant"}),
    ("support_agent.history.precedent", "_tenant", {"groq_api_key", "precedent_tenant",
                                                    "junk_tenant"}),
    ("support_agent.history.precedent", "_rag", {"groq_api_key"}),
)


# ---------------------------------------------------------------------------
# secrets
# ---------------------------------------------------------------------------

def _fernet():
    from cryptography.fernet import Fernet

    secret = env_settings().session_secret
    if not secret:
        raise SettingsError("SESSION_SECRET is not set in .env; passwords cannot be "
                            "stored safely without it")
    digest = hashlib.sha256(b"support-agent settings\0" +
                            secret.get_secret_value().encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def _encrypt(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def _decrypt(token: str) -> str | None:
    """None when it cannot be read: SESSION_SECRET changed since it was saved."""
    from cryptography.fernet import InvalidToken

    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, SettingsError):
        return None


# ---------------------------------------------------------------------------
# reading
# ---------------------------------------------------------------------------

def _rows() -> dict[str, dict]:
    """The saved values. On a connection of its own with a short time limit,
    not the pool: this runs inside get_settings(), which must stay quick and
    must not open the pool (and its threads) in a process that only wanted a
    setting while the database is down."""
    import psycopg
    from psycopg.rows import dict_row

    try:
        with psycopg.connect(env_settings().db_dsn, connect_timeout=3,
                             row_factory=dict_row) as conn:
            rows = conn.execute("SELECT key, value, secret, updated_by, updated_at "
                                "FROM app_settings").fetchall()
    except Exception as exc:
        # Before migration 0021 there is no table: nothing has been saved.
        if "app_settings" in str(exc) and "does not exist" in str(exc):
            return {}
        raise
    return {r["key"]: r for r in rows if r["key"] in BY_KEY}


def _optional(key: str) -> bool:
    annotation = Settings.model_fields[key].annotation
    return type(None) in getattr(annotation, "__args__", ())


def _typed(key: str, text: str | None) -> Any:
    """A stored or typed value as the model takes it: "" means unset for a
    setting that may be unset."""
    if text is None or (text == "" and _optional(key)):
        return None
    if BY_KEY[key].kind in ("list", "pairs"):
        return ",".join(x.strip() for x in text.replace("\n", ",").split(",") if x.strip())
    return text


def _merge(base: Settings, overrides: dict[str, Any]) -> Settings:
    data = base.model_dump()
    data.update(overrides)
    return type(base).model_validate(data)


def apply(base: Settings) -> Settings:
    """`base` with the saved values on top. A stored value the model refuses
    (it was valid when saved, the rules changed since) is skipped and logged
    rather than taking every other setting down with it."""
    overrides: dict[str, Any] = {}
    for key, row in _rows().items():
        if row["secret"]:
            plain = _decrypt(row["value"])
            if plain is None:
                log.warning("saved secret cannot be read; using .env",
                            extra={"setting": key})
                continue
            overrides[key] = plain or None
        else:
            overrides[key] = _typed(key, row["value"])
    if not overrides:
        return base
    try:
        return _merge(base, overrides)
    except ValidationError as exc:
        bad = {str(e["loc"][0]) for e in exc.errors() if e.get("loc")}
        log.warning("saved settings refused; using .env for them", extra={"settings": sorted(bad)})
        return _merge(base, {k: v for k, v in overrides.items() if k not in bad})


def _display(value: Any) -> Any:
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if isinstance(value, SecretStr):
        return None
    return str(value)


def describe() -> dict:
    """Everything the Settings page shows. Never a secret's value."""
    from support_agent.config import get_settings

    base = env_settings()
    rows = _rows()
    current = get_settings()
    from_env = base.model_fields_set
    fields = []
    for f in FIELDS:
        row = rows.get(f.key)
        source = "page" if row else (".env" if f.key in from_env else "default")
        item = {"key": f.key, "group": f.group, "label": f.label, "help": f.help,
                "kind": f.kind, "danger": f.danger, "source": source,
                "env_set": f.key in from_env,
                "updated_by": row["updated_by"] if row else None,
                "updated_at": row["updated_at"].isoformat() if row else None}
        if f.kind == "secret":
            item["is_set"] = bool(getattr(current, f.key))
            item["unreadable"] = bool(row) and _decrypt(row["value"]) is None
        else:
            item["value"] = _display(getattr(current, f.key))
        fields.append(item)
    return {"groups": list(GROUPS), "fields": fields,
            "env_only": ["DATABASE_URL", "SESSION_SECRET", "SUPER_ADMIN_EMAILS", "PRODUCT",
                         "SESSION_COOKIE", "QDRANT_URL", "DOCLING_URL", "LOG_LEVEL"]}


# ---------------------------------------------------------------------------
# saving
# ---------------------------------------------------------------------------

def _check_fallbacks(merged: Settings, changed: set[str]) -> list[str]:
    from support_agent import options

    problems = []
    for key, kind in (("fallback_service", "service"), ("fallback_queue", "queue"),
                      ("fallback_sla", "sla")):
        if key not in changed:
            continue
        value = getattr(merged, key)
        try:
            locked = {o.value for o in options.described(kind) if o.locked}
        except Exception:
            continue      # no Configuration values yet: nothing to check against
        if value not in locked:
            problems.append(f"{BY_KEY[key].label}: {value!r} is not a locked {kind} value on "
                            "the Configuration page; add or lock it there first")
    return problems


def _check_ranges(merged: Settings, changed: set[str]) -> list[str]:
    problems = []
    for key in ("similar_min_score", "junk_threshold", "junk_floor", "junk_margin"):
        if key in changed and not 0 <= getattr(merged, key) <= 1:
            problems.append(f"{BY_KEY[key].label}: between 0 and 1")
    if "otrs_poll_seconds" in changed and merged.otrs_poll_seconds < 10:
        problems.append("Seconds between checks: at least 10")
    if "team_queues" in changed and merged.team_queues:
        if not merged.team_queue_map or any("=" not in p for p in merged.team_queues.split(",")):
            problems.append("Teams: write each as queue=key, separated by commas")
    return problems


def save(changes: dict[str, Any], who: str) -> dict:
    """Save what the page sent: {key: value}, or {key: None} for "use .env".

    For a secret, a value replaces it and None goes back to .env; it is never
    read back. Everything is checked together first; nothing is written if
    anything is wrong.
    """
    unknown = [k for k in changes if k not in BY_KEY]
    if unknown:
        raise SettingsError(f"not a setting the page can change: {', '.join(unknown)}")
    if not changes:
        return describe()

    from support_agent.config import get_settings

    before = get_settings()
    rows = _rows()
    # What will be in force: the saved values that stay, plus these changes.
    overrides: dict[str, Any] = {}
    for key, row in rows.items():
        if key in changes:
            continue
        overrides[key] = (_decrypt(row["value"]) if row["secret"] else _typed(key, row["value"]))
    resets, writes = set(), {}
    for key, value in changes.items():
        if value is None:
            resets.add(key)
            continue
        text = str(value).lower() if isinstance(value, bool) else str(value).strip()
        if BY_KEY[key].kind == "secret" and not text:
            resets.add(key)        # an empty password box means "nothing typed"
            continue
        writes[key] = text
        overrides[key] = _typed(key, text) if BY_KEY[key].kind != "secret" else text
    try:
        merged = _merge(env_settings(), {k: v for k, v in overrides.items() if v is not None
                                         or _optional(k)})
    except ValidationError as exc:
        raise SettingsError("; ".join(
            f"{BY_KEY.get(str(e['loc'][0]), Field(str(e['loc'][0]), '', str(e['loc'][0]), '')).label}"
            f": {e['msg']}" for e in exc.errors())) from None
    changed = set(writes) | resets
    problems = _check_fallbacks(merged, changed) + _check_ranges(merged, changed)
    if problems:
        raise SettingsError("; ".join(problems))

    with postgres.connection() as conn:
        with conn.cursor() as cur:
            for key in resets:
                cur.execute("DELETE FROM app_settings WHERE key = %s", (key,))
            for key, text in writes.items():
                secret = BY_KEY[key].kind == "secret"
                cur.execute(
                    """
                    INSERT INTO app_settings (key, value, secret, updated_by, updated_at)
                    VALUES (%s, %s, %s, %s, now())
                    ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                        secret = EXCLUDED.secret, updated_by = EXCLUDED.updated_by,
                        updated_at = now()
                    """,
                    (key, _encrypt(text) if secret else text, secret, who))

    refresh_settings()
    after = get_settings()
    record = {}
    for key in sorted(changed):
        if BY_KEY[key].kind == "secret":
            record[key] = "replaced" if key in writes else "back to .env"
        else:
            record[key] = {"from": _display(getattr(before, key)),
                           "to": _display(getattr(after, key)),
                           **({"back_to_env": True} if key in resets else {})}
    from support_agent.db import audit
    audit.record(actor="reviewer", action="settings_changed", reasoning=who,
                 evidence={"by": who, "changes": record})
    _rebuild(changed)
    log.info("settings changed", extra={"by": who, "keys": sorted(changed)})
    return describe()


def _rebuild(changed: set[str]) -> None:
    """Drop what was built from a setting that changed, so it is built again
    with the new value on next use."""
    for module, name, keys in _CACHED:
        if changed & keys and module in sys.modules:
            fn = getattr(importlib.import_module(module), name, None)
            if fn is not None and hasattr(fn, "cache_clear"):
                fn.cache_clear()
