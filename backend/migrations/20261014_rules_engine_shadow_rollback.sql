-- Reversible companion to 20261014_rules_engine_shadow.sql.
-- These are Rules Engine-exclusive tables introduced by that migration.
BEGIN;
DROP TABLE IF EXISTS rules_engine_audit_events;
DROP TABLE IF EXISTS rules_engine_progress;
DROP TABLE IF EXISTS rules_engine_selection_contributors;
DROP TABLE IF EXISTS rules_engine_selections;
DROP TABLE IF EXISTS rules_engine_evaluations;
DROP TABLE IF EXISTS rules_engine_states;
DROP TABLE IF EXISTS rules_engine_sessions;
DROP TABLE IF EXISTS rules_engine_configuration_versions;
DROP TABLE IF EXISTS rules_engine_definitions;
COMMIT;
