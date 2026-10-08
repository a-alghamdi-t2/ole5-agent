"""Configuration. Every environment variable the system reads, in one place.

Nothing else in the codebase calls os.getenv. Values are loaded from the .env
file at the repository root, or from real environment variables in deployment,
which take precedence.

Two kinds of field below. The first block mirrors .env one for one. The second
holds values code reads but nobody sets per environment; adding the variable to
.env overrides any of them.

This module does not talk to the network. It only describes how to.
"""

from __future__ import annotations

from datetime import date

from enum import Enum
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# .../ole5-agent-v2/src/ole5/config.py -> .../ole5-agent-v2
REPO_ROOT = Path(__file__).resolve().parents[2]


# The values the agent may choose from -- services, queues, states, types,
# subtypes, priorities, SLAs -- are not configuration in this sense. They live
# in the agent_options table and are edited from the console; see ole5.options.


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=REPO_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ================= set in .env =================

    # ---- database ------------------------------------------------------
    database_url: SecretStr
    """Pooled endpoint (host contains -pooler.). Used by the application."""

    database_url_direct: SecretStr | None = None
    """Direct endpoint. Migrations need a real session for the advisory lock;
    the transaction pooler cannot hold one."""

    # ---- groq ----------------------------------------------------------
    groq_api_key: SecretStr | None = None
    groq_model: str = "openai/gpt-oss-120b"

    # The journey extraction over closed tickets (ole5.history.journey). Called
    # through the same Groq client and key as groq_model. The fallback answers
    # when the main model fails or is not served; each journey records which
    # model wrote it.
    # The junk check (ole5.history.junk). junk_threshold is the similarity a
    # ticket must reach against the junk index; set it from
    # `python scripts/junk.py calibrate`, never by guess -- a false positive
    # marks a real customer's ticket as spam.
    # Mail, for the urgent emails. Same settings as the earlier project's
    # sender. Sending follows DRY_RUN: with it on, emails are composed and
    # recorded but not sent. With no SMTP_HOST, nothing is sent either.
    smtp_host: str | None = None
    smtp_port: int = 587
    mail_from: str | None = None
    mail_username: str | None = None
    mail_password: SecretStr | None = None
    console_url: str | None = None
    """Where the console is reached, e.g. http://ole5-server:8420. When set,
    urgent emails link straight to the draft."""

    registration_allowlist: str | None = None
    """Who may create a reviewer account: addresses and/or domains ("@t2.sa"),
    comma-separated. Unset: only the first account, then sign-up is closed."""

    testing_page: bool = False  # testing page
    """Shows the Testing page, which creates tickets in the intake queue.
    Staging only: it refuses to write to production OTRS whatever this says."""

    # The weekly history update (ole5.history.weekly): Friday 04:00, Riyadh.
    weekly_enabled: bool = True
    weekly_report_recipients: str | None = None
    """Comma-separated addresses for the weekly report. Empty: the run still
    happens and the report is kept in weekly_runs, but nobody is emailed."""
    # Where the history is read from, when it is not the OTRS the agent works
    # on -- staging drafting, production history. Unset: the same OTRS.
    history_otrs_base_url: str | None = None
    history_otrs_user: str | None = None
    history_otrs_password: SecretStr | None = None

    history_candidate_pool: int = 10
    """Candidates the reranker scores in the similar-tickets and junk searches.
    The reranker runs on CPU at about a second per candidate, so this is most
    of a search's time -- but drafts are made in the background, and a wider
    pool means the closest ticket is more likely among the candidates at all.
    The junk calibration was measured with 10; keep them the same. The
    knowledge base has its own, kb_candidate_pool.""" 

    junk_threshold: float = 0.90
    junk_floor: float = 0.30
    junk_margin: float = 0.25
    """A ticket whose best junk match reaches junk_floor and beats its best
    real match by junk_margin is flagged too: reworded spam of a known kind.
    Both set from `python scripts/junk.py calibrate`."""
    junk_sender_min: int = 5
    """Junk tickets a domain must have sent, with no real ones, before the
    sender alone counts as a signal."""

    journey_model: str = "minimaxai/minimax-m2.7"
    journey_fallback_model: str = "openai/gpt-oss-120b"
    journey_timeout: float = 300.0
    journey_language: str = "English"
    """The language the journey's steps and summary are written in. They are
    read by reviewers and by the agent, never by a customer."""

    # ---- otrs ----------------------------------------------------------
    # Unset means the client is unconfigured and tickets arrive through the
    # entry point instead.
    otrs_base_url: str | None = None
    otrs_user: str | None = None
    otrs_password: SecretStr | None = None
    otrs_intake_queue: str = "Support"
    otrs_webservice: str = "TicketAPI"

    # ---- ragent2 -------------------------------------------------------
    ragent_tenant: str = "ole5"
    qdrant_url: str | None = None
    docling_url: str | None = None
    upload_dir: Path = REPO_ROOT / "data" / "uploads"
    max_upload_mb: int = 25

    kb_candidate_pool: int = 10
    """How many candidates the reranker scores.
    """

    kb_top_k: int = 5

    kb_tool_repair_attempts: int = 1
    """How many times RAGent2 retries a malformed tool call from the model."""

    kb_max_rounds: int = 1

    similar_limit: int = 5
    similar_min_score: float = 0.10
    """The lowest search score a past ticket needs to count as similar. Below
    it, the matches are what the search returns when nothing is really alike
    -- around 0.01 -- while related tickets have scored 0.15 and up. Only
    tickets at or above it are stored with a draft; the rest are discarded."""
    """How many similar past tickets are stored with each draft."""

    kb_embed_timeout: float = 180.0
    """Seconds to wait for the embedding service (ds.t2.sa) on one request.

    RAGent2's own default is 60, which a long document's batch of chunks can
    exceed: indexing then fails with a ReadTimeout. Searches embed one short
    question and finish in well under that, so raising it only matters when
    the service is slow -- and when it is down, a search now waits this long
    before the retriever records the failure and the ticket is routed.
    """

    # ---- web -----------------------------------------------------------
    session_secret: SecretStr | None = None

    # ---- behaviour -----------------------------------------------------
    dry_run: bool = True
    """When true, nothing is written to OTRS.

    The draft is still built, still stored, still reviewable, and the outbox row
    is still created. Only the call that posts the note and moves the ticket is
    skipped, so a dry run exercises everything except the one irreversible step.

    Defaults to TRUE deliberately: a missing or misspelled variable must not be
    able to write into their production ticket system. Turning it off is an
    explicit act.
    """

    log_level: str = "INFO"

    # ============ defaults; add to .env only to override ============

    env: Literal["dev", "staging", "prod"] = "dev"
    log_format: Literal["console", "json"] = "console"

    db_connect_timeout: int = 15
    db_pool_max: int = 5

    otrs_timeout: int = 30
    otrs_poll_seconds: int = 60

    session_hours: int = 12

    timezone: str = "Asia/Riyadh"
    """The timezone OTRS reports in.

    TicketGet returns naive strings like "2026-08-23 16:30:00" with no offset.
    Our columns are timestamptz, so intake attaches this zone when parsing them.
    Their own auto-reply confirms it: the creation time is printed followed by
    (Asia/Riyadh).
    """

    # ---- the server process --------------------------------------------
    host: str = "0.0.0.0"
    port: int = 8420

    poller_enabled: bool = True
    """Whether the server polls the intake queue itself. Off means tickets
    arrive through /ingest only."""

    outbox_enabled: bool = True
    outbox_seconds: int = 30
    """How often approved drafts are retried. Approving already kicks a send;
    this is what recovers the ones where OTRS was down at the time."""

    migrate_on_start: bool = True
    migrations_dir: Path = REPO_ROOT / "migrations"

    prompts_dir: Path = REPO_ROOT / "prompts"

    # ---- validation ----------------------------------------------------
    @field_validator("*", mode="before")
    @classmethod
    def _blank_is_unset(cls, v: object) -> object:
        # OTRS_PASSWORD= in .env arrives as "", which would otherwise satisfy a
        # required field and produce a config that looks valid and is not.
        # An empty variable means unset, everywhere, without exception.
        if isinstance(v, str) and not v.strip():
            return None
        return v

    @field_validator("otrs_base_url")
    @classmethod
    def _no_trailing_slash(cls, v: str | None) -> str | None:
        return v.rstrip("/") if v else v

    # ---- accessors -----------------------------------------------------
    @property
    def db_dsn(self) -> str:
        return self.database_url.get_secret_value()

    @property
    def db_dsn_direct(self) -> str:
        """Falls back to the pooled DSN so nothing breaks before it is set."""
        if self.database_url_direct is None:
            return self.db_dsn
        return self.database_url_direct.get_secret_value()

    @property
    def db_dsn_yoyo(self) -> str:
        """The direct DSN, scheme-annotated for yoyo.

        yoyo maps postgresql:// to psycopg2, which we do not install. Only yoyo
        needs the annotation, so only yoyo sees it.
        """
        dsn = self.db_dsn_direct
        for prefix in ("postgresql://", "postgres://"):
            if dsn.startswith(prefix):
                return "postgresql+psycopg://" + dsn[len(prefix):]
        return dsn

    @property
    def otrs_configured(self) -> bool:
        """False until we have access. Until then, tickets arrive by POST."""
        return bool(self.otrs_base_url and self.otrs_user and self.otrs_password)

    def redacted(self) -> dict:
        """The config as something safe to print. Secrets become a length."""
        out: dict = {}
        for name in type(self).model_fields:
            value = getattr(self, name)
            if isinstance(value, SecretStr):
                raw = value.get_secret_value()
                out[name] = f"<set, {len(raw)} chars>" if raw else None
            elif isinstance(value, Enum):
                out[name] = value.value
            elif isinstance(value, Path):
                out[name] = str(value)
            else:
                out[name] = value
        return out

    def warnings(self) -> list[str]:
        """Legal, but usually a mistake. Printed by the health check."""
        w: list[str] = []
        dsn = self.db_dsn
        if "neon.tech" in dsn:
            if "sslmode=" not in dsn:
                w.append("DATABASE_URL has no sslmode; Neon expects sslmode=require")
            if "-pooler." not in dsn:
                w.append(
                    "DATABASE_URL is the direct Neon endpoint, not the pooler "
                    "(-pooler.). The application should use the pooled one."
                )
        if self.database_url_direct and "-pooler." in self.db_dsn_direct:
            w.append("DATABASE_URL_DIRECT points at the pooler; migrations want the direct endpoint")
        if not self.dry_run:
            w.append("DRY_RUN=false: this process CAN write into OTRS")
        if not self.otrs_configured:
            w.append("OTRS not configured; tickets arrive through the entry point only")
        if not self.session_secret:
            w.append("SESSION_SECRET is unset; review sessions cannot be signed")
        return w


class ConfigError(RuntimeError):
    pass


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load once, reuse. Raises ConfigError with a readable list of problems."""
    try:
        return Settings()  # type: ignore[call-arg]
    except ValidationError as exc:
        problems = "\n".join(
            f"  - {'.'.join(str(p) for p in e['loc']).upper()}: {e['msg']}"
            for e in exc.errors()
        )
        raise ConfigError(
            f"Configuration is incomplete.\n{problems}\n\n"
            f"Expected a .env file at: {REPO_ROOT / '.env'}"
        ) from None
