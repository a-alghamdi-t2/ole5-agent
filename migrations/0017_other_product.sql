-- "Not ours": a draft for another product's ticket, set aside by a reviewer.
-- Its own status so it is not counted as a rejection.
ALTER TYPE draft_status ADD VALUE IF NOT EXISTS 'other_product';
