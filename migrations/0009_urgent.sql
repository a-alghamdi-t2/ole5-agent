-- Urgent tickets: who is told, what marked a draft urgent, and every email sent.
--
-- urgent_recipients: the people emailed when a ticket is urgent, kept by the
-- team on the Knowledge page beside the keywords. Removed ones are switched
-- off, not deleted, so past emails still name them.
--
-- draft_urgent: why a draft was marked urgent. Two signals, kept apart so the
-- email and the page can say which: a keyword from the team's list in the
-- customer's message (a rule the team wrote), or the agent's own judgement
-- from the ticket's content (a model's call).
--
-- urgent_emails: one row per email per recipient -- sent, skipped because
-- DRY_RUN is on or no mail server is configured, or failed with the error.

CREATE TABLE urgent_recipients (
    id          BIGSERIAL PRIMARY KEY,
    email       TEXT NOT NULL CHECK (position('@' in email) > 1),
    name        TEXT,
    active      BOOLEAN NOT NULL DEFAULT true,
    added_by    BIGINT REFERENCES reviewers(id),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_urgent_recipient ON urgent_recipients (lower(btrim(email)));
CREATE TRIGGER urgent_recipients_touch BEFORE UPDATE ON urgent_recipients
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

CREATE TABLE draft_urgent (
    draft_id      BIGINT PRIMARY KEY REFERENCES drafts(id) ON DELETE CASCADE,
    by_keyword    BOOLEAN NOT NULL DEFAULT false,
    keywords      TEXT[] NOT NULL DEFAULT '{}',
    by_agent      BOOLEAN NOT NULL DEFAULT false,
    agent_reason  TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE urgent_emails (
    id              BIGSERIAL PRIMARY KEY,
    draft_id        BIGINT REFERENCES drafts(id) ON DELETE SET NULL,
    ticket_id       BIGINT REFERENCES tickets(id) ON DELETE SET NULL,
    to_address      TEXT NOT NULL,
    subject         TEXT NOT NULL,
    body            TEXT NOT NULL,
    status          TEXT NOT NULL CHECK (status IN ('sent', 'dry_run', 'skipped', 'failed')),
    error           TEXT,
    rfc_message_id  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX idx_urgent_emails_draft ON urgent_emails (draft_id);
