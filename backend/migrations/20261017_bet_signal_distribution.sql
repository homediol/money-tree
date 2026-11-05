-- Additive, replayable storage for authoritative Node Rules Engine BET signals.
-- Existing rounds, rules, shadow sessions, execution, and audit data are untouched.
BEGIN;

CREATE TABLE IF NOT EXISTS bet_signals (
  signal_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  round_id TEXT NOT NULL,
  target_round_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  payload TEXT NOT NULL,
  UNIQUE(session_id,target_round_id)
);

CREATE INDEX IF NOT EXISTS bet_signals_created_idx ON bet_signals(created_at DESC);

COMMIT;
