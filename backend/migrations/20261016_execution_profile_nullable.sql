-- Stop requiring the retired betting profile for new executions.
-- Existing historical profile values and the column itself are preserved.
ALTER TABLE bet_executions ALTER COLUMN profile DROP NOT NULL;
