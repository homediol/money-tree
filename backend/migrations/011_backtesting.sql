-- Part 13: reproducible, immutable historical simulation runs.
CREATE TABLE IF NOT EXISTS backtest_runs (
  run_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, dataset_hash TEXT NOT NULL,
  config TEXT NOT NULL, payload TEXT NOT NULL
);
