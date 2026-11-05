BEGIN;

CREATE TABLE IF NOT EXISTS uivision_macro_mappings (
  mapping_id TEXT PRIMARY KEY,
  rule_id TEXT NOT NULL UNIQUE,
  macro_name TEXT NOT NULL UNIQUE,
  enabled BOOLEAN NOT NULL DEFAULT FALSE,
  shadow_safe_confirmed BOOLEAN NOT NULL DEFAULT FALSE,
  updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS uivision_macro_executions (
  execution_id TEXT PRIMARY KEY,
  idempotency_key TEXT NOT NULL UNIQUE,
  session_id TEXT NOT NULL,
  rule_id TEXT NOT NULL,
  evaluation_id TEXT NOT NULL,
  round_id TEXT NOT NULL,
  evaluation_timestamp TIMESTAMPTZ NOT NULL,
  mapping_id TEXT,
  macro_id TEXT NOT NULL,
  macro_name TEXT,
  rule_result BOOLEAN NOT NULL CHECK (rule_result IS TRUE),
  status TEXT NOT NULL CHECK (status IN
    ('PENDING','DISPATCHED','RUNNING','COMPLETED','FAILED','TIMEOUT','CANCELLED')),
  trigger_timestamp TIMESTAMPTZ NOT NULL,
  dispatch_timestamp TIMESTAMPTZ,
  start_timestamp TIMESTAMPTZ,
  completion_timestamp TIMESTAMPTZ,
  duration_ms BIGINT,
  transport_response JSONB,
  error_message TEXT,
  retry_count INTEGER NOT NULL DEFAULT 0 CHECK (retry_count >= 0),
  metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  UNIQUE(session_id,rule_id,evaluation_id)
);

CREATE INDEX IF NOT EXISTS uivision_macro_executions_session_idx
  ON uivision_macro_executions(session_id,created_at DESC);
CREATE INDEX IF NOT EXISTS uivision_macro_executions_pending_idx
  ON uivision_macro_executions(status,created_at);

COMMIT;
