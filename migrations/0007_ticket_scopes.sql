-- What each Ole5 team did on each closed ticket: the evidence the team scopes
-- are written from.
--
-- One row per ticket and team, for the four Ole5 New teams only; RiCH queues
-- are not counted. All four rows are written for every ticket extracted, with
-- did and passed_on empty for a team that did not take part, so a ticket that
-- has been done is never picked up again.
--
--   involved   the team appears in the ticket's recorded path or is the queue
--              it was closed in. Only an involved team can have did or
--              passed_on: the model may not attribute work to a team the
--              records do not show.
--   did        the kind of work the team did, in general words
--   passed_on  what the team handed to another team, to whom, and why

CREATE TABLE ticket_scopes (
    closed_ticket_id  BIGINT NOT NULL REFERENCES closed_tickets(id) ON DELETE CASCADE,
    team              TEXT NOT NULL CHECK (team IN
                          ('operations', 'product', 'product_support', 'customer_success')),
    involved          BOOLEAN NOT NULL DEFAULT false,
    did               TEXT,
    passed_on         TEXT,
    model             TEXT,
    fallback_reason   TEXT,
    prompt_ver        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (closed_ticket_id, team)
);

CREATE INDEX idx_ticket_scopes_team ON ticket_scopes (team) WHERE involved;
