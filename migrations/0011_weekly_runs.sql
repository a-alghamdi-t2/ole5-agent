-- The weekly history update: one row per run.
--
-- Every Friday at 04:00 Riyadh time the app adds the tickets closed that week
-- (the four Ole5 queues and Junk) to the history, rebuilds the similar-tickets
-- and junk indexes, and emails a report of what the agent did and how its
-- tickets ended up. This table is its record, and its lock: week_key is
-- unique, so a week is claimed once even if two processes look at the same
-- moment. A run started by hand gets a key of its own.
--
-- report is the email exactly as sent, so any week can be read again.

CREATE TABLE weekly_runs (
    id            BIGSERIAL PRIMARY KEY,
    week_key      TEXT NOT NULL UNIQUE,
    status        TEXT NOT NULL CHECK (status IN ('running', 'done', 'failed')),
    window_start  TIMESTAMPTZ NOT NULL,
    window_end    TIMESTAMPTZ NOT NULL,
    summary       JSONB NOT NULL DEFAULT '{}'::jsonb,
    report        TEXT,
    emailed_to    TEXT[] NOT NULL DEFAULT '{}',
    error         TEXT,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ
);
