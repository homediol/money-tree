-- ADIE is a SHADOW-only observer. These tables contain research decisions and
-- simulated fixed-stake accounting; they do not connect to wagering actions.
CREATE TABLE IF NOT EXISTS adie_state (
    singleton SMALLINT PRIMARY KEY CHECK (singleton = 1),
    state_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
INSERT INTO adie_state(singleton,state_json,updated_at)
VALUES (1,'{"position":0,"eligible_since_selection":0,"health":"WARMING_UP"}',NOW()::text)
ON CONFLICT(singleton) DO NOTHING;

CREATE TABLE IF NOT EXISTS adie_round_events (
    round_id TEXT PRIMARY KEY,
    position BIGINT NOT NULL UNIQUE,
    stream_id TEXT,
    stream_sequence BIGINT,
    multiplier DOUBLE PRECISION NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('HIGH','LOW','EXACT')),
    observed_at TEXT,
    received_at TEXT,
    verified_at TEXT,
    committed_at TEXT,
    delivery_mode TEXT,
    continuity_status TEXT NOT NULL,
    identity_source TEXT,
    source_round_index BIGINT,
    source_platform_index BIGINT,
    fingerprint_json TEXT NOT NULL,
    event_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS adie_events_position_idx ON adie_round_events(position);

CREATE TABLE IF NOT EXISTS adie_decisions (
    decision_id TEXT PRIMARY KEY,
    round_id TEXT NOT NULL UNIQUE,
    position BIGINT NOT NULL,
    policy_version TEXT NOT NULL,
    cohort TEXT NOT NULL,
    eligible BOOLEAN NOT NULL,
    abstention_reason TEXT,
    context_state TEXT,
    eligible_since_selection INTEGER NOT NULL,
    features_json TEXT NOT NULL,
    candidates_json TEXT NOT NULL,
    selected_forecast_id TEXT,
    decision_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS adie_decisions_policy_idx ON adie_decisions(policy_version,position);

CREATE TABLE IF NOT EXISTS adie_forecasts (
    forecast_id TEXT PRIMARY KEY,
    decision_id TEXT NOT NULL UNIQUE,
    decision_round_id TEXT NOT NULL,
    decision_position BIGINT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('HIGH','LOW')),
    horizon INTEGER NOT NULL CHECK (horizon IN (5,10,20,30,50)),
    start_offset INTEGER NOT NULL CHECK (start_offset BETWEEN 0 AND 10),
    target_start_position BIGINT NOT NULL,
    target_end_position BIGINT NOT NULL,
    target_start_round_id TEXT,
    target_end_round_id TEXT,
    status TEXT NOT NULL,
    selection_status TEXT NOT NULL,
    policy_version TEXT NOT NULL,
    reason TEXT NOT NULL,
    evidence_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    committed_at TEXT NOT NULL,
    activated_at TEXT,
    completed_at TEXT,
    target_start_observed_at TEXT,
    target_end_observed_at TEXT,
    high_count INTEGER NOT NULL DEFAULT 0,
    low_count INTEGER NOT NULL DEFAULT 0,
    exact_count INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    net_units INTEGER NOT NULL DEFAULT 0,
    positive_net BOOLEAN,
    invalidation_reason TEXT
);
CREATE INDEX IF NOT EXISTS adie_forecasts_status_target_idx
    ON adie_forecasts(status,target_start_position,target_end_position);

CREATE TABLE IF NOT EXISTS adie_forecast_rounds (
    forecast_id TEXT NOT NULL,
    position BIGINT NOT NULL,
    round_id TEXT NOT NULL,
    multiplier DOUBLE PRECISION NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('HIGH','LOW','EXACT')),
    accounted_win BOOLEAN NOT NULL,
    recorded_at TEXT NOT NULL,
    PRIMARY KEY(forecast_id,position),
    UNIQUE(forecast_id,round_id)
);

-- Fixed immediate 5/10 HIGH and LOW controls are recorded prospectively at
-- each eligible decision. Their windows overlap by design and are descriptive.
CREATE TABLE IF NOT EXISTS adie_baseline_windows (
    baseline_id TEXT PRIMARY KEY,
    decision_round_id TEXT NOT NULL,
    decision_position BIGINT NOT NULL,
    direction TEXT NOT NULL CHECK (direction IN ('HIGH','LOW')),
    horizon INTEGER NOT NULL CHECK (horizon IN (5,10)),
    start_position BIGINT NOT NULL,
    end_position BIGINT NOT NULL,
    start_round_id TEXT,
    end_round_id TEXT,
    decision_at TEXT NOT NULL,
    status TEXT NOT NULL,
    high_count INTEGER NOT NULL DEFAULT 0,
    low_count INTEGER NOT NULL DEFAULT 0,
    exact_count INTEGER NOT NULL DEFAULT 0,
    wins INTEGER NOT NULL DEFAULT 0,
    losses INTEGER NOT NULL DEFAULT 0,
    net_units INTEGER NOT NULL DEFAULT 0,
    positive_net BOOLEAN,
    UNIQUE(decision_round_id,direction,horizon)
);
CREATE INDEX IF NOT EXISTS adie_baseline_status_idx
    ON adie_baseline_windows(status,start_position,end_position);

CREATE TABLE IF NOT EXISTS adie_baseline_window_rounds (
    baseline_id TEXT NOT NULL,
    position BIGINT NOT NULL,
    round_id TEXT NOT NULL,
    multiplier DOUBLE PRECISION NOT NULL,
    outcome TEXT NOT NULL CHECK (outcome IN ('HIGH','LOW','EXACT')),
    accounted_win BOOLEAN NOT NULL,
    PRIMARY KEY(baseline_id,position),
    UNIQUE(baseline_id,round_id)
);
