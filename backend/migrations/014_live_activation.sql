-- Part 16: immutable live activation session snapshots.
CREATE TABLE IF NOT EXISTS live_sessions (
  session_id TEXT PRIMARY KEY,
  started_at TEXT NOT NULL,
  ended_at TEXT,
  mode TEXT NOT NULL,
  snapshot TEXT NOT NULL,
  payload TEXT NOT NULL
);
