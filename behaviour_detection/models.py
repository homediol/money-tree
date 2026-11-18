from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Index, Integer, JSON, Numeric, String, Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class SchemaMigration(Base):
    __tablename__ = "behaviour_schema_migrations"
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    applied_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchImport(Base):
    __tablename__ = "research_imports"
    snapshot_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    snapshot_utc: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    input_files_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_records: Mapped[int] = mapped_column(Integer, nullable=False)
    canonical_rounds: Mapped[int] = mapped_column(Integer, nullable=False)
    high_count: Mapped[int] = mapped_column(Integer, nullable=False)
    low_count: Mapped[int] = mapped_column(Integer, nullable=False)
    exact_count: Mapped[int] = mapped_column(Integer, nullable=False)
    imported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class ResearchFinding(Base):
    __tablename__ = "phase1_research_findings"
    snapshot_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    finding_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)


class CanonicalRound(Base):
    __tablename__ = "canonical_rounds"
    identity_key: Mapped[str] = mapped_column(String(180), primary_key=True)
    round_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    source_round_index: Mapped[str | None] = mapped_column(String(80), nullable=True)
    source_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    analysis_position: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    multiplier_raw: Mapped[str] = mapped_column(Text, nullable=False)
    multiplier_value: Mapped[Decimal] = mapped_column(Numeric(50, 24), nullable=False)
    outcome: Mapped[str] = mapped_column(String(5), nullable=False)
    event_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingestion_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_name: Mapped[str] = mapped_column(String(120), nullable=False)
    timestamp_quality: Mapped[str] = mapped_column(String(40), nullable=False)
    order_quality: Mapped[str] = mapped_column(String(50), nullable=False)
    import_snapshot_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    inserted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        Index("canonical_rounds_event_time_idx", "event_timestamp"),
        Index("canonical_rounds_ingestion_time_idx", "ingestion_timestamp"),
        Index("canonical_rounds_outcome_position_idx", "outcome", "analysis_position"),
        Index("canonical_rounds_source_round_id_idx", "source_name", "round_id"),
        Index("canonical_rounds_source_sequence_idx", "source_name", "source_sequence"),
    )


class BehaviourApiRound(Base):
    """Behaviour-owned durable inbox for verified pre-PostgreSQL observations."""
    __tablename__ = "behaviour_api_round_inbox"
    identity_key: Mapped[str] = mapped_column(String(180), primary_key=True)
    stream_id: Mapped[str] = mapped_column(String(120), nullable=False)
    round_id: Mapped[str] = mapped_column(String(240), nullable=False)
    round_index: Mapped[int] = mapped_column(Integer, nullable=False)
    multiplier_raw: Mapped[str] = mapped_column(String(80), nullable=False)
    observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    platform_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default="STAGED")
    __table_args__ = (
        UniqueConstraint("stream_id", "round_id", name="behaviour_api_round_identity_uq"),
        UniqueConstraint("stream_id", "round_index", name="behaviour_api_round_index_uq"),
        Index("behaviour_api_round_status_index_idx", "status", "round_index"),
        Index("behaviour_api_round_stream_status_index_idx", "stream_id", "status", "round_index"),
    )


class SourceRecord(Base):
    __tablename__ = "raw_source_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[str] = mapped_column(String(80), nullable=False)
    source: Mapped[str] = mapped_column(String(180), nullable=False)
    source_row: Mapped[str] = mapped_column(String(180), nullable=False)
    research_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    reconciliation_status: Mapped[str] = mapped_column(String(100), nullable=False)
    reconciliation_detail: Mapped[str | None] = mapped_column(Text, nullable=True)
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    __table_args__ = (
        UniqueConstraint("snapshot_id", "source", "source_row", name="raw_source_identity_uq"),
        Index("raw_source_research_key_idx", "research_key"),
        Index("raw_source_status_idx", "reconciliation_status"),
    )


class RoundSourceMapping(Base):
    __tablename__ = "round_source_mappings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    snapshot_id: Mapped[str] = mapped_column(String(80), nullable=False)
    identity_key: Mapped[str] = mapped_column(ForeignKey("canonical_rounds.identity_key"), nullable=False)
    source_record_id: Mapped[int] = mapped_column(ForeignKey("raw_source_records.id"), nullable=False)
    relation: Mapped[str] = mapped_column(String(100), nullable=False)
    __table_args__ = (
        UniqueConstraint("source_record_id", "identity_key", name="round_source_mapping_uq"),
        Index("round_source_mapping_round_idx", "identity_key"),
    )


class OrbitReading(Base):
    __tablename__ = "orbit_ingestion_records"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    watch_id: Mapped[str] = mapped_column(String(80), nullable=False)
    reading_id: Mapped[str] = mapped_column(String(40), nullable=False)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw_payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(String(60), nullable=False)
    validation_error: Mapped[str | None] = mapped_column(String(100), nullable=True)
    __table_args__ = (
        UniqueConstraint("watch_id", "reading_id", name="orbit_reading_identity_uq"),
        Index("orbit_ingestion_received_idx", "received_at"),
    )


class OrbitWatchState(Base):
    __tablename__ = "orbit_watch_state"
    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    watch_id: Mapped[str] = mapped_column(String(80), nullable=False)
    cursor: Mapped[str] = mapped_column(String(40), nullable=False, default="0")
    last_snapshot: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    source_position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    continuity_status: Mapped[str] = mapped_column(String(60), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WinnerPredictSyncState(Base):
    __tablename__ = "winner_predict_sync_state"
    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    last_round_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_round_id: Mapped[str] = mapped_column(String(240), nullable=False)
    anchor_round_index: Mapped[int] = mapped_column(Integer, nullable=False)
    anchor_round_id: Mapped[str] = mapped_column(String(240), nullable=False)
    overlap_rounds: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    synced_round_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_latest_round_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    source_latest_stored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TimeGroup(Base):
    __tablename__ = "time_based_groups"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    first_position: Mapped[int] = mapped_column(Integer, nullable=False)
    last_position: Mapped[int] = mapped_column(Integer, nullable=False)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    duration_seconds: Mapped[float | None] = mapped_column(Numeric(16, 4), nullable=True)
    threshold_seconds: Mapped[float] = mapped_column(Numeric(12, 4), nullable=False)
    round_count: Mapped[int] = mapped_column(Integer, nullable=False)
    stats: Mapped[dict] = mapped_column(JSON, nullable=False)
    timestamp_quality: Mapped[str] = mapped_column(String(60), nullable=False)
    __table_args__ = (
        UniqueConstraint("first_position", "last_position", name="time_group_position_uq"),
        Index("time_group_start_idx", "start_at"),
    )


class AnalysisSnapshot(Base):
    __tablename__ = "behaviour_analysis_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    latest_identity_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    latest_position: Mapped[int] = mapped_column(Integer, nullable=False)
    round_count: Mapped[int] = mapped_column(Integer, nullable=False)
    features: Mapped[dict] = mapped_column(JSON, nullable=False)
    source_quality: Mapped[dict] = mapped_column(JSON, nullable=False)
    __table_args__ = (Index("analysis_snapshot_generated_idx", "generated_at"),)


class DistributionSnapshot(Base):
    __tablename__ = "high_low_distribution_snapshots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    horizon: Mapped[str] = mapped_column(String(40), nullable=False)
    counts: Mapped[dict] = mapped_column(JSON, nullable=False)
    statistics: Mapped[dict] = mapped_column(JSON, nullable=False)
    __table_args__ = (Index("distribution_snapshot_horizon_idx", "horizon", "generated_at"),)


class SequenceObservation(Base):
    __tablename__ = "sequence_observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    identity_key: Mapped[str] = mapped_column(ForeignKey("canonical_rounds.identity_key"), nullable=False)
    analysis_position: Mapped[int] = mapped_column(Integer, nullable=False)
    symbol: Mapped[str] = mapped_column(String(1), nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    order_quality: Mapped[str] = mapped_column(String(50), nullable=False)
    __table_args__ = (
        UniqueConstraint("identity_key", name="sequence_observation_round_uq"),
        Index("sequence_observation_position_idx", "analysis_position"),
    )


class OrbitRoundMapping(Base):
    __tablename__ = "orbit_round_observation_mappings"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    identity_key: Mapped[str] = mapped_column(ForeignKey("canonical_rounds.identity_key"), nullable=False)
    orbit_reading_id: Mapped[int] = mapped_column(ForeignKey("orbit_ingestion_records.id"), nullable=False)
    item_index: Mapped[int] = mapped_column(Integer, nullable=False)
    source_identity: Mapped[str] = mapped_column(String(240), nullable=False, unique=True)
    cross_source_overlap: Mapped[str] = mapped_column(String(60), nullable=False)
    __table_args__ = (UniqueConstraint("orbit_reading_id", "item_index", name="orbit_mapping_item_uq"),)


class DiscoveredMotif(Base):
    __tablename__ = "discovered_motifs"
    motif: Mapped[str] = mapped_column(String(12), primary_key=True)
    length: Mapped[int] = mapped_column(Integer, nullable=False)
    historical_count: Mapped[int] = mapped_column(Integer, nullable=False)
    relative_frequency: Mapped[float | None] = mapped_column(Numeric(14, 10), nullable=True)
    shuffle_p_value: Mapped[float | None] = mapped_column(Numeric(14, 10), nullable=True)
    bh_q_value: Mapped[float | None] = mapped_column(Numeric(14, 10), nullable=True)
    evidence_status: Mapped[str] = mapped_column(String(60), nullable=False)
    research_snapshot_id: Mapped[str] = mapped_column(String(80), nullable=False)


class MotifResearchEvidence(Base):
    __tablename__ = "motif_research_evidence"
    motif: Mapped[str] = mapped_column(String(12), primary_key=True)
    research_snapshot_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    details: Mapped[dict] = mapped_column(JSON, nullable=False)


class MotifObservation(Base):
    __tablename__ = "motif_observations"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    motif: Mapped[str] = mapped_column(String(12), nullable=False)
    horizon: Mapped[int] = mapped_column(Integer, nullable=False)
    occurrence_count: Mapped[int] = mapped_column(Integer, nullable=False)
    possible_windows: Mapped[int] = mapped_column(Integer, nullable=False)
    __table_args__ = (Index("motif_observation_lookup_idx", "motif", "generated_at"),)


class BehaviourStateHistory(Base):
    __tablename__ = "behaviour_state_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    generated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    latest_identity_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    observed_state: Mapped[str] = mapped_column(String(40), nullable=False)
    confirmed_state: Mapped[str] = mapped_column(String(40), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSON, nullable=False)
    rule_version: Mapped[str] = mapped_column(String(40), nullable=False)
    __table_args__ = (Index("state_history_generated_idx", "generated_at"),)


class RuleDefinition(Base):
    __tablename__ = "rule_definitions"
    rule_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    version: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(180), nullable=False)
    category: Mapped[str] = mapped_column(String(60), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    formula: Mapped[str] = mapped_column(Text, nullable=False)
    input_features: Mapped[list] = mapped_column(JSON, nullable=False)
    thresholds: Mapped[dict] = mapped_column(JSON, nullable=False)
    minimum_evidence: Mapped[dict] = mapped_column(JSON, nullable=False)
    evaluation_frequency: Mapped[str] = mapped_column(String(60), nullable=False)
    validation_status: Mapped[str] = mapped_column(String(160), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class RuleEvaluation(Base):
    __tablename__ = "rule_evaluation_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[str] = mapped_column(String(100), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(40), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    latest_identity_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    result: Mapped[str] = mapped_column(String(60), nullable=False)
    evidence_class: Mapped[str] = mapped_column(String(50), nullable=False)
    explanation: Mapped[dict] = mapped_column(JSON, nullable=False)
    __table_args__ = (Index("rule_evaluation_time_idx", "rule_id", "evaluated_at"),)


class ForecastReferenceBlock(Base):
    __tablename__ = "forecast_reference_blocks"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    block_number: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    first_position: Mapped[int] = mapped_column(Integer, nullable=False)
    last_position: Mapped[int] = mapped_column(Integer, nullable=False)
    high_count: Mapped[int] = mapped_column(Integer, nullable=False)
    low_count: Mapped[int] = mapped_column(Integer, nullable=False)
    exact_count: Mapped[int] = mapped_column(Integer, nullable=False)
    high_rate_nonexact: Mapped[float] = mapped_column(Numeric(14, 10), nullable=False)
    sequence_stats: Mapped[dict] = mapped_column(JSON, nullable=False)
    source: Mapped[str] = mapped_column(String(60), nullable=False)


class Next100Assessment(Base):
    __tablename__ = "next100_assessments"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    latest_identity_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    latest_position: Mapped[int] = mapped_column(Integer, nullable=False)
    training_blocks: Mapped[int] = mapped_column(Integer, nullable=False)
    expected_high_rate_nonexact: Mapped[float | None] = mapped_column(Numeric(14, 10), nullable=True)
    expected_exact_rate: Mapped[float | None] = mapped_column(Numeric(14, 10), nullable=True)
    expected_high_count: Mapped[float | None] = mapped_column(Numeric(14, 6), nullable=True)
    expected_low_count: Mapped[float | None] = mapped_column(Numeric(14, 6), nullable=True)
    expected_exact_count: Mapped[float | None] = mapped_column(Numeric(14, 6), nullable=True)
    historical_count_distribution: Mapped[dict] = mapped_column(JSON, nullable=False)
    threshold_probabilities: Mapped[dict] = mapped_column(JSON, nullable=False)
    data_sufficiency: Mapped[str] = mapped_column(String(60), nullable=False)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    active_rules: Mapped[list] = mapped_column(JSON, nullable=False)
    explanation: Mapped[dict] = mapped_column(JSON, nullable=False)
    __table_args__ = (Index("next100_assessed_time_idx", "assessed_at"),)


class SequenceOutlookResult(Base):
    __tablename__ = "sequence_outlook_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    latest_identity_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    historical_windows: Mapped[int] = mapped_column(Integer, nullable=False)
    outlook: Mapped[dict] = mapped_column(JSON, nullable=False)
    evidence_status: Mapped[str] = mapped_column(String(60), nullable=False)


class ForecastHistory(Base):
    __tablename__ = "forecast_history"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    assessed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    latest_identity_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    latest_position: Mapped[int] = mapped_column(Integer, nullable=False)
    forecast_type: Mapped[str] = mapped_column(String(60), nullable=False)
    forecast: Mapped[dict] = mapped_column(JSON, nullable=False)
    change_from_previous: Mapped[dict] = mapped_column(JSON, nullable=False)
    __table_args__ = (Index("forecast_history_time_idx", "forecast_type", "assessed_at"),)


class ForecastEvaluationResult(Base):
    __tablename__ = "forecast_evaluation_results"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    forecast_id: Mapped[int] = mapped_column(ForeignKey("forecast_history.id"), nullable=False)
    evaluated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    horizon_end_position: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[dict] = mapped_column(JSON, nullable=False)
    metrics: Mapped[dict] = mapped_column(JSON, nullable=False)


class CollectorActivity(Base):
    __tablename__ = "collector_activity"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, nullable=False)
    __table_args__ = (Index("collector_activity_time_idx", "occurred_at"),)


class ApiError(Base):
    __tablename__ = "api_errors"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    component: Mapped[str] = mapped_column(String(80), nullable=False)
    error_code: Mapped[str] = mapped_column(String(120), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, nullable=False)


class SystemHealth(Base):
    __tablename__ = "system_health"
    component: Mapped[str] = mapped_column(String(80), primary_key=True)
    state: Mapped[str] = mapped_column(String(40), nullable=False)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    details: Mapped[dict] = mapped_column(JSON, nullable=False)


class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    action: Mapped[str] = mapped_column(String(120), nullable=False)
    object_type: Mapped[str] = mapped_column(String(100), nullable=False)
    object_id: Mapped[str | None] = mapped_column(String(180), nullable=True)
    details: Mapped[dict] = mapped_column(JSON, nullable=False)


class WorkerCheckpoint(Base):
    __tablename__ = "worker_checkpoints"
    name: Mapped[str] = mapped_column(String(100), primary_key=True)
    last_position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HybridCohort(Base):
    """One prospective, locally anchored sequence for hybrid behaviour analysis."""
    __tablename__ = "hybrid_behaviour_cohorts"
    singleton_id: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    cohort_id: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    activated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_cohort_position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_identity_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    confirmed_state: Mapped[str] = mapped_column(String(40), nullable=False, default="INSUFFICIENT_DATA")
    candidate_state: Mapped[str] = mapped_column(String(40), nullable=False, default="INSUFFICIENT_DATA")
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confirmed_momentum: Mapped[str] = mapped_column(String(40), nullable=False, default="INSUFFICIENT_DATA")
    candidate_momentum: Mapped[str] = mapped_column(String(40), nullable=False, default="INSUFFICIENT_DATA")
    momentum_candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rolling_horizon_states: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class HybridCohortRound(Base):
    """Verified outcomes assigned a local prospective position after activation."""
    __tablename__ = "hybrid_behaviour_cohort_rounds"
    identity_key: Mapped[str] = mapped_column(
        ForeignKey("canonical_rounds.identity_key"), primary_key=True)
    cohort_id: Mapped[str] = mapped_column(String(100), nullable=False)
    cohort_position: Mapped[int] = mapped_column(Integer, nullable=False)
    window_number: Mapped[int] = mapped_column(Integer, nullable=False)
    outcome: Mapped[str] = mapped_column(String(5), nullable=False)
    source_name: Mapped[str] = mapped_column(String(120), nullable=False)
    source_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    order_quality: Mapped[str] = mapped_column(String(60), nullable=False)
    timestamp_quality: Mapped[str] = mapped_column(String(40), nullable=False)
    event_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    source_observed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ingestion_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    __table_args__ = (
        UniqueConstraint("cohort_id", "cohort_position", name="hybrid_cohort_position_uq"),
        Index("hybrid_cohort_window_position_idx", "cohort_id", "window_number", "cohort_position"),
    )


class HighDominance2xBatch(Base):
    """Prospective five-round 2x evaluation batches opened by HIGH dominance."""
    __tablename__ = "high_dominance_2x_batches"
    batch_id: Mapped[str] = mapped_column(String(180), primary_key=True)
    cohort_id: Mapped[str] = mapped_column(String(100), nullable=False)
    series_anchor_position: Mapped[int] = mapped_column(Integer, nullable=False)
    batch_number: Mapped[int] = mapped_column(Integer, nullable=False)
    decision_position: Mapped[int] = mapped_column(Integer, nullable=False)
    decision_identity_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    decision_round_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    decision_source_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    decision_source_sequence: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trigger_horizon: Mapped[str | None] = mapped_column(String(10), nullable=True)
    phase: Mapped[str] = mapped_column(String(20), nullable=False, default="INITIAL")
    start_position: Mapped[int] = mapped_column(Integer, nullable=False)
    end_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    state_at_decision: Mapped[str] = mapped_column(String(40), nullable=False)
    state_at_end: Mapped[str | None] = mapped_column(String(40), nullable=True)
    lifecycle_status: Mapped[str] = mapped_column(String(40), nullable=False)
    coverage_issue: Mapped[str | None] = mapped_column(String(80), nullable=True)
    outcomes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    wins: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    losses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("cohort_id", "series_anchor_position", "batch_number",
                         name="high_dominance_2x_batch_number_uq"),
        Index("high_dominance_2x_batch_lookup_idx", "cohort_id", "series_anchor_position",
              "batch_number"),
    )


class HighDominanceSeriesState(Base):
    """Atomic durable snapshot of the prospective HIGH series state machine."""
    __tablename__ = "high_dominance_series_state"
    cohort_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class BehaviourStrategySettlement(Base):
    """Idempotent receipt for a durably committed Rules Engine SHADOW result."""
    __tablename__ = "behaviour_strategy_settlements"
    signal_id: Mapped[str] = mapped_column(String(180), primary_key=True)
    cohort_id: Mapped[str] = mapped_column(String(100), nullable=False)
    session_id: Mapped[str] = mapped_column(String(180), nullable=False)
    series_id: Mapped[str] = mapped_column(String(100), nullable=False)
    part: Mapped[str] = mapped_column(String(40), nullable=False)
    batch_id: Mapped[str] = mapped_column(String(100), nullable=False)
    configuration_version: Mapped[int] = mapped_column(Integer, nullable=False)
    target_round_id: Mapped[str] = mapped_column(String(240), nullable=False)
    target_round_identity: Mapped[str] = mapped_column(String(180), nullable=False)
    target_stream_id: Mapped[str] = mapped_column(String(180), nullable=False)
    target_round_index: Mapped[int] = mapped_column(Integer, nullable=False)
    result: Mapped[str] = mapped_column(String(8), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        Index("behaviour_strategy_settlement_target_idx", "target_stream_id", "target_round_index"),
        Index("behaviour_strategy_settlement_series_idx", "series_id", "part", "batch_id"),
    )


class HighDominanceStateTest(Base):
    """Prospective evaluation episodes opened on confirmed HIGH state edges."""
    __tablename__ = "high_dominance_state_tests"
    episode_id: Mapped[str] = mapped_column(String(180), primary_key=True)
    cohort_id: Mapped[str] = mapped_column(String(100), nullable=False)
    decision_position: Mapped[int] = mapped_column(Integer, nullable=False)
    decision_identity_key: Mapped[str | None] = mapped_column(String(180), nullable=True)
    decision_round_id: Mapped[str | None] = mapped_column(String(240), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="COLLECTING")
    exit_position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exit_state: Mapped[str | None] = mapped_column(String(40), nullable=True)
    outcomes: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    checkpoint_results: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    wins: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    losses: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    exact_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("cohort_id", "decision_position", name="high_dom_state_test_decision_uq"),
        Index("high_dom_state_test_cohort_position_idx", "cohort_id", "decision_position"),
        Index("high_dom_state_test_active_idx", "cohort_id", "status", "decision_position"),
    )


class DominanceWindow(Base):
    """Durable current/completed 100-observation windows and reproducible reports."""
    __tablename__ = "dominance_windows"
    window_id: Mapped[str] = mapped_column(String(140), primary_key=True)
    cohort_id: Mapped[str] = mapped_column(String(100), nullable=False)
    window_number: Mapped[int] = mapped_column(Integer, nullable=False)
    lifecycle_status: Mapped[str] = mapped_column(String(32), nullable=False)
    start_cohort_position: Mapped[int] = mapped_column(Integer, nullable=False)
    end_cohort_position: Mapped[int] = mapped_column(Integer, nullable=False)
    previous_window_id: Mapped[str | None] = mapped_column(String(140), nullable=True)
    next_window_id: Mapped[str | None] = mapped_column(String(140), nullable=True)
    current_summary: Mapped[dict] = mapped_column(JSON, nullable=False)
    report: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    sound_alerts: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    report_generated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (
        UniqueConstraint("cohort_id", "window_number", name="dominance_window_number_uq"),
        Index("dominance_window_updated_idx", "updated_at"),
    )


class CumulativeStats(Base):
    __tablename__ = "cumulative_round_statistics"
    scope: Mapped[str] = mapped_column(String(40), primary_key=True)
    round_count: Mapped[int] = mapped_column(Integer, nullable=False)
    high_count: Mapped[int] = mapped_column(Integer, nullable=False)
    low_count: Mapped[int] = mapped_column(Integer, nullable=False)
    exact_count: Mapped[int] = mapped_column(Integer, nullable=False)
    event_timestamp_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    event_timestamp_missing_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    timestamp_tie_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    order_uncertain_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cross_source_overlap_unknown_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    minimum_multiplier: Mapped[Decimal] = mapped_column(Numeric(50, 24), nullable=False)
    maximum_multiplier: Mapped[Decimal] = mapped_column(Numeric(50, 24), nullable=False)
    multiplier_sum: Mapped[Decimal] = mapped_column(Numeric(60, 24), nullable=False)
    log_multiplier_sum: Mapped[float] = mapped_column(Numeric(30, 12), nullable=False)
    log_multiplier_square_sum: Mapped[float] = mapped_column(Numeric(30, 12), nullable=False)
    tail_counts: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
