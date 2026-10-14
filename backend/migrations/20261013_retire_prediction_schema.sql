-- Reversible archival migration for tables used only by the retired ML,
-- statistical, pattern, frozen-opportunity, backtest, and training systems.
-- Round, account, audit, risk, and financial execution tables stay in public.
BEGIN;
CREATE SCHEMA IF NOT EXISTS retired_prediction_20261013;
DO $$
DECLARE
  table_name text;
  retired_tables text[] := ARRAY[
    'patterns', 'signals', 'signal_results', 'model_versions', 'model_artifacts',
    'training_runs', 'statistics', 'ml_predictions', 'ml_observations',
    'statistical_engine_assessment_telemetry', 'statistical_engine_experiments',
    'statistical_engine_policies', 'statistical_engine_predictions',
    'statistical_engine_preflight_windows', 'statistical_engine_reports',
    'opportunity_observations', 'opportunity_v4_assessments', 'opportunity_v4_outcomes',
    'opportunity_research_gates', 'opportunity_selection_policies', 'opportunity_experiments',
    'opportunity_experiment_targets', 'opportunity_experiment_predictions',
    'opportunity_experiment_checkpoints', 'opportunity_v4_observer_queue_control',
    'opportunity_v4_observer_queue', 'prediction_evidence', 'prediction_evaluations',
    'readiness_state', 'model_candidates', 'analytics_reports', 'backtest_runs', 'research_runs',
    'decision_records', 'decision_visibility_records', 'decision_evaluation_failures', 'rule_based_decisions',
    'rule_based_decision_results', 'rule_based_config_audit', 'live_shadow_rule_experiments',
    'live_shadow_rule_rounds', 'live_shadow_rule_interruptions', 'live_shadow_rule_phase_events'
  ];
BEGIN
  FOREACH table_name IN ARRAY retired_tables LOOP
    IF to_regclass(format('public.%I', table_name)) IS NOT NULL THEN
      EXECUTE format('ALTER TABLE public.%I SET SCHEMA retired_prediction_20261013', table_name);
    END IF;
  END LOOP;
END $$;
COMMIT;
