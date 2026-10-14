-- Additive SHADOW Rules Engine persistence. No collector, account, financial,
-- audit, or execution table is altered by this migration.
BEGIN;

CREATE TABLE IF NOT EXISTS rules_engine_definitions (
  rule_id TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  definition JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (rule_id, rule_version)
);

INSERT INTO rules_engine_definitions(rule_id,rule_version,definition) VALUES
('USER_PLAYING_POLICY_START_CHECK','AVIATOR_2X_CONSECUTIVE_STOP_V3',
 '{"name":"Start a Check: source legacy policy","mode":"START_CHECK","target_offset":1,"parameters":{"stake_rwf":100,"cashout_multiplier":2.0,"window_size":50,"profit_target_rwf":400,"loss_limit_rwf":400},"condition":{"standard_trigger":["L","H"],"trigger_h_counts_as_previous_result":true,"standard_stop":["HH","LL"],"two_bet_wins_activates_after_two_failed_ll_cycles":true,"two_bet_wins_stop":"two consecutive selected H outcomes"},"initial_state":{"rule_state":"WAIT_LOW","mode":"STANDARD","previous_outcome":null,"failed_cycle_count":0,"consecutive_bet_wins":0,"pending_target":null},"transition_rules":["Legacy reducer seeds previous result with trigger H.","TWO_BET_WINS follows two consecutive LL-ending STANDARD cycles.","NET +400/-400 stops selections while the window continues observing."]}'),
('USER_PLAYING_POLICY_LEGACY_TRACKER','AVIATOR_2X_CONSECUTIVE_STOP_V3',
 '{"name":"Legacy separate tracker: single rule","mode":"LEGACY_SEPARATE_TRACKER","target_offset":1,"parameters":{"stake_rwf":100,"cashout_multiplier":2.0,"window_size":50,"profit_target_rwf":400,"loss_limit_rwf":400},"condition":{"standard_trigger":["L","H"],"trigger_h_counts_as_previous_result":true,"standard_stop":["HH","LL"],"two_bet_wins_activates_after_two_failed_ll_cycles":true,"two_bet_wins_stop":"two consecutive selected H outcomes"},"initial_state":{"rule_state":"WAIT_LOW","mode":"STANDARD","previous_outcome":null,"failed_cycle_count":0,"consecutive_bet_wins":0,"pending_target":null},"transition_rules":["Separate engine state and outcome ledger; preserve legacy reducer semantics."]}'),
('RECENT_5_H_GE_4','1.0.0',
 '{"name":"Recent 5: at least 4 H","mode":"FIVE_RULE_TRACKER|ALL_RULES_TOGETHER","target_offset":1,"parameters":{},"condition":{"window":5,"outcome":"H","operator":">=","count":4,"includes_source":true,"latest_source_must_be_h":false},"initial_state":{"status":"WARMING_UP","pending_target":null,"failure_count":0,"recovery_count":0},"transition_rules":["Use five consecutive verified completed outcomes including source.","TRUE at four or more H; FALSE only when window is ready."]}'),
('PATTERN_LH','1.0.0',
 '{"name":"Pattern LH","mode":"FIVE_RULE_TRACKER|ALL_RULES_TOGETHER","target_offset":1,"parameters":{},"condition":{"pattern":["L","H"],"includes_source":true},"initial_state":{"status":"WARMING_UP","pending_target":null,"failure_count":0,"recovery_count":0},"transition_rules":["TRUE exactly for the latest verified pair L,H; request its immediate successor once."]}'),
('PATTERN_HH','1.0.0',
 '{"name":"Pattern HH","mode":"FIVE_RULE_TRACKER|ALL_RULES_TOGETHER","target_offset":1,"parameters":{},"condition":{"pattern":["H","H"],"includes_source":true,"reevaluate_each_source":true,"overlap_allowed":true},"initial_state":{"status":"WARMING_UP","pending_target":null,"failure_count":0,"recovery_count":0},"transition_rules":["TRUE exactly for the latest verified pair H,H.","Overlapping patterns may request distinct successive targets."]}'),
('SOURCE_H_MULT_2_3','1.0.0',
 '{"name":"Source multiplier [2,3)","mode":"FIVE_RULE_TRACKER|ALL_RULES_TOGETHER","target_offset":1,"parameters":{},"condition":{"minimum_inclusive":2.0,"maximum_exclusive":3.0},"initial_state":{"status":"WAIT","pending_target":null,"failure_count":0,"recovery_count":0},"transition_rules":["TRUE when original multiplier is >=2.00 and <3.00; request immediate successor once."]}'),
('AFTER_HH_CONTINUE_MAX_3','1.0.0',
 '{"name":"After HH: continue for at most 3 targets","mode":"FIVE_RULE_TRACKER|ALL_RULES_TOGETHER","target_offset":1,"parameters":{},"condition":{"trigger_pattern":["H","H"],"maximum_selections":3,"ignore_triggers_while_active":true,"stop_on_ww_or_ll":false,"rearm_after_third_resolved_target":true},"initial_state":{"cycle_mode":"IDLE","cycle":null,"pending_target":null,"failure_count":0,"recovery_count":0},"transition_rules":["Idle HH starts a private cycle.","Request up to three consecutive targets, one at a time.","Ignore HH while active; rearm at the third resolved target source regardless of result."]}'),
('USER_PLAYING_POLICY','AVIATOR_2X_CONSECUTIVE_STOP_V3',
 '{"name":"Accepted All Rules Together policy","mode":"ALL_RULES_TOGETHER","target_offset":1,"parameters":{},"condition":{"standard_trigger":["L","H"],"trigger_h_counts_as_selected_result":false,"standard_first_selected_h_stops":true,"after_first_selected_l_stop_on":["HH","LL"],"two_bet_wins_activates_after_two_consecutive_standard_ll_cycles":true,"two_bet_wins_trigger_h_counts_as_selected_result":false,"two_bet_wins_continue_until":["HH","LL"],"two_bet_wins_ll_keeps_mode":true,"two_bet_wins_hh_clears_mode":true},"initial_state":{"rule_state":"WAIT_LOW","mode":"STANDARD","failed_cycle_count":0,"pending_target":null,"selected_result_streak":[]},"transition_rules":["Trigger H requests the next target and is not a selected result.","STANDARD first selected H ends cycle; after first L continue to HH/LL.","TWO_BET_WINS keeps mode on LL and clears only on selected HH."]}')
ON CONFLICT(rule_id,rule_version) DO NOTHING;

CREATE TABLE IF NOT EXISTS rules_engine_configuration_versions (
  configuration_version BIGINT GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
  mode TEXT NOT NULL,
  configuration JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rules_engine_sessions (
  session_id TEXT PRIMARY KEY,
  mode TEXT NOT NULL CHECK (mode IN ('START_CHECK','LEGACY_SEPARATE_TRACKER','FIVE_RULE_TRACKER','ALL_RULES_TOGETHER')),
  status TEXT NOT NULL,
  selection_status TEXT NOT NULL,
  configuration_version BIGINT NOT NULL REFERENCES rules_engine_configuration_versions(configuration_version),
  start_boundary JSONB NOT NULL,
  manifest JSONB NOT NULL,
  configuration_snapshot JSONB NOT NULL,
  started_at TIMESTAMPTZ NOT NULL,
  ended_at TIMESTAMPTZ,
  stop_reason TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rules_engine_states (
  session_id TEXT NOT NULL REFERENCES rules_engine_sessions(session_id),
  rule_id TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  state_version BIGINT NOT NULL DEFAULT 1,
  private_state JSONB NOT NULL,
  pending_selection_id TEXT,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (session_id, rule_id)
);

CREATE TABLE IF NOT EXISTS rules_engine_evaluations (
  evaluation_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES rules_engine_sessions(session_id),
  rule_id TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  source_stream_id TEXT NOT NULL,
  source_round_id TEXT NOT NULL,
  source_round_index BIGINT NOT NULL,
  expected_target_index BIGINT,
  eligible BOOLEAN,
  status TEXT NOT NULL,
  reason TEXT NOT NULL,
  evaluated_at TIMESTAMPTZ NOT NULL,
  source_evidence JSONB NOT NULL,
  state_snapshot JSONB NOT NULL,
  UNIQUE (session_id, rule_id, source_stream_id, source_round_index)
);

CREATE TABLE IF NOT EXISTS rules_engine_selections (
  selection_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL REFERENCES rules_engine_sessions(session_id),
  selection_scope TEXT NOT NULL CHECK (selection_scope IN ('COMBINED','INDEPENDENT_RULE')),
  owner_rule_id TEXT,
  target_stream_id TEXT NOT NULL,
  target_round_index BIGINT NOT NULL,
  source_stream_id TEXT NOT NULL,
  source_round_id TEXT NOT NULL,
  source_round_index BIGINT NOT NULL,
  selected_at TIMESTAMPTZ NOT NULL,
  result_status TEXT NOT NULL CHECK (result_status IN ('PENDING','WIN','LOSS','INVALID','UNAVAILABLE')),
  actual_multiplier NUMERIC(16,8),
  resolved_at TIMESTAMPTZ,
  immutable_decision JSONB NOT NULL,
  CHECK ((selection_scope='COMBINED' AND owner_rule_id IS NULL)
      OR (selection_scope='INDEPENDENT_RULE' AND owner_rule_id IS NOT NULL))
);

CREATE UNIQUE INDEX IF NOT EXISTS rules_engine_combined_target_once_idx
  ON rules_engine_selections(session_id,target_stream_id,target_round_index)
  WHERE selection_scope='COMBINED';
CREATE UNIQUE INDEX IF NOT EXISTS rules_engine_independent_rule_target_once_idx
  ON rules_engine_selections(session_id,owner_rule_id,target_stream_id,target_round_index)
  WHERE selection_scope='INDEPENDENT_RULE';

CREATE TABLE IF NOT EXISTS rules_engine_selection_contributors (
  selection_id TEXT NOT NULL REFERENCES rules_engine_selections(selection_id),
  rule_id TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  reason TEXT NOT NULL,
  proposal JSONB NOT NULL,
  PRIMARY KEY (selection_id, rule_id)
);

CREATE TABLE IF NOT EXISTS rules_engine_progress (
  session_id TEXT PRIMARY KEY REFERENCES rules_engine_sessions(session_id),
  cursor_stream_id TEXT,
  cursor_stream_sequence BIGINT NOT NULL DEFAULT 0,
  cursor_round_id TEXT,
  cursor_round_index BIGINT,
  observed_rounds BIGINT NOT NULL DEFAULT 0,
  total_wins BIGINT NOT NULL DEFAULT 0,
  total_losses BIGINT NOT NULL DEFAULT 0,
  progress JSONB NOT NULL DEFAULT '{}'::jsonb,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS rules_engine_audit_events (
  audit_id TEXT PRIMARY KEY,
  session_id TEXT REFERENCES rules_engine_sessions(session_id),
  event_type TEXT NOT NULL,
  recorded_at TIMESTAMPTZ NOT NULL,
  evidence JSONB NOT NULL
);

CREATE INDEX IF NOT EXISTS rules_engine_sessions_status_idx ON rules_engine_sessions(status, started_at DESC);
CREATE INDEX IF NOT EXISTS rules_engine_evaluations_source_idx
  ON rules_engine_evaluations(source_stream_id, source_round_index, evaluated_at);
CREATE INDEX IF NOT EXISTS rules_engine_selections_target_idx
  ON rules_engine_selections(target_stream_id, target_round_index);
CREATE INDEX IF NOT EXISTS rules_engine_audit_session_idx
  ON rules_engine_audit_events(session_id, recorded_at);

COMMIT;
