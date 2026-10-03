-- Which Junk tickets the junk index learns from.
--
-- The team's rule: anything in the Junk queue is junk. But the queue also
-- holds two kinds of copy, and learning from either does harm:
--
--   repeat     the same message sent many times (reminder notices, surveys).
--              One is enough; the rest only weigh the index.
--   real_copy  a customer's real request sent several times: the team handled
--              one copy and closed the rest into Junk. Learning from those
--              makes the next customer asking the same thing look like junk.
--
-- Each Junk ticket is checked once, and the result kept here:
--
--   junk_check        kept | repeat | real_copy, or empty until checked
--   junk_check_ref    the ticket it repeats or copies
--   junk_check_score  for real_copy: how closely it matched the real ticket
--   junk_fingerprint  the message with numbers, links and punctuation
--                     removed: the same fingerprint means the same message

ALTER TABLE closed_tickets
    ADD COLUMN junk_check        TEXT CHECK (junk_check IN ('kept', 'repeat', 'real_copy')),
    ADD COLUMN junk_check_ref    TEXT,
    ADD COLUMN junk_check_score  REAL,
    ADD COLUMN junk_fingerprint  TEXT,
    ADD COLUMN junk_checked_at   TIMESTAMPTZ;

CREATE INDEX idx_closed_junk_fingerprint ON closed_tickets (junk_fingerprint)
    WHERE junk_check = 'kept';
