"""Who is reviewing.

Sessions live in the database rather than in a signed cookie. A cookie would be
one fewer table, but a review carries a person's name against a decision that
reaches a government customer, and being able to end a session -- because
someone left, or a laptop went missing -- is worth the table.

The cookie holds a random token and nothing else. Nothing is trusted from the
browser except that string.
"""

from __future__ import annotations

import secrets
import bcrypt
from datetime import UTC, datetime, timedelta

from fastapi import Cookie, HTTPException

from ole5.config import get_settings
from ole5.db import postgres
from ole5.logging import get_logger

log = get_logger(__name__)

COOKIE = "ole5_session"

class AuthError(RuntimeError):
    pass


def hash_password(plain: str) -> str:
    raw = plain.encode("utf-8")[:72]
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode("ascii")


def verify_password(plain: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(plain.encode("utf-8")[:72], hashed.encode("ascii"))
    except Exception:
        return False


def may_register(email: str) -> bool:
    """Whether this address may create an account.

    REGISTRATION_ALLOWLIST in .env: exact addresses and/or whole domains
    ("@t2.sa"), separated by commas. Without it, only the very first account
    can be created -- for setting the console up -- and sign-up is closed
    after that. A reviewer approves what is written to OTRS, so an account is
    not something anyone who finds the address should be able to make.
    """
    from ole5.config import get_settings

    email = email.strip().lower()
    listed = get_settings().registration_allowlist
    if not listed:
        return not anyone_registered()
    for entry in (x.strip().lower() for x in listed.split(",") if x.strip()):
        if entry.startswith("@") and email.endswith(entry):
            return True
        if email == entry:
            return True
    return False


def register(email: str, password: str, display_name: str | None = None) -> int:
    """Create a reviewer. Raises AuthError if the address is taken or not
    allowed to register."""
    email = email.strip().lower()
    if not may_register(email):
        raise AuthError("this address cannot create an account here; ask an administrator")
    if len(password) < 8:
        raise AuthError("the password must be at least 8 characters")

    existing = postgres.query_one("SELECT id FROM reviewers WHERE email = %s",
                                  (email,))
    if existing:
        raise AuthError("that email is already registered")

    row = postgres.query_one(
        """
        INSERT INTO reviewers (email, display_name, password_hash)
        VALUES (%s, %s, %s) RETURNING id
        """,
        (email, display_name or email.split("@")[0], hash_password(password)),
    )
    log.info("reviewer registered", extra={"email": email})
    return row["id"]


def login(email: str, password: str) -> str:
    """Return a session token. Raises AuthError on bad credentials.

    The same message for an unknown address and a wrong password, so the form
    cannot be used to find out who has an account.
    """
    email = email.strip().lower()
    row = postgres.query_one(
        "SELECT id, password_hash, is_active FROM reviewers WHERE email = %s",
        (email,),
    )
    if row is None or not verify_password(password, row["password_hash"]):
        raise AuthError("wrong email or password")
    if not row["is_active"]:
        raise AuthError("that account is not active")

    token = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(hours=get_settings().session_hours)
    postgres.execute(
        "INSERT INTO sessions (token, reviewer_id, expires_at) VALUES (%s, %s, %s)",
        (token, row["id"], expires),
    )
    log.info("logged in", extra={"reviewer": row["id"]})
    return token


def set_password(reviewer_id: int, new: str, keep_token: str | None = None) -> None:
    """Replace a reviewer's password, and sign them out everywhere except the
    session given -- a changed password should end any other sign-in."""
    if len(new or "") < 8:
        raise AuthError("the password must be at least 8 characters")
    postgres.execute("UPDATE reviewers SET password_hash = %s WHERE id = %s",
                     (hash_password(new), reviewer_id))
    postgres.execute("DELETE FROM sessions WHERE reviewer_id = %s AND token IS DISTINCT FROM %s",
                     (reviewer_id, keep_token))


def change_password(reviewer_id: int, current: str, new: str, keep_token: str | None) -> None:
    """A signed-in reviewer changing their own password: the current one first."""
    row = postgres.query_one("SELECT password_hash FROM reviewers WHERE id = %s", (reviewer_id,))
    if row is None or not verify_password(current or "", row["password_hash"]):
        raise AuthError("the current password is not right")
    if current == new:
        raise AuthError("the new password is the same as the current one")
    set_password(reviewer_id, new, keep_token)


def logout(token: str) -> None:
    postgres.execute("DELETE FROM sessions WHERE token = %s", (token,))


def reviewer_for(token: str | None) -> dict | None:
    """The reviewer behind a token, or None. Expired sessions are removed."""
    if not token:
        return None
    row = postgres.query_one(
        """
        SELECT r.id, r.email, r.display_name, s.expires_at
        FROM sessions s JOIN reviewers r ON r.id = s.reviewer_id
        WHERE s.token = %s AND r.is_active
        """,
        (token,),
    )
    if row is None:
        return None
    if row["expires_at"] < datetime.now(UTC):
        postgres.execute("DELETE FROM sessions WHERE token = %s", (token,))
        return None

    postgres.execute("UPDATE sessions SET last_seen = now() WHERE token = %s",
                     (token,))
    return {"id": row["id"], "email": row["email"],
            "display_name": row["display_name"]}


def require(ole5_session: str | None = Cookie(default=None, alias=COOKIE)) -> dict:
    """FastAPI dependency. 401 when there is nobody behind the request."""
    reviewer = reviewer_for(ole5_session)
    if reviewer is None:
        raise HTTPException(status_code=401, detail="not signed in")
    return reviewer


def anyone_registered() -> bool:
    """False before the first sign-up, which is what opens the sign-up form."""
    row = postgres.query_one("SELECT count(*) AS n FROM reviewers")
    return bool(row and row["n"])