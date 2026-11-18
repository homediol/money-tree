from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, inspect as sa_inspect, select
from sqlalchemy.orm import Session

from behaviour_detection.db import make_engine, migrate
from behaviour_detection.models import (ForecastReferenceBlock, HighDominance2xBatch,
                                        HybridCohort, SchemaMigration)


def test_migration_repairs_legacy_phase1_reference_positions():
    engine = make_engine("sqlite:///:memory:")
    migrate(engine)
    with Session(engine) as session, session.begin():
        session.add(ForecastReferenceBlock(
            block_number=1, first_position=202, last_position=301,
            high_count=50, low_count=50, exact_count=0,
            high_rate_nonexact=0.5, sequence_stats={},
            source="phase1_frozen_disjoint_100",
        ))
        session.execute(delete(SchemaMigration).where(SchemaMigration.version >= 4))
    migrate(engine)
    with Session(engine) as session:
        block = session.scalar(select(ForecastReferenceBlock))
        assert (block.first_position, block.last_position) == (201, 300)
    engine.dispose()


def test_migration_creates_confirmed_high_state_test_store_idempotently():
    engine = make_engine("sqlite:///:memory:")
    migrate(engine)
    migrate(engine)
    with Session(engine) as session:
        versions = set(session.scalars(select(SchemaMigration.version)).all())
    assert 13 in versions
    columns = {column["name"] for column in sa_inspect(engine).get_columns(
        "high_dominance_state_tests")}
    assert {"episode_id", "cohort_id", "decision_position", "outcomes",
            "checkpoint_results", "status"}.issubset(columns)
    engine.dispose()


def test_migration_adds_indexes_for_live_round_promotion_queries():
    engine = make_engine("sqlite:///:memory:")
    migrate(engine)
    # Simulate an existing v15 database. The indexes may be absent even though
    # current metadata knows about them; v16 must upgrade that database in place.
    with engine.begin() as connection:
        connection.exec_driver_sql("DROP INDEX canonical_rounds_source_sequence_idx")
        connection.exec_driver_sql(
            "DROP INDEX behaviour_api_round_stream_status_index_idx")
        connection.exec_driver_sql(
            "DELETE FROM behaviour_schema_migrations WHERE version = 16")
    migrate(engine)
    indexes = {
        table: {item["name"]: tuple(item["column_names"])
                for item in sa_inspect(engine).get_indexes(table)}
        for table in ("canonical_rounds", "behaviour_api_round_inbox")
    }
    assert indexes["canonical_rounds"]["canonical_rounds_source_sequence_idx"] == (
        "source_name", "source_sequence")
    assert indexes["behaviour_api_round_inbox"]["behaviour_api_round_stream_status_index_idx"] == (
        "stream_id", "status", "round_index")
    engine.dispose()


def test_migration_anchors_empty_initial_batch_on_next_round():
    engine = make_engine("sqlite:///:memory:")
    migrate(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session, session.begin():
        session.add(HighDominance2xBatch(
            batch_id="cohort:high2x:00000005:0001", cohort_id="cohort",
            series_anchor_position=5, batch_number=1, decision_position=5,
            decision_identity_key="r5", decision_round_id="5",
            decision_source_name="source", decision_source_sequence=5,
            trigger_horizon="10", phase="INITIAL", start_position=6,
            end_position=None, state_at_decision="HIGH_DOMINANT", state_at_end=None,
            lifecycle_status="COLLECTING", coverage_issue=None, outcomes=[],
            wins=0, losses=0, opened_at=now, completed_at=None, updated_at=now,
        ))
        session.execute(delete(SchemaMigration).where(SchemaMigration.version == 10))
    migrate(engine)
    with Session(engine) as session:
        batch = session.get(HighDominance2xBatch, "cohort:high2x:00000005:0001")
        assert batch.start_position == batch.decision_position + 1 == 6
    engine.dispose()


def test_migration_removes_decision_round_from_active_initial_batch():
    engine = make_engine("sqlite:///:memory:")
    migrate(engine)
    now = datetime.now(timezone.utc)
    outcomes = [
        {"cohort_position": 5, "result_at_2x": "LOSS"},
        {"cohort_position": 6, "result_at_2x": "WIN"},
    ]
    with Session(engine) as session, session.begin():
        session.add(HighDominance2xBatch(
            batch_id="cohort:high2x:00000005:0001", cohort_id="cohort",
            series_anchor_position=5, batch_number=1, decision_position=5,
            decision_identity_key="r5", decision_round_id="5",
            decision_source_name="source", decision_source_sequence=5,
            trigger_horizon="10", phase="INITIAL", start_position=5,
            end_position=6, state_at_decision="HIGH_DOMINANT", state_at_end="HIGH_DOMINANT",
            lifecycle_status="COLLECTING", coverage_issue=None, outcomes=outcomes,
            wins=1, losses=1, opened_at=now, completed_at=None, updated_at=now,
        ))
        session.execute(delete(SchemaMigration).where(SchemaMigration.version == 12))
    migrate(engine)
    with Session(engine) as session:
        batch = session.get(HighDominance2xBatch, "cohort:high2x:00000005:0001")
        assert batch.start_position == 6
        assert batch.outcomes == [{"cohort_position": 6, "result_at_2x": "WIN"}]
        assert (batch.wins, batch.losses) == (1, 0)
    engine.dispose()


def test_migration_shifts_active_continuation_after_corrected_initial_five():
    engine = make_engine("sqlite:///:memory:")
    migrate(engine)
    now = datetime.now(timezone.utc)
    initial_outcomes = [
        {"cohort_position": position, "identity_key": f"r{position}",
         "round_id": str(position), "source_name": "source",
         "source_sequence": position, "result_at_2x": "LOSS" if position == 5 else "WIN"}
        for position in range(5, 10)
    ]
    continuation_outcomes = [
        {"cohort_position": 10, "identity_key": "r10", "round_id": "10",
         "source_name": "source", "source_sequence": 10, "result_at_2x": "WIN"}
    ]
    with Session(engine) as session, session.begin():
        cohort = session.get(HybridCohort, 1)
        cohort.last_cohort_position = 10
        cohort.rolling_horizon_states = {"5": "HIGH_DOMINANT"}
        session.add(HighDominance2xBatch(
            batch_id="cohort:high2x:00000005:0001", cohort_id=cohort.cohort_id,
            series_anchor_position=5, batch_number=1, decision_position=5,
            decision_identity_key="r5", decision_round_id="5",
            decision_source_name="source", decision_source_sequence=5,
            trigger_horizon="5", phase="INITIAL", start_position=5,
            end_position=9, state_at_decision="HIGH_DOMINANT", state_at_end="HIGH_DOMINANT",
            lifecycle_status="COMPLETED", coverage_issue=None, outcomes=initial_outcomes,
            wins=4, losses=1, opened_at=now, completed_at=now, updated_at=now,
        ))
        session.add(HighDominance2xBatch(
            batch_id="cohort:high2x:00000005:0002", cohort_id=cohort.cohort_id,
            series_anchor_position=5, batch_number=2, decision_position=9,
            decision_identity_key="r9", decision_round_id="9",
            decision_source_name="source", decision_source_sequence=9,
            trigger_horizon="5", phase="CONTINUATION", start_position=10,
            end_position=10, state_at_decision="HIGH_DOMINANT", state_at_end="HIGH_DOMINANT",
            lifecycle_status="COLLECTING", coverage_issue=None, outcomes=continuation_outcomes,
            wins=1, losses=0, opened_at=now + timedelta(seconds=1), completed_at=None,
            updated_at=now + timedelta(seconds=1),
        ))
        session.execute(delete(SchemaMigration).where(SchemaMigration.version == 12))
    migrate(engine)
    with Session(engine) as session:
        initial = session.get(HighDominance2xBatch, "cohort:high2x:00000005:0001")
        continuation = session.get(HighDominance2xBatch, "cohort:high2x:00000005:0002")
        assert [row["cohort_position"] for row in initial.outcomes] == [6, 7, 8, 9, 10]
        assert initial.start_position == 6
        assert (initial.wins, initial.losses) == (5, 0)
        assert continuation.decision_position == 10
        assert continuation.decision_identity_key == "r10"
        assert continuation.start_position == 11
        assert continuation.outcomes == []
        assert continuation.lifecycle_status == "COLLECTING"
    engine.dispose()
