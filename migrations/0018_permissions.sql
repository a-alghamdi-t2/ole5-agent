-- Who may do what in the console. An admin may do everything, including
-- giving others their permissions on the Users page. Everyone else has the
-- permissions listed, and a new account starts with review and tables.
-- Existing accounts start the same; an admin ticks the rest.
ALTER TABLE reviewers ADD COLUMN is_admin BOOLEAN NOT NULL DEFAULT false;
ALTER TABLE reviewers ADD COLUMN permissions TEXT[] NOT NULL DEFAULT ARRAY['review', 'tables'];

-- The first admin of the OLE5 console.
UPDATE reviewers SET is_admin = true WHERE lower(email) = 'a.alghamdi@t2.sa';

-- Never without an admin: if that account is not here (another product's
-- database, say), the oldest account is. A database with no accounts yet gets
-- its admin at the first sign-up instead (web/auth.py).
UPDATE reviewers SET is_admin = true
WHERE id = (SELECT id FROM reviewers WHERE is_active ORDER BY created_at, id LIMIT 1)
  AND NOT EXISTS (SELECT 1 FROM reviewers WHERE is_admin);
