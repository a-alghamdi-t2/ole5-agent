-- Postgres cannot drop a value from an enum. Drafts set aside become
-- rejections, and the value stays, unused.
UPDATE drafts SET status = 'rejected' WHERE status = 'other_product';
