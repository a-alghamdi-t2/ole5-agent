-- What happened on a closed ticket, from start to finish.
--
-- Two different kinds of field, kept apart on purpose:
--
--   path    -- the exact queues the ticket passed through, from OTRS's own
--              queue notifications (closed_queue_events). Copied, never
--              generated: a model can miscount or invent a step.
--   steps, moves, summary
--           -- what the model wrote after reading the ticket's timeline
--              (ticket_timelines): the steps taken in order, why each move in
--              the path happened, and a short hint for a ticket like this one.
--
-- model is whichever model actually wrote it. fallback_reason is set when the
-- main model failed and the fallback answered instead, and says why.

CREATE TABLE ticket_journeys (
    id                BIGSERIAL PRIMARY KEY,
    closed_ticket_id  BIGINT NOT NULL UNIQUE REFERENCES closed_tickets(id) ON DELETE CASCADE,
    path              TEXT[] NOT NULL DEFAULT '{}',
    steps             JSONB NOT NULL DEFAULT '[]'::jsonb,
    moves             JSONB NOT NULL DEFAULT '[]'::jsonb,
    summary           TEXT,
    model             TEXT,
    fallback_reason   TEXT,
    prompt_ver        TEXT,
    input_chars       INTEGER,
    latency_ms        INTEGER,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now()
);
