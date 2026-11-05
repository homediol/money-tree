-- Additive API key scopes, expiry/usage metadata, and key-management audit trail.
-- Existing project keys remain consumer keys with read-only scopes.
BEGIN;

ALTER TABLE external_project_tokens
  ADD COLUMN IF NOT EXISTS key_type TEXT NOT NULL DEFAULT 'consumer';
ALTER TABLE external_project_tokens
  ADD COLUMN IF NOT EXISTS scopes TEXT NOT NULL DEFAULT '["signals:read","rules:read","metrics:read"]';
ALTER TABLE external_project_tokens
  ADD COLUMN IF NOT EXISTS console_managed BOOLEAN NOT NULL DEFAULT FALSE;
ALTER TABLE external_project_tokens
  ADD COLUMN IF NOT EXISTS last_used_at TIMESTAMPTZ;
ALTER TABLE external_project_tokens
  ADD COLUMN IF NOT EXISTS expires_at TIMESTAMPTZ;

CREATE TABLE IF NOT EXISTS api_key_audit_log (
  audit_id TEXT PRIMARY KEY,
  action TEXT NOT NULL,
  token_id TEXT,
  key_name TEXT,
  actor TEXT NOT NULL DEFAULT 'owner',
  created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS api_key_audit_created_idx
  ON api_key_audit_log(created_at DESC);

COMMIT;
