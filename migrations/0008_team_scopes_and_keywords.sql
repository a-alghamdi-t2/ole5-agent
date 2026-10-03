-- Two things the team maintains on the Knowledge page, beside the documents.
--
-- team_scopes: one Markdown document describing what each team handles and
-- where the lines between them fall. Read whole by the agent through a tool
-- before it chooses a queue; not indexed, not split. Every upload is kept as a
-- new version and the newest is the one in use, so a change that makes routing
-- worse can be compared with the one before it.
--
-- urgent_keywords: words or phrases that mark a ticket urgent when they appear
-- in the customer's message. Removed ones are kept, switched off, so the audit
-- trail can still name them.

CREATE TABLE team_scopes (
    id           BIGSERIAL PRIMARY KEY,
    content      TEXT NOT NULL CHECK (length(btrim(content)) > 0),
    filename     TEXT,
    uploaded_by  BIGINT REFERENCES reviewers(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE urgent_keywords (
    id           BIGSERIAL PRIMARY KEY,
    keyword      TEXT NOT NULL CHECK (length(btrim(keyword)) > 0),
    active       BOOLEAN NOT NULL DEFAULT true,
    added_by     BIGINT REFERENCES reviewers(id),
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- "Urgent" and "urgent" are one keyword.
CREATE UNIQUE INDEX uq_urgent_keyword ON urgent_keywords (lower(btrim(keyword)));

CREATE TRIGGER urgent_keywords_touch BEFORE UPDATE ON urgent_keywords
    FOR EACH ROW EXECUTE FUNCTION touch_updated_at();
