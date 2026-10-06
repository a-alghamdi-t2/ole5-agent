-- Sign-up waits for the address to be confirmed.
--
-- Registering no longer creates the account. It stores the request here and
-- emails a 6-digit code to the address; the account is created only when that
-- code is entered. A mistyped or invented address never receives its code, so
-- it never becomes an account -- which is what makes "@t2.sa" in
-- REGISTRATION_ALLOWLIST safe: only someone who reads that mailbox can join.
--
-- The code is stored hashed. A request expires after 15 minutes and allows
-- 5 wrong tries; registering again replaces it with a new code.

CREATE TABLE signup_codes (
    email          TEXT PRIMARY KEY,
    code_hash      TEXT NOT NULL,
    password_hash  TEXT NOT NULL,
    display_name   TEXT,
    attempts       INT NOT NULL DEFAULT 0,
    expires_at     TIMESTAMPTZ NOT NULL,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
