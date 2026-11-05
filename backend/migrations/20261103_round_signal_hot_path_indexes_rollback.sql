BEGIN;

DROP INDEX IF EXISTS bet_signals_target_round_created_idx;
DROP INDEX IF EXISTS round_distribution_events_stream_sequence_idx;

COMMIT;
