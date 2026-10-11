-- NOT VALID: rows for teams other than OLE5's four may exist by now. The check
-- applies to new rows only.
ALTER TABLE ticket_scopes ADD CONSTRAINT ticket_scopes_team_check
    CHECK (team IN ('operations', 'product', 'product_support', 'customer_success')) NOT VALID;
