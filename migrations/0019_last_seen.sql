-- When each person was last active, kept on the account. Read from the
-- sessions before, which signing out deletes: someone who had signed in and
-- out showed as "Never signed in" on the Users page.
ALTER TABLE reviewers ADD COLUMN last_seen_at TIMESTAMPTZ;
UPDATE reviewers r SET last_seen_at = (
    SELECT max(coalesce(s.last_seen, s.created_at)) FROM sessions s WHERE s.reviewer_id = r.id);
