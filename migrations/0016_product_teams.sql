-- The teams in ticket_scopes come from TEAM_QUEUES, so another product's
-- teams can be stored. The check listed OLE5's four by name; OLE5's rows are
-- unchanged. Postgres named the inline check ticket_scopes_team_check.
ALTER TABLE ticket_scopes DROP CONSTRAINT IF EXISTS ticket_scopes_team_check;
