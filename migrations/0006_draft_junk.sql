-- What the junk check found for a draft's ticket.
--
-- Stored with the draft, like the similar tickets: the reviewer sees what was
-- known when the decision was drafted, and it stays in the record afterwards.
--
-- A flag, nothing more. Nothing is closed or moved to Junk because of it; the
-- agent decides as usual and a person approves.
--
--   score        the nearest junk ticket's similarity, 0 when nothing matched
--   matches      those tickets, for the reviewer to judge by
--   sender_junk  junk tickets from this sender's domain
--   sender_real  real tickets from the same domain
--   flagged      score over the threshold, or the domain has only sent junk
--   reason       why, in words, for the page and the audit trail

CREATE TABLE draft_junk (
    draft_id     BIGINT PRIMARY KEY REFERENCES drafts(id) ON DELETE CASCADE,
    score        REAL NOT NULL DEFAULT 0,
    matches      JSONB NOT NULL DEFAULT '[]'::jsonb,
    sender_junk  INTEGER NOT NULL DEFAULT 0,
    sender_real  INTEGER NOT NULL DEFAULT 0,
    flagged      BOOLEAN NOT NULL DEFAULT false,
    reason       TEXT,
    threshold    REAL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_draft_junk_flagged ON draft_junk (flagged) WHERE flagged;
