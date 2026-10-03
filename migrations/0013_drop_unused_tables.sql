-- Tables nothing uses any more.
--
--   eval_runs, eval_results  an early evaluation, nothing reads or writes them
--   kb_entries               the first knowledge base, replaced by documents
--                            and the vector index
--   replays                  the replay tool (replaying closed tickets through
--                            the agent), removed with it; the weekly report
--                            now measures the agent on real outcomes
--   ticket_extractions       question / missing / answer per closed ticket,
--                            written for the agent's past-tickets tool and the
--                            replay tool, both removed
--
-- Take a backup of the five before applying this (see the rollback file):
-- the rollback recreates two of them empty, and restores the data only from
-- that backup.

DROP TABLE IF EXISTS eval_results;
DROP TABLE IF EXISTS eval_runs;
DROP TABLE IF EXISTS kb_entries;
DROP TABLE IF EXISTS replays;
DROP TABLE IF EXISTS ticket_extractions;
