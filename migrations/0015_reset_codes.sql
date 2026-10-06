-- "Forgot your password?" on the sign-in page.
--
-- Asking for a reset emails a 6-digit code to the account's address; the
-- password changes only when that code is entered with the new one. Only
-- someone who reads that mailbox can reset its password. Like sign-up codes:
-- stored hashed, 15 minutes, 5 wrong tries, a new request replaces the old.
-- A request for an address with no account stores nothing and sends nothing,
-- but the page answers the same, so it cannot be used to find out who has one.

CREATE TABLE reset_codes (
    email       TEXT PRIMARY KEY,
    code_hash   TEXT NOT NULL,
    attempts    INT NOT NULL DEFAULT 0,
    expires_at  TIMESTAMPTZ NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
