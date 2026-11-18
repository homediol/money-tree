-- Retire automatic/live real-money execution and its financial ledgers.
-- Rules Engine SHADOW sessions and canonical round/outcome history remain intact.
DROP TABLE IF EXISTS decision_audit;
DROP TABLE IF EXISTS bet_executions;
DROP TABLE IF EXISTS execution_events;
DROP TABLE IF EXISTS balance_ledger;
DROP TABLE IF EXISTS reconciliation_records;
DROP TABLE IF EXISTS session_metrics;
DROP TABLE IF EXISTS live_sessions;
DROP TABLE IF EXISTS betting_mode_state;
DROP TABLE IF EXISTS rule_execution_intents;
DROP TABLE IF EXISTS platform_balance_observations;
DROP TABLE IF EXISTS rule_session_financials;

DELETE FROM application_state
WHERE state_key IN ('operational_safety', 'risk_runtime');

UPDATE rule_target_outcomes
SET payload = ((payload::jsonb
    - 'financial_result'
    - 'bet_result'
    - 'execution_id'
    - 'platform_execution_id'
    - 'platform_evidence'
    - 'balance_evidence')
    || jsonb_build_object('evidence_type', 'CANONICAL_ROUND_RESULT_ONLY'))::text;

ALTER TABLE rule_target_outcomes DROP COLUMN IF EXISTS bet_result;
ALTER TABLE rule_target_outcomes DROP COLUMN IF EXISTS reconciled_at;
