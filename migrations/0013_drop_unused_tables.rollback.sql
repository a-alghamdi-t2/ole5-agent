-- Recreates ticket_extractions and replays, empty, as they were defined.
-- eval_runs, eval_results and kb_entries, and the data of all five, come back
-- only from the backup taken before 0013 was applied:
--
--   docker compose cp .\backup-unused-tables.dump db:/tmp/u.dump
--   docker compose exec db pg_restore -U ole5 -d ole5 --clean --if-exists /tmp/u.dump

CREATE TABLE IF NOT EXISTS ticket_extractions (
    id                BIGSERIAL PRIMARY KEY,
    closed_ticket_id  BIGINT NOT NULL UNIQUE REFERENCES closed_tickets(id) ON DELETE CASCADE,
    request_raw       TEXT,
    question          TEXT NOT NULL,
    missing           JSONB NOT NULL DEFAULT '[]'::jsonb,
    answer            TEXT,
    model             TEXT,
    prompt_ver        TEXT,
    latency_ms        INTEGER,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_extractions_ticket ON ticket_extractions (closed_ticket_id);

CREATE TABLE IF NOT EXISTS replays (
    id                BIGSERIAL PRIMARY KEY,
    closed_ticket_id  BIGINT NOT NULL REFERENCES closed_tickets(id) ON DELETE CASCADE,
    run_id            TEXT NOT NULL,
    expected_action   TEXT NOT NULL,
    got_action        TEXT NOT NULL,
    expected          JSONB NOT NULL,
    got               JSONB NOT NULL,
    reply_body        TEXT,
    reasoning         TEXT,
    corrections       JSONB NOT NULL DEFAULT '[]'::jsonb,
    searches          JSONB NOT NULL DEFAULT '[]'::jsonb,
    model             TEXT,
    prompt_ver        TEXT,
    seconds           NUMERIC(6,1),
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (run_id, closed_ticket_id)
);
CREATE INDEX IF NOT EXISTS idx_replays_run ON replays (run_id);
