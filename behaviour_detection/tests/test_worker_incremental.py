from datetime import datetime, timezone
from decimal import Decimal
import math

from sqlalchemy import select
from sqlalchemy.orm import Session

from behaviour_detection.analysis import sequence_features
from behaviour_detection.db import make_engine, migrate
from behaviour_detection.models import (
    AnalysisSnapshot, CanonicalRound, CumulativeStats, ForecastReferenceBlock,
    MotifObservation, Next100Assessment, ResearchImport, WorkerCheckpoint,
)
from behaviour_detection import worker
from behaviour_detection.worker import analyse_pending, process_round


def _round(position: int, outcome: str) -> CanonicalRound:
    value = "2.10" if outcome == "H" else "1.90" if outcome == "L" else "2.00"
    return CanonicalRound(
        identity_key=f"fixture:{position}", round_id=str(position), source_round_index=None,
        source_sequence=position, analysis_position=position, multiplier_raw=value,
        multiplier_value=Decimal(value), outcome=outcome, event_timestamp=None,
        ingestion_timestamp=datetime.now(timezone.utc), source_name="fixture",
        timestamp_quality="event_time_unknown", order_quality="fixture_sequence",
        import_snapshot_id=None, inserted_at=datetime.now(timezone.utc))


def test_each_pending_round_writes_a_fresh_expanding_baseline_assessment():
    engine = make_engine("sqlite:///:memory:")
    migrate(engine)
    now = datetime.now(timezone.utc)
    warmup = [_round(position, "H" if position % 2 else "L") for position in range(1, 101)]
    with Session(engine) as session, session.begin():
        session.add_all(warmup)
        new_round = _round(101, "H")
        new_round.multiplier_raw = "10"
        new_round.multiplier_value = Decimal("10")
        session.add(new_round)
        session.add(WorkerCheckpoint(name="analysis", last_position=100, updated_at=now))
        session.add(CumulativeStats(
            scope="global", round_count=100, high_count=50, low_count=50, exact_count=0,
            event_timestamp_count=0, event_timestamp_missing_count=100, timestamp_tie_count=0,
            order_uncertain_count=0, cross_source_overlap_unknown_count=0,
            minimum_multiplier=Decimal("1.90"), maximum_multiplier=Decimal("2.10"),
            multiplier_sum=Decimal("200.00"), log_multiplier_sum=60.0,
            log_multiplier_square_sum=36.0, tail_counts={"ge_5": 0, "ge_10": 0,
                                                         "ge_100": 0, "ge_1000": 0},
            updated_at=now))
        sequence = sequence_features(["H", "L"] * 50)
        session.add(ForecastReferenceBlock(
            block_number=1, first_position=201, last_position=300, high_count=50,
            low_count=50, exact_count=0, high_rate_nonexact=.5,
            sequence_stats=sequence, source="phase1_test"))

    processed = analyse_pending(engine)
    with Session(engine) as session:
        assessments = session.scalars(select(Next100Assessment).order_by(Next100Assessment.id)).all()
        checkpoint = session.get(WorkerCheckpoint, "analysis")
        cumulative = session.get(CumulativeStats, "global")
        snapshot = session.scalar(select(AnalysisSnapshot).order_by(AnalysisSnapshot.id.desc()).limit(1))
        assert processed == 1
        assert len(assessments) == 1
        assert assessments[0].latest_position == 101
        assert assessments[0].expected_high_count is not None
        assert assessments[0].expected_low_count is not None
        assert "Arithmetic mean" in assessments[0].explanation["high_rate"]
        assert "No contextual adjustment is active" in assessments[0].explanation["baseline_evidence"]
        assert checkpoint.last_position == 101
        assert cumulative.tail_counts["ge_5"] == 1
        assert cumulative.tail_counts["ge_10"] == 1
        assert math.isclose(float(cumulative.log_multiplier_square_sum), 36.0 + math.log(10) ** 2)
        assert snapshot.features["global_distribution"]["tail_counts"]["ge_10"] == 1
        assert snapshot.features["global_distribution"]["log_volatility_sample"] > 0
    engine.dispose()


def test_each_new_round_persists_rolling50_deficit_and_phase1_analogue_context():
    engine = make_engine("sqlite:///:memory:")
    migrate(engine)
    now = datetime.now(timezone.utc)
    symbols = ["H", "L"] * 75
    with Session(engine) as session, session.begin():
        historical = []
        for position, symbol in enumerate(symbols, start=1):
            row = _round(position, symbol)
            row.import_snapshot_id = "phase1-live-test"
            row.source_name = "phase1"
            historical.append(row)
        live_rounds = []
        for position in (151, 152):
            live = _round(position, "H")
            live.source_name = "orbit_data_watch"
            live.order_quality = "verified_orbit_delta_aligned_to_phase1"
            live_rounds.append(live)
        session.add_all([*historical, *live_rounds])
        session.add(ResearchImport(
            snapshot_id="phase1-live-test", snapshot_utc=now,
            source_sha256="c" * 64, input_files_sha256="d" * 64,
            source_records=150, canonical_rounds=150, high_count=75,
            low_count=75, exact_count=0, imported_at=now))
        session.add(WorkerCheckpoint(name="analysis", last_position=150, updated_at=now))
        session.add(CumulativeStats(
            scope="global", round_count=150, high_count=75, low_count=75, exact_count=0,
            event_timestamp_count=0, event_timestamp_missing_count=150, timestamp_tie_count=0,
            order_uncertain_count=0, cross_source_overlap_unknown_count=0,
            minimum_multiplier=Decimal("1.90"), maximum_multiplier=Decimal("2.10"),
            multiplier_sum=Decimal("300.00"), log_multiplier_sum=150 * math.log(2),
            log_multiplier_square_sum=150 * math.log(2) ** 2,
            tail_counts={"ge_5": 0, "ge_10": 0, "ge_100": 0, "ge_1000": 0},
            updated_at=now))
        session.add(ForecastReferenceBlock(
            block_number=1, first_position=51, last_position=150, high_count=50,
            low_count=50, exact_count=0, high_rate_nonexact=.5,
            sequence_stats=sequence_features(["H", "L"] * 50), source="phase1_test"))

    assert analyse_pending(engine) == 2
    with Session(engine) as session:
        assessments = session.scalars(select(Next100Assessment).order_by(Next100Assessment.id)).all()
        assessment = assessments[-1]
        snapshot = session.scalar(select(AnalysisSnapshot).order_by(AnalysisSnapshot.id.desc()).limit(1))
        assert [item.latest_position for item in assessments] == [151, 152]
        assert assessment.expected_high_rate_nonexact == Decimal("0.5000000000")
        assert assessments[0].explanation["live_context"]["rolling50"]["counts"] == {"H": 25, "L": 25, "E": 0}
        assert assessment.explanation["live_context"]["rolling50"]["counts"] == {"H": 26, "L": 24, "E": 0}
        assert assessment.explanation["live_context"]["session_history"]["round_count"] == 2
        recovery = assessment.explanation["historical_recovery_comparison"]
        assert recovery["matched_historical_windows"] == 1
        assert recovery["recovery_likelihood"] is None
        assert snapshot.features["live_context"]["analysis_position"] == 152
    engine.dispose()


def test_motif_observations_are_written_only_when_rolling_counts_change(monkeypatch):
    engine = make_engine("sqlite:///:memory:")
    migrate(engine)
    now = datetime.now(timezone.utc)
    initial_motifs = {
        "HH": {"count_last_500": 3, "possible_windows_last_500": 499},
        "HL": {"count_last_500": 2, "possible_windows_last_500": 499},
    }
    changed_motifs = {
        "HH": {"count_last_500": 3, "possible_windows_last_500": 499},
        "HL": {"count_last_500": 3, "possible_windows_last_500": 499},
    }
    motif_results = iter((initial_motifs, changed_motifs))
    monkeypatch.setattr(worker, "motif_evidence", lambda _symbols, _patterns: next(motif_results))
    with Session(engine) as session, session.begin():
        session.add_all([_round(position, "H" if position % 2 else "L")
                         for position in range(1, 103)])
        session.add(WorkerCheckpoint(name="analysis", last_position=100, updated_at=now))
        session.add(CumulativeStats(
            scope="global", round_count=100, high_count=50, low_count=50, exact_count=0,
            event_timestamp_count=0, event_timestamp_missing_count=100, timestamp_tie_count=0,
            order_uncertain_count=0, cross_source_overlap_unknown_count=0,
            minimum_multiplier=Decimal("1.90"), maximum_multiplier=Decimal("2.10"),
            multiplier_sum=Decimal("200.00"), log_multiplier_sum=60.0,
            log_multiplier_square_sum=36.0, tail_counts={"ge_5": 0, "ge_10": 0,
                                                         "ge_100": 0, "ge_1000": 0},
            updated_at=now))
        session.add(ForecastReferenceBlock(
            block_number=1, first_position=1, last_position=100, high_count=50,
            low_count=50, exact_count=0, high_rate_nonexact=.5,
            sequence_stats=sequence_features(["H", "L"] * 50), source="phase1_test"))

    reference = [{"block_number": 1, "first_position": 1, "last_position": 100,
                  "high_count": 50, "low_count": 50, "exact_count": 0,
                  "high_rate_nonexact": .5,
                  "sequence": sequence_features(["H", "L"] * 50)}]
    for position in (101, 102):
        with Session(engine) as session, session.begin():
            row = session.get(CanonicalRound, f"fixture:{position}")
            process_round(session, row, reference)

    with Session(engine) as session:
        observations = session.scalars(select(MotifObservation).order_by(
            MotifObservation.generated_at, MotifObservation.id)).all()
        assert [(item.motif, item.occurrence_count) for item in observations] == [
            ("HH", 3), ("HL", 2), ("HL", 3)]
    engine.dispose()
