-- The console's Settings page: values a super admin set there, which win over
-- .env. One row per setting; a setting with no row keeps its .env value.
-- Passwords and keys are stored encrypted (secret = true) and never sent back
-- to the browser.
CREATE TABLE app_settings (
    key         TEXT PRIMARY KEY,
    value       TEXT NOT NULL,
    secret      BOOLEAN NOT NULL DEFAULT false,
    updated_by  TEXT,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
