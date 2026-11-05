BEGIN;

DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM uivision_macro_executions LIMIT 1) THEN
    RAISE EXCEPTION 'Ui.Vision execution history exists; archive it before rollback';
  END IF;
  IF EXISTS (SELECT 1 FROM uivision_macro_mappings LIMIT 1) THEN
    RAISE EXCEPTION 'Ui.Vision mappings exist; export them before rollback';
  END IF;
END $$;

DROP TABLE IF EXISTS uivision_macro_executions;
DROP TABLE IF EXISTS uivision_macro_mappings;

COMMIT;
