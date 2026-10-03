DROP INDEX IF EXISTS idx_closed_junk_fingerprint;
ALTER TABLE closed_tickets
    DROP COLUMN IF EXISTS junk_checked_at,
    DROP COLUMN IF EXISTS junk_fingerprint,
    DROP COLUMN IF EXISTS junk_check_score,
    DROP COLUMN IF EXISTS junk_check_ref,
    DROP COLUMN IF EXISTS junk_check;
