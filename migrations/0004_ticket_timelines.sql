-- Everything that happened on a closed ticket, in order, ready to be read.
--
-- One row per closed ticket, fetched again from OTRS: the customer's messages,
-- our replies, internal notes, and each time the ticket arrived in a queue --
-- the "New Ticket in Your Queue" notifications the first export dropped as
-- noise. Generated noise (the feedback-job articles) is still left out.
--
-- entries holds the timeline as a JSON list, each entry already cleaned of
-- signatures, disclaimers and quoted history. queue_path is the queues named
-- by the notifications, in order, repeats collapsed: exact where OTRS recorded
-- them, empty where it did not.
--
-- This is the input for the step that explains each ticket's journey; it does
-- not replace closed_articles, which stays as it is.

CREATE TABLE ticket_timelines (
    closed_ticket_id    BIGINT PRIMARY KEY REFERENCES closed_tickets(id) ON DELETE CASCADE,
    entries             JSONB NOT NULL,
    queue_path          TEXT[] NOT NULL DEFAULT '{}',
    article_count       INTEGER NOT NULL,
    kept_count          INTEGER NOT NULL,
    notification_count  INTEGER NOT NULL,
    noise_count         INTEGER NOT NULL,
    empty_count         INTEGER NOT NULL DEFAULT 0,
    clean_ver           TEXT NOT NULL,
    fetched_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
