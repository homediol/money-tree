CREATE TABLE IF NOT EXISTS aviator_rounds (
  id BIGSERIAL PRIMARY KEY,
  round_index BIGINT NOT NULL UNIQUE,
  multiplier NUMERIC(12, 2) NOT NULL CHECK (multiplier >= 1),
  timestamp TIMESTAMPTZ,
  source TEXT NOT NULL DEFAULT 'collector',
  raw JSONB NOT NULL DEFAULT '{}'::jsonb,
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS aviator_rounds_timestamp_idx
  ON aviator_rounds (timestamp);
