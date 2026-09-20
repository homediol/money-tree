-- Part 14: isolated strategy experiment reports.
CREATE TABLE IF NOT EXISTS research_runs (
  research_id TEXT PRIMARY KEY, created_at TEXT NOT NULL, dataset_hash TEXT NOT NULL,
  config TEXT NOT NULL, payload TEXT NOT NULL
);
