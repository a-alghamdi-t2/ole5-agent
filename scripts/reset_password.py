"""Reset a reviewer's password when they have forgotten it.

    docker compose exec ole5 python scripts/reset_password.py name@t2.sa

Sets a temporary password and prints it, and signs the reviewer out
everywhere. Give it to them; they sign in with it and change it from the
console ("Change password", under their name). Only someone with access to
the server can run this, which is the point: there is no reset by email.
"""

from __future__ import annotations

import secrets
import sys

from ole5.db import audit, postgres
from ole5.web import auth


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit("usage: python scripts/reset_password.py <reviewer email>")
    email = sys.argv[1].strip().lower()
    try:
        row = postgres.query_one("SELECT id FROM reviewers WHERE email = %s", (email,))
        if row is None:
            sys.exit(f"no reviewer {email}")
        temporary = secrets.token_urlsafe(9)
        auth.set_password(row["id"], temporary)
        audit.record(actor="reviewer", action="password_reset", reasoning=email,
                     evidence={"reviewer_id": row["id"], "by": "reset_password script"})
        print(f"{email}: temporary password  {temporary}")
        print("Signed out everywhere. They sign in with it, then change it under their name.")
    finally:
        postgres.close_pool()


if __name__ == "__main__":
    main()
