-- Keep identity validation and committed-event replay on indexed access paths.
-- aviator_rounds.round_index is already UNIQUE; its existing unique index is
-- the ordered access path for newest-round context queries.
BEGIN;

CREATE INDEX IF NOT EXISTS bet_signals_target_round_created_idx
  ON bet_signals(target_round_id, created_at, signal_id);

CREATE INDEX IF NOT EXISTS round_distribution_events_stream_sequence_idx
  ON round_distribution_events(stream_id, stream_sequence);

COMMIT;
