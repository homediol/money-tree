-- Part 15: isolated paper sessions, trades and ledger.
CREATE TABLE IF NOT EXISTS shadow_sessions (session_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shadow_trades (execution_id TEXT PRIMARY KEY, session_id TEXT NOT NULL, target_round_id TEXT NOT NULL, decision_id TEXT NOT NULL UNIQUE, status TEXT NOT NULL, payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS shadow_ledger (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, event_type TEXT NOT NULL, amount REAL, balance REAL, recorded_at TEXT NOT NULL, payload TEXT NOT NULL);
