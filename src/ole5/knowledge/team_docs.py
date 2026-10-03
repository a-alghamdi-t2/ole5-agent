"""The two things the team keeps beside the knowledge base documents.

Team scopes: one Markdown document, read whole by the agent before it chooses a
queue. Not indexed and not split: its value is in the lines between teams,
which only make sense read together. Every upload is a new version; the newest
is in use.

Urgent keywords: words and phrases that mark a ticket urgent when they appear
in the customer's message. The matching itself comes with the urgent check;
this is the list and its upkeep. Removing a keyword switches it off rather
than deleting it, so the audit trail can still name it.

Every change is written to the audit log with who made it.
"""

from __future__ import annotations

from ole5.db import audit, postgres
from ole5.logging import get_logger

log = get_logger(__name__)

SCOPES_MAX_BYTES = 200 * 1024        # the current document is under 10 KB
SCOPES_SUFFIXES = (".md", ".txt")
KEYWORD_MAX = 100


class KnowledgeError(ValueError):
    """A change that cannot be made. The message is shown to the person."""


def _who(reviewer_id: int | None) -> str | None:
    if reviewer_id is None:
        return None
    row = postgres.query_one("SELECT display_name, email FROM reviewers WHERE id = %s",
                             (reviewer_id,))
    return (row["display_name"] or row["email"]) if row else None


# ---------------------------------------------------------------------------
# team scopes
# ---------------------------------------------------------------------------

def current_scopes() -> dict | None:
    """The document in use, with who uploaded it and how many versions exist."""
    row = postgres.query_one(
        """
        SELECT s.id, s.content, s.filename, s.created_at,
               coalesce(r.display_name, r.email) AS uploaded_by,
               (SELECT count(*) FROM team_scopes) AS versions
        FROM team_scopes s LEFT JOIN reviewers r ON r.id = s.uploaded_by
        ORDER BY s.id DESC LIMIT 1
        """
    )
    return row


def scopes_text() -> str | None:
    """Just the document, for the agent. None when nothing has been uploaded."""
    row = postgres.query_one("SELECT content FROM team_scopes ORDER BY id DESC LIMIT 1")
    return row["content"] if row else None


def upload_scopes(raw: bytes, filename: str, reviewer_id: int) -> dict:
    """Store an uploaded file as the new version in use."""
    name = (filename or "").strip() or "team_scopes.md"
    if not name.lower().endswith(SCOPES_SUFFIXES):
        raise KnowledgeError("the team scopes must be a Markdown (.md) or text (.txt) file")
    if len(raw) > SCOPES_MAX_BYTES:
        raise KnowledgeError(f"larger than {SCOPES_MAX_BYTES // 1024} KB -- "
                             "this is meant to be a short document the agent reads whole")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise KnowledgeError("the file is not UTF-8 text; save it as UTF-8 and upload again")
    text = text.replace("\r\n", "\n").strip()
    if not text:
        raise KnowledgeError("the file is empty")

    previous = scopes_text()
    if previous is not None and previous.strip() == text:
        raise KnowledgeError("this is the same as the version already in use")

    row = postgres.query_one(
        "INSERT INTO team_scopes (content, filename, uploaded_by) VALUES (%s, %s, %s) "
        "RETURNING id, created_at",
        (text, name, reviewer_id),
    )
    audit.record(actor="reviewer", action="team_scopes_uploaded",
                 reasoning=f"{name}, {len(text)} characters",
                 evidence={"version": row["id"], "filename": name,
                           "previous_characters": len(previous) if previous else 0,
                           "characters": len(text), "reviewer": _who(reviewer_id)})
    log.info("team scopes uploaded", extra={"version": row["id"], "chars": len(text)})
    return current_scopes()


# ---------------------------------------------------------------------------
# urgent keywords
# ---------------------------------------------------------------------------

def keywords() -> list[dict]:
    """The keywords in use, newest first."""
    return postgres.query(
        """
        SELECT k.id, k.keyword, k.created_at, coalesce(r.display_name, r.email) AS added_by
        FROM urgent_keywords k LEFT JOIN reviewers r ON r.id = k.added_by
        WHERE k.active ORDER BY k.created_at DESC
        """
    )


def add_keyword(keyword: str, reviewer_id: int) -> dict:
    word = " ".join((keyword or "").split())
    if not word:
        raise KnowledgeError("the keyword is empty")
    if len(word) > KEYWORD_MAX:
        raise KnowledgeError(f"longer than {KEYWORD_MAX} characters")

    existing = postgres.query_one(
        "SELECT id, keyword, active FROM urgent_keywords WHERE lower(btrim(keyword)) = lower(%s)",
        (word,))
    if existing and existing["active"]:
        raise KnowledgeError(f"{existing['keyword']!r} is already a keyword")
    if existing:
        # Removed before: switch it back on rather than add a second row.
        postgres.execute("UPDATE urgent_keywords SET active = true, added_by = %s WHERE id = %s",
                         (reviewer_id, existing["id"]))
        kid = existing["id"]
    else:
        kid = postgres.query_one(
            "INSERT INTO urgent_keywords (keyword, added_by) VALUES (%s, %s) RETURNING id",
            (word, reviewer_id))["id"]
    audit.record(actor="reviewer", action="urgent_keyword_added", reasoning=word,
                 evidence={"keyword_id": kid, "reviewer": _who(reviewer_id)})
    return {"id": kid, "keyword": word}


def remove_keyword(keyword_id: int, reviewer_id: int) -> None:
    row = postgres.query_one("SELECT keyword, active FROM urgent_keywords WHERE id = %s",
                             (keyword_id,))
    if row is None or not row["active"]:
        raise KnowledgeError("no such keyword")
    postgres.execute("UPDATE urgent_keywords SET active = false WHERE id = %s", (keyword_id,))
    audit.record(actor="reviewer", action="urgent_keyword_removed", reasoning=row["keyword"],
                 evidence={"keyword_id": keyword_id, "reviewer": _who(reviewer_id)})
