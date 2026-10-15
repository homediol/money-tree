-- Retire application-owned bankroll policy from current Rules Engine
-- definitions and allow observation intents without an application cashout.
-- Historical definitions and settlement records remain intact.
BEGIN;

CREATE TABLE IF NOT EXISTS rules_engine_definitions (
  rule_id TEXT NOT NULL,
  rule_version TEXT NOT NULL,
  definition JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  PRIMARY KEY (rule_id, rule_version)
);

INSERT INTO rules_engine_definitions(rule_id, rule_version, definition) VALUES
('USER_PLAYING_POLICY_START_CHECK', 'AVIATOR_RULE_TRANSITIONS_V4',
 $json${"name":"Start a Check: source legacy policy","mode":"START_CHECK","parameters":{"window_size":50},"target_offset":1,"condition":{"standard_trigger":["L","H"],"trigger_h_counts_as_previous_result":true,"standard_stop":["HH","LL"],"two_bet_wins_activates_after_two_failed_ll_cycles":true,"two_bet_wins_stop":"two consecutive selected H outcomes"},"initial_state":{"rule_state":"WAIT_LOW","mode":"STANDARD","previous_outcome":null,"failed_cycle_count":0,"consecutive_bet_wins":0,"pending_target":null},"transition_rules":["WAIT_LOW waits for L; WAIT_HIGH waits for H.","H triggers source+1; legacy reducer seeds previous outcome H.","STANDARD stops on HH or LL; LL terminal L satisfies the low condition.","Two consecutive LL-ending STANDARD cycles activate TWO_BET_WINS.","TWO_BET_WINS stops after two consecutive selected H outcomes; trigger H is not a bet."],"source_semantics_note":"Distinct from All Rules Together; preserves rule-reducer.js behavior."}$json$::jsonb),
('USER_PLAYING_POLICY_LEGACY_TRACKER', 'AVIATOR_RULE_TRANSITIONS_V4',
 $json${"name":"Legacy separate tracker: single rule","mode":"LEGACY_SEPARATE_TRACKER","parameters":{"window_size":50},"target_offset":1,"condition":{"standard_trigger":["L","H"],"trigger_h_counts_as_previous_result":true,"standard_stop":["HH","LL"],"two_bet_wins_activates_after_two_failed_ll_cycles":true,"two_bet_wins_stop":"two consecutive selected H outcomes"},"initial_state":{"rule_state":"WAIT_LOW","mode":"STANDARD","previous_outcome":null,"failed_cycle_count":0,"consecutive_bet_wins":0,"pending_target":null},"transition_rules":["WAIT_LOW waits for L; WAIT_HIGH waits for H.","H triggers source+1; legacy reducer seeds previous outcome H.","STANDARD stops on HH or LL.","Two consecutive LL-ending STANDARD cycles activate TWO_BET_WINS.","TWO_BET_WINS stops after two consecutive selected H outcomes."],"source_semantics_note":"Separate state and outcome ledger; source is live-shadow-test.js."}$json$::jsonb),
('USER_PLAYING_POLICY', 'AVIATOR_RULE_TRANSITIONS_V4',
 $json${"name":"Accepted All Rules Together policy","mode":"ALL_RULES_TOGETHER","parameters":{},"target_offset":1,"condition":{"standard_trigger":["L","H"],"trigger_h_counts_as_selected_result":false,"standard_first_selected_h_stops":true,"after_first_selected_l_stop_on":["HH","LL"],"two_bet_wins_activates_after_two_consecutive_standard_ll_cycles":true,"two_bet_wins_trigger_h_counts_as_selected_result":false,"two_bet_wins_continue_until":["HH","LL"],"two_bet_wins_ll_keeps_mode":true,"two_bet_wins_hh_clears_mode":true},"initial_state":{"rule_state":"WAIT_LOW","mode":"STANDARD","failed_cycle_count":0,"pending_target":null,"selected_result_streak":[]},"transition_rules":["WAIT_LOW waits for L; WAIT_HIGH waits for H.","H proposes source+1 and is not a selected WIN.","In STANDARD, the first selected H ends the cycle; after first selected L, continue to HH or LL.","Two consecutive STANDARD LL cycles activate TWO_BET_WINS; a successful STANDARD cycle resets that streak.","TWO_BET_WINS keeps mode after LL and clears to STANDARD after selected HH.","Only this policy's own matching combined selection advances selected-result counters."],"source_semantics_note":"Accepted All Rules Together contract; nested mode, not a duplicate rule instance."}$json$::jsonb)
ON CONFLICT(rule_id, rule_version) DO NOTHING;

ALTER TABLE IF EXISTS rule_execution_intents
  ALTER COLUMN requested_stake DROP NOT NULL,
  ALTER COLUMN requested_stake_currency DROP NOT NULL,
  ALTER COLUMN cashout_target DROP NOT NULL;

COMMIT;
