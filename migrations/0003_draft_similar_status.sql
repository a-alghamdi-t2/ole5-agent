-- What happened when a draft's similar tickets were looked up.
--
--   found     -- matches were stored in draft_similar
--   none      -- the search ran and nothing came back, or the ticket had no text
--   failed    -- the search could not run: the embedding service did not answer
--   no_index  -- the history index had not been built yet
--
-- Without it the review page could only say "none found" whenever
-- draft_similar was empty, which is wrong when the search never ran.
-- NULL on drafts made before this column existed.

ALTER TABLE drafts ADD COLUMN similar_status TEXT
    CHECK (similar_status IN ('found', 'none', 'failed', 'no_index'));
