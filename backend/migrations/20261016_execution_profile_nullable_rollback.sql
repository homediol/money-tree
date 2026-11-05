-- Restore the former constraint with a neutral compatibility value for rows
-- created while the nullable migration was active.
UPDATE bet_executions SET profile='FIXED' WHERE profile IS NULL;
ALTER TABLE bet_executions ALTER COLUMN profile SET NOT NULL;
