-- Past tickets that resemble the one a draft was made for.
--
-- Found when the draft is made, by searching the history index with the new
-- ticket's search_text, and stored with the draft rather than looked up when
-- the page opens: the reviewer, and anyone reading the audit trail later,
-- sees what the matches were at the time the decision was drafted, not what a
-- rebuilt index would say today.
--
-- rank is the order the search returned them in, 1 first. score is the
-- search's own relevance score, kept for tuning a threshold later.

CREATE TABLE draft_similar (
    draft_id          BIGINT NOT NULL REFERENCES drafts(id) ON DELETE CASCADE,
    closed_ticket_id  BIGINT NOT NULL REFERENCES closed_tickets(id) ON DELETE CASCADE,
    rank              SMALLINT NOT NULL,
    score             REAL,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (draft_id, closed_ticket_id)
);

CREATE INDEX idx_draft_similar_draft ON draft_similar (draft_id, rank);
