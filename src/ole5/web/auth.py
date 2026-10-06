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


# ---------------------------------------------------------------------------
# sign-up, confirmed by a code sent to the address
# ---------------------------------------------------------------------------

CODE_MINUTES = 15
CODE_TRIES = 5
RESEND_SECONDS = 60


def _code_hash(email: str, code: str) -> str:
    import hashlib
    import hmac

    from ole5.config import get_settings

    secret = get_settings().session_secret
    key = (secret.get_secret_value() if secret else "ole5-signup").encode()
    return hmac.new(key, f"{email}|{code}".encode(), hashlib.sha256).hexdigest()


def start_signup(email: str, password: str, display_name: str | None = None) -> None:
    """Check the request, store it, and email a 6-digit code to the address.
    The account does not exist until finish_signup is given that code."""
    import secrets

    from ole5.notify import mail

    email = (email or "").strip().lower()
    if not may_register(email):
        raise AuthError("this address cannot create an account here; ask an administrator")
    if len(password or "") < 8:
        raise AuthError("the password must be at least 8 characters")
    if postgres.query_one("SELECT id FROM reviewers WHERE email = %s", (email,)):
        raise AuthError("that email is already registered")
    recent = postgres.query_one(
        "SELECT 1 AS x FROM signup_codes WHERE email = %s "
        "AND created_at > now() - make_interval(secs => %s)", (email, RESEND_SECONDS))
    if recent:
        raise AuthError("a code was just sent to this address; wait a minute before asking again")

    code = f"{secrets.randbelow(1_000_000):06d}"
    postgres.execute(
        """
        INSERT INTO signup_codes (email, code_hash, password_hash, display_name, expires_at)
        VALUES (%s, %s, %s, %s, now() + make_interval(mins => %s))
        ON CONFLICT (email) DO UPDATE SET
            code_hash = EXCLUDED.code_hash, password_hash = EXCLUDED.password_hash,
            display_name = EXCLUDED.display_name, attempts = 0,
            expires_at = EXCLUDED.expires_at, created_at = now()
        """,
        (email, _code_hash(email, code), hash_password(password),
         (display_name or "").strip() or None, CODE_MINUTES),
    )
    body = (f"Your code to create a Support Agent account:\n\n    {code}\n\n"
            f"It works for {CODE_MINUTES} minutes. If you did not ask for this, "
            "ignore this email: no account is created without the code.")
    try:
        sent = mail.deliver(mail.compose(email, "Your Support Agent sign-up code", body))
    except Exception as exc:
        postgres.execute("DELETE FROM signup_codes WHERE email = %s", (email,))
        log.exception("sign-up code not sent", extra={"email": email})
        raise AuthError("the code could not be emailed; ask an administrator") from exc
    if not sent:
        # DRY_RUN: no email goes out. The code is in the server log, so an
        # administrator can still finish a sign-up on a test setup.
        log.warning("sign-up code not emailed (DRY_RUN)", extra={"email": email, "code": code})
    log.info("sign-up code sent", extra={"email": email})


def finish_signup(email: str, code: str) -> int:
    """Create the account if the code is right. Returns the reviewer id."""
    import hmac

    email = (email or "").strip().lower()
    row = postgres.query_one(
        "SELECT code_hash, password_hash, display_name, attempts, expires_at < now() AS expired "
        "FROM signup_codes WHERE email = %s", (email,))
    if row is None:
        raise AuthError("no code is waiting for this address; create the account again")
    if row["expired"]:
        raise AuthError("this code has expired; create the account again for a new one")
    if row["attempts"] >= CODE_TRIES:
        raise AuthError("too many wrong codes; create the account again for a new one")
    if not hmac.compare_digest(row["code_hash"], _code_hash(email, (code or "").strip())):
        postgres.execute("UPDATE signup_codes SET attempts = attempts + 1 WHERE email = %s", (email,))
        raise AuthError("that code is not right")
    if not may_register(email):
        raise AuthError("this address cannot create an account here; ask an administrator")
    if postgres.query_one("SELECT id FROM reviewers WHERE email = %s", (email,)):
        raise AuthError("that email is already registered")
    new = postgres.query_one(
        "INSERT INTO reviewers (email, display_name, password_hash) VALUES (%s, %s, %s) RETURNING id",
        (email, row["display_name"] or email.split("@")[0], row["password_hash"]))
    postgres.execute("DELETE FROM signup_codes WHERE email = %s", (email,))
    log.info("reviewer registered", extra={"email": email})
    return new["id"]


def session_for(reviewer_id: int) -> str:
    """A new sign-in for a reviewer just created."""
    token = secrets.token_urlsafe(32)
    expires = datetime.now(UTC) + timedelta(hours=get_settings().session_hours)
    postgres.execute(
        "INSERT INTO sessions (token, reviewer_id, expires_at) VALUES (%s, %s, %s)",
        (token, reviewer_id, expires),
    )
    return token


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