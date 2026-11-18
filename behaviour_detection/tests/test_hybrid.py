from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient

from behaviour_detection.api import create_app
from behaviour_detection.db import make_engine, migrate
from behaviour_detection.hybrid import (analyse_window, rolling_analysis,
                                        time_coverage, update_hysteresis)
from behaviour_detection.high_series import new_state
from behaviour_detection.live_strategy_processor import LiveStrategyProcessor
from behaviour_detection.models import (CanonicalRound, CollectorActivity, DominanceWindow,
    HighDominance2xBatch, HighDominanceSeriesState, HighDominanceStateTest,
    BehaviourApiRound, HybridCohort, HybridCohortRound)
from behaviour_detection.worker import (_update_confirmed_high_state_test,
                                        process_hybrid_round)


def _dict_round(index, outcome, when=None):
    when = when or datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index * 20)
    return {"identity_key": f"r{index}", "round_id": str(index),
            "analysis_position": index, "cohort_position": index, "outcome": outcome,
            "source_name": "winner_predict_postgres", "source_sequence": index,
            "order_quality": "winner_predict_canonical_order_verified",
            "timestamp_quality": "platform_event_time", "event_timestamp": when,
            "source_observed_at": when + timedelta(milliseconds=200),
            "ingestion_timestamp": when + timedelta(milliseconds=400),
            "timestamp_role": "platform_event_time"}


def _canonical(index, outcome, inserted_at, *, order_quality="winner_predict_canonical_order_verified"):
    multiplier = "2.10" if outcome == "H" else "1.90" if outcome == "L" else "2.00"
    when = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=index * 20)
    return CanonicalRound(identity_key=f"winner-predict:{index}", round_id=str(index),
        source_round_index=str(index), source_sequence=index, analysis_position=index,
        multiplier_raw=multiplier, multiplier_value=Decimal(multiplier), outcome=outcome,
        event_timestamp=when, source_observed_at=when + timedelta(milliseconds=200),
        ingestion_timestamp=when + timedelta(milliseconds=400),
        source_name="winner_predict_postgres", timestamp_quality="platform_event_time",
        order_quality=order_quality, import_snapshot_id=None, inserted_at=inserted_at)


def test_rolling_horizons_keep_exact_separate_and_report_descriptive_states():
    rows = [_dict_round(i, "E" if i % 13 == 0 else "H" if i % 5 else "L") for i in range(1, 121)]
    result = rolling_analysis(rows)
    assert tuple(map(int, result["horizons"])) == (5, 10, 20, 30, 50, 100)
    assert result["horizons"]["100"]["counts"]["E"] == 8
    assert result["descriptive_only"] is True
    assert result["horizons"]["5"]["rounds"] == 5


def test_high_series_api_marks_latest_round_pending_until_reducer_commits(monkeypatch, tmp_path):
    from behaviour_detection import api as behaviour_api

    monkeypatch.setattr(behaviour_api.settings, "WINNER_PREDICT_DATABASE_URL", "")
    engine = make_engine(f"sqlite:///{tmp_path / 'high-series-round-sync.sqlite3'}")
    migrate(engine)
    with Session(engine) as session:
        start = session.get(HybridCohort, 1).activated_at + timedelta(seconds=1)
    row = _canonical(1, "H", start + timedelta(seconds=1))
    row_identity = row.identity_key
    with Session(engine) as session, session.begin():
        session.add(row)

    with TestClient(create_app(engine)) as client:
        audit = client.get("/api/v1/behaviour/hybrid").json()["high_dominance_2x"]
        assert audit["round_sync"]["status"] == "ANALYSIS_PENDING"
        assert audit["round_sync"]["latest_verified_identity"] == row_identity
        assert audit["round_sync"]["last_cohort_identity"] is None

        with Session(engine) as session, session.begin():
            process_hybrid_round(session, session.get(CanonicalRound, row_identity))

        audit = client.get("/api/v1/behaviour/hybrid").json()["high_dominance_2x"]
        assert audit["round_sync"]["status"] == "SYNCED"
        assert audit["round_sync"]["last_processed_identity"] is None
        assert audit["round_sync"]["provenance"] == "HISTORICAL_HYDRATION"
    engine.dispose()


def test_window_boundary_report_and_relationship_are_observation_based():
    previous = {"window_id": "cohort:000001", "report": {"closing_state": "LOW_DOMINANT"}}
    rows = [_dict_round(i, "H" if i <= 70 else "L") for i in range(1, 101)]
    report = analyse_window(rows, window_number=2, cohort_id="cohort",
                            previous=previous, completed=True)
    assert report["verified_round_count"] == 100
    assert report["start_cohort_position"] == 1
    assert report["end_cohort_position"] == 100
    assert report["coverage_status"] == "OBSERVED_100_COMPLETED"
    assert report["relationship_to_previous"]["classification"] == "LOW_TO_HIGH"
    assert report["opening_state"] == "HIGH_DOMINANT"
    assert report["closing_state"] == "LOW_DOMINANT"
    assert report["limitations"]


def test_interrupted_clusters_and_hysteresis_are_symmetric_and_stable():
    symbols = list("HHHHLHHHLLHH")
    report = analyse_window([_dict_round(i, symbol) for i, symbol in enumerate(symbols, 1)],
                            window_number=1, cohort_id="cohort")
    assert report["high_clusters"]["interrupted_cluster_count"] >= 1
    assert report["high_clusters"]["interruption_round_counts"][1] >= 1
    confirmed, candidate, count, changed = update_hysteresis("BALANCED", "BALANCED", "HIGH_DOMINANT", 0)
    assert (confirmed, candidate, count, changed) == ("BALANCED", "HIGH_DOMINANT", 1, False)
    confirmed, candidate, count, changed = update_hysteresis(confirmed, candidate, "HIGH_DOMINANT", count)
    assert (confirmed, count, changed) == ("BALANCED", 2, False)
    confirmed, candidate, count, changed = update_hysteresis(confirmed, candidate, "HIGH_DOMINANT", count)
    assert (confirmed, count, changed) == ("HIGH_DOMINANT", 0, True)


def test_time_coverage_uses_adjacent_positive_gaps_and_never_confirms_time_only():
    now = datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=30 * 20)
    rows = [_dict_round(i, "H", datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=i * 20))
            for i in range(1, 31)]
    rows[3]["event_timestamp"] = rows[2]["event_timestamp"]
    coverage = time_coverage(rows, now=now, collector_state="HEALTHY")
    assert coverage["event_round_cadence"]["sample_count"] == 28
    assert coverage["event_round_cadence"]["timestamp_ties"] == 1
    assert coverage["identity_invented"] is False
    assert coverage["status"] != "CONFIRMED_SOURCE_GAP"
    # A collector outage is reported as an outage, not converted to a round count.
    outage = time_coverage(rows, now=now + timedelta(hours=2), collector_state="ERROR")
    assert outage["status"] == "KNOWN_COLLECTOR_OUTAGE"
    assert outage["possible_missing_round_range"] is None


def test_time_coverage_confirms_only_authoritative_source_sequence_gaps():
    rows = [_dict_round(1, "H"), _dict_round(3, "L")]
    rows[1]["source_sequence"] = 3
    excluded = [{"identity_key": "winner-predict:round-2", "round_id": "round-2",
        "source_name": "winner_predict_postgres", "source_sequence": 2,
        "multiplier_raw": "4.20", "order_quality": "winner_predict_order_uncertain_source_gap"}]
    coverage = time_coverage(rows, now=datetime(2026, 1, 1, tzinfo=timezone.utc),
                             collector_state="HEALTHY", excluded_rounds=excluded)
    assert coverage["status"] == "CONFIRMED_SOURCE_GAP"
    assert coverage["confirmed_source_gaps"][0]["confirmed_missing_source_sequence_count"] == 1
    gap = coverage["confirmed_source_gaps"][0]
    assert gap["observed_but_excluded_rounds"] == [{
        "identity_key": "winner-predict:round-2", "round_id": "round-2",
        "source_sequence": 2, "multiplier": "4.20",
        "order_quality": "winner_predict_order_uncertain_source_gap"}]
    assert gap["unrepresented_analyzed_sequence_count"] == 0


def test_time_coverage_recognizes_phase1_aligned_orbit_order_as_authoritative():
    rows = [_dict_round(1, "H"), _dict_round(3, "L")]
    for row in rows:
        row["order_quality"] = "verified_orbit_delta_aligned_to_phase1"
    rows[0]["source_sequence"] = 101
    rows[1]["source_sequence"] = 103
    coverage = time_coverage(rows, now=rows[-1]["source_observed_at"] + timedelta(seconds=1),
                             collector_state="HEALTHY")
    assert coverage["status"] == "CONFIRMED_SOURCE_GAP"
    assert coverage["confirmed_source_gaps"][0]["confirmed_missing_source_sequence_count"] == 1


def test_prospective_cohort_recovery_reports_exactly_at_100_and_does_not_backfill(tmp_path):
    database_url = f"sqlite:///{tmp_path / 'hybrid.sqlite3'}"
    engine = make_engine(database_url)
    migrate(engine)
    with Session(engine) as session, session.begin():
        cohort = session.get(HybridCohort, 1)
        activation = cohort.activated_at
        old = _canonical(1, "H", activation - timedelta(seconds=5))
        session.add(old)
        process_hybrid_round(session, old)
        assert cohort.last_cohort_position == 0

    for index in range(1, 102):
        with Session(engine) as session, session.begin():
            row = _canonical(index + 1, "H" if index % 2 else "L",
                             datetime.now(timezone.utc) + timedelta(microseconds=index))
            session.add(row)
            session.flush()
            result = process_hybrid_round(session, row)
            if index == 99:
                assert result["window_completed"] is False
            if index == 100:
                assert result["window_completed"] is True
        if index == 100:
            engine.dispose()
            engine = make_engine(database_url)
            migrate(engine)
        if index == 101:
            assert result["window_number"] == 2

    with Session(engine) as session:
        cohort = session.get(HybridCohort, 1)
        windows = session.scalars(select(DominanceWindow).order_by(DominanceWindow.window_number)).all()
        observed = session.scalars(select(HybridCohortRound).order_by(HybridCohortRound.cohort_position)).all()
        hydrated_machine = session.get(HighDominanceSeriesState, cohort.cohort_id)
        assert hydrated_machine is not None
        assert hydrated_machine.payload.get("last_processed_identity") is None
        assert hydrated_machine.payload.get("latest_strategy_eligibility") is None
        assert cohort.last_cohort_position == 101
        assert len(observed) == 101
        assert windows[0].lifecycle_status == "REPORTED"
        assert windows[0].report["verified_round_count"] == 100
        assert windows[0].next_window_id == windows[1].window_id
        assert windows[1].lifecycle_status == "OPEN"
        assert windows[1].start_cohort_position == 101
        # A retry of the latest canonical identity is idempotent.
        latest = session.get(CanonicalRound, observed[-1].identity_key)
        assert process_hybrid_round(session, latest) is None
        assert cohort.last_cohort_position == 101
    engine.dispose()


def test_rolling_high_series_part1_then_part2_pauses_on_low_without_lookahead(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'high-2x-audit.sqlite3'}")
    migrate(engine)
    sequence = ["H", "H", "H", "H", "L",  # 5-round horizon first becomes HIGH
                "H", "H", "H", "L", "H",  # five future rounds; R5 is HIGH
                "L", "L"]                       # continuation exits to LOW on its second round

    with Session(engine) as session:
        cohort = session.get(HybridCohort, 1)
        start = cohort.activated_at + timedelta(seconds=1)
        cohort_id = cohort.cohort_id

    processor = LiveStrategyProcessor(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql("""CREATE TABLE fast_signal_outbox (
            signal_id TEXT, session_id TEXT, target_id TEXT, signal_json TEXT,
            persisted INTEGER NOT NULL DEFAULT 0)""")
    for position, outcome in enumerate(sequence, 1):
        with Session(engine) as session, session.begin():
            inserted_at = start + timedelta(seconds=position)
            row = _canonical(position, outcome, inserted_at)
            identity_key = row.identity_key
            session.add(row)
            session.flush()
        event = {"event_type": "round.observed", "durability": "PENDING",
            "continuity_status": "VERIFIED", "delivery_mode": "LIVE",
            "behaviour_provenance": "LIVE_VERIFIED", "round_id": str(position),
            "round_uid": str(position), "round_index": position, "stream_id": "stream-test",
            "multiplier": "2.10" if outcome == "H" else "1.90" if outcome == "L" else "2.00"}
        if position == 11:
            # Simulate a Rules Engine accepted and locally durable lock for the
            # Part 2 target. Without this evidence the reducer must not invent a
            # win/loss settlement for a merely eligible opportunity.
            signal = {"signal_id": "signal-r11", "session_id": "session-test",
                "target_round_id": "shadow-index:stream-test:11", "mode": "SHADOW",
                "distribution_status": "ACCEPTED"}
            with engine.begin() as connection:
                connection.exec_driver_sql("INSERT INTO fast_signal_outbox VALUES(?,?,?,?,1)",
                    (signal["signal_id"], signal["session_id"], signal["target_round_id"],
                     json.dumps(signal)))
        with Session(engine) as session, session.begin():
            session.add(BehaviourApiRound(
                identity_key=f"stream-test:{position}", stream_id="stream-test",
                round_id=str(position), round_index=position,
                multiplier_raw=str(event["multiplier"]), observed_at=inserted_at,
                platform_timestamp=None, received_at=inserted_at,
                payload=event, status="PROMOTED"))
        result = processor.process(event)
        assert result["status"] == "PROCESSED"
        # The analysis worker remains responsible for descriptive windows. It
        # must be able to process the same round without owning strategy state.
        with Session(engine) as session, session.begin():
            persisted = session.get(CanonicalRound, identity_key)
            process_hybrid_round(session, persisted, live_delivery=True)
            cohort = session.get(HybridCohort, 1)
            machine = session.get(HighDominanceSeriesState, cohort.cohort_id)
            if position == 5:
                assert cohort.confirmed_state != "HIGH_DOMINANT"
                assert machine.payload["state"] == "PART1_COLLECTING"
                assert machine.payload["series"]["trigger_round_id"] == "5"
                assert machine.payload["series"]["part1"]["rounds"] == []
            if position == 10:
                assert machine.payload["state"] == "PART2_ACTIVE"
                assert len(machine.payload["series"]["part1"]["rounds"]) == 5
                assert machine.payload["series"]["part2"]["current_batch"]["rounds"] == []

    with Session(engine) as session:
        machine = session.get(HighDominanceSeriesState, cohort_id).payload
        assert machine["state"] == "PART2_PAUSED_NON_HIGH"
        assert len(machine["series"]["part1"]["rounds"]) == 5
        assert len(machine["series"]["part2"]["current_batch"]["rounds"]) == 1
        assert machine["series"]["part2"]["current_batch"]["rounds"][-1]["result"] == "LOSS"
    with TestClient(create_app(engine)) as client:
        payload = client.get("/api/v1/behaviour/hybrid").json()["high_dominance_2x"]
        assert payload["series_machine"]["state"] == "PART2_PAUSED_NON_HIGH"
        assert payload["series_machine"]["series"]["part2"]["pause_count"] == 1
    engine.dispose()


def test_live_transition_notifications_are_committed_once_without_replay_sound(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'live-transition.sqlite3'}")
    migrate(engine)
    with Session(engine) as session:
        start = session.get(HybridCohort, 1).activated_at + timedelta(seconds=1)
    for position in range(1, 31):
        with Session(engine) as session, session.begin():
            row = _canonical(position, "H", start + timedelta(seconds=position))
            session.add(row)
            session.flush()
            process_hybrid_round(session, row, live_delivery=True)
    with Session(engine) as session:
        notifications = session.scalars(select(CollectorActivity).where(
            CollectorActivity.event_type == "hybrid_live_notification")).all()
        assert any(item.details["event_type"] == "behaviour.high" for item in notifications)
        count = len(notifications)
    with Session(engine) as session, session.begin():
        row = session.get(CanonicalRound, "winner-predict:30")
        assert process_hybrid_round(session, row, live_delivery=True) is None
    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(CollectorActivity).where(
            CollectorActivity.event_type == "hybrid_live_notification")) == count
    engine.dispose()


def test_high_dominance_batch_delete_protects_active_series(tmp_path, monkeypatch):
    monkeypatch.setenv("BEHAVIOUR_CORS_ORIGINS", '["http://testserver"]')
    engine = make_engine(f"sqlite:///{tmp_path / 'high-2x-delete.sqlite3'}")
    migrate(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session, session.begin():
        cohort = session.get(HybridCohort, 1)
        cohort_id = cohort.cohort_id
        common = {
            "cohort_id": cohort_id,
            "decision_identity_key": None,
            "decision_source_name": "winner_predict_postgres",
            "decision_source_sequence": None,
            "trigger_horizon": "10",
            "state_at_decision": "HIGH_DOMINANT",
            "coverage_issue": None,
            "outcomes": [],
            "wins": 0,
            "losses": 0,
            "opened_at": now,
            "updated_at": now,
        }
        session.add_all([
            HighDominance2xBatch(
                batch_id="active-series-part1", series_anchor_position=30,
                batch_number=1, decision_position=30, decision_round_id="30",
                phase="INITIAL", start_position=31, end_position=35,
                state_at_end=None, lifecycle_status="COLLECTING", completed_at=None,
                **common,
            ),
            HighDominance2xBatch(
                batch_id="active-series-part2", series_anchor_position=30,
                batch_number=2, decision_position=35, decision_round_id="35",
                phase="CONTINUATION", start_position=36, end_position=40,
                state_at_end=None, lifecycle_status="COMPLETED", completed_at=now,
                **common,
            ),
            HighDominance2xBatch(
                batch_id="older-finished-series", series_anchor_position=20,
                batch_number=1, decision_position=20, decision_round_id="20",
                phase="INITIAL", start_position=21, end_position=25,
                state_at_end="LOW_DOMINANT", lifecycle_status="STOPPED_LOW_DOMINANT",
                completed_at=now, **common,
            ),
            HighDominance2xBatch(
                batch_id="historic-series-part1", series_anchor_position=5,
                batch_number=1, decision_position=5, decision_round_id="5",
                phase="INITIAL", start_position=6, end_position=10,
                state_at_end="HIGH_DOMINANT", lifecycle_status="COMPLETED",
                completed_at=now, wins=4, losses=1, **{k: v for k, v in common.items() if k not in {"wins", "losses"}},
            ),
            HighDominance2xBatch(
                batch_id="historic-series-part2", series_anchor_position=5,
                batch_number=2, decision_position=10, decision_round_id="10",
                phase="CONTINUATION", start_position=11, end_position=12,
                state_at_end="LOW_DOMINANT", lifecycle_status="STOPPED_LOW_DOMINANT",
                completed_at=now, wins=0, losses=2,
                **{k: v for k, v in common.items() if k not in {"wins", "losses"}},
            ),
        ])

    with TestClient(create_app(engine)) as client:
        base = "/api/v1/behaviour/hybrid/high-dominance-2x/"
        headers = {"Origin": "http://testserver"}
        assert client.delete(base + "historic-series-part1").status_code == 403
        assert client.delete(base + "historic-series-part1",
                             headers={"Origin": "http://evil.example"}).status_code == 403
        assert client.delete(base + "active-series-part2", headers=headers).status_code == 409
        assert client.delete(base + "active-series-part1", headers=headers).status_code == 409
        # Once the live series stops, it remains protected as the latest
        # tracking series until a later HIGH series supersedes it.
        with Session(engine) as session, session.begin():
            active_initial = session.get(HighDominance2xBatch, "active-series-part1")
            active_initial.lifecycle_status = "STOPPED_LOW_DOMINANT"
            active_initial.state_at_end = "LOW_DOMINANT"
            active_initial.completed_at = now
        assert client.delete(base + "active-series-part1", headers=headers).status_code == 409

        deleted_older = client.delete(base + "older-finished-series", headers=headers)
        assert deleted_older.status_code == 200
        assert deleted_older.json()["remaining_summary"]["batch_count"] == 4

        assert client.delete(base + "historic-series-part1", headers=headers).status_code == 409
        deleted = client.delete(base + "historic-series-part2", headers=headers)
        assert deleted.status_code == 200
        assert deleted.json()["deleted"] is True
        assert deleted.json()["remaining_summary"]["batch_count"] == 3
        assert deleted.json()["remaining_summary"]["net_units"] == 3
        deleted = client.delete(base + "historic-series-part1", headers=headers)
        assert deleted.status_code == 200
        assert deleted.json()["remaining_summary"]["batch_count"] == 2
        assert deleted.json()["remaining_summary"]["net_units"] == 0
        assert client.delete(base + "missing-batch-id", headers=headers).status_code == 404
    engine.dispose()


def test_high_dominance_bulk_delete_selected_and_all_keeps_live_series_and_rounds(tmp_path, monkeypatch):
    monkeypatch.setenv("BEHAVIOUR_CORS_ORIGINS", '["http://testserver"]')
    engine = make_engine(f"sqlite:///{tmp_path / 'high-2x-bulk-delete.sqlite3'}")
    migrate(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session, session.begin():
        cohort = session.get(HybridCohort, 1)
        common = {
            "cohort_id": cohort.cohort_id,
            "decision_identity_key": None,
            "decision_source_name": "winner_predict_postgres",
            "decision_source_sequence": None,
            "trigger_horizon": "10",
            "state_at_decision": "HIGH_DOMINANT",
            "coverage_issue": None,
            "outcomes": [],
            "wins": 0,
            "losses": 0,
            "opened_at": now,
            "updated_at": now,
        }
        session.add(_canonical(99, "H", now))
        session.add_all([
            HighDominance2xBatch(
                batch_id="old-part1-with-part2", series_anchor_position=10,
                batch_number=1, decision_position=10, decision_round_id="10",
                phase="INITIAL", start_position=11, end_position=15,
                state_at_end="HIGH_DOMINANT", lifecycle_status="COMPLETED",
                completed_at=now, **common,
            ),
            HighDominance2xBatch(
                batch_id="old-part2-a", series_anchor_position=10,
                batch_number=2, decision_position=15, decision_round_id="15",
                phase="CONTINUATION", start_position=16, end_position=20,
                state_at_end="HIGH_DOMINANT", lifecycle_status="COMPLETED",
                completed_at=now, **common,
            ),
            HighDominance2xBatch(
                batch_id="old-part2-b", series_anchor_position=10,
                batch_number=3, decision_position=20, decision_round_id="20",
                phase="CONTINUATION", start_position=21, end_position=25,
                state_at_end="LOW_DOMINANT", lifecycle_status="STOPPED_LOW_DOMINANT",
                completed_at=now, **common,
            ),
            HighDominance2xBatch(
                batch_id="old-independent-part1", series_anchor_position=15,
                batch_number=1, decision_position=15, decision_round_id="15",
                phase="INITIAL", start_position=16, end_position=20,
                state_at_end="LOW_DOMINANT", lifecycle_status="STOPPED_LOW_DOMINANT",
                completed_at=now, **common,
            ),
            HighDominance2xBatch(
                batch_id="current-live-part1", series_anchor_position=20,
                batch_number=1, decision_position=20, decision_round_id="20",
                phase="INITIAL", start_position=21, end_position=25,
                state_at_end=None, lifecycle_status="COLLECTING", completed_at=None,
                **common,
            ),
        ])

    base = "/api/v1/behaviour/hybrid/high-dominance-2x/"
    headers = {"Origin": "http://testserver"}
    with TestClient(create_app(engine)) as client:
        blocked = client.request("DELETE", base + "bulk-delete", headers=headers,
                                 json={"phase": "INITIAL", "delete_all": True})
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["code"] == "HIGH_DOMINANCE_CONTINUATION_EXISTS"

        selected = client.request("DELETE", base + "bulk-delete", headers=headers,
                                  json={"phase": "CONTINUATION", "batch_ids": ["old-part2-a"]})
        assert selected.status_code == 200
        assert selected.json()["deleted_count"] == 1

        deleted_part2 = client.request("DELETE", base + "bulk-delete", headers=headers,
                                       json={"phase": "CONTINUATION", "delete_all": True})
        assert deleted_part2.status_code == 200
        assert deleted_part2.json()["deleted_count"] == 1

        deleted_selected_part1 = client.request("DELETE", base + "bulk-delete", headers=headers,
                                                json={"phase": "INITIAL", "batch_ids": ["old-independent-part1"]})
        assert deleted_selected_part1.status_code == 200
        assert deleted_selected_part1.json()["deleted_count"] == 1

        deleted_part1 = client.request("DELETE", base + "bulk-delete", headers=headers,
                                       json={"phase": "INITIAL", "delete_all": True})
        assert deleted_part1.status_code == 200
        assert deleted_part1.json()["deleted_count"] == 1

    with Session(engine) as session:
        assert session.scalar(select(func.count()).select_from(CanonicalRound)) == 1
        remaining = session.scalars(select(HighDominance2xBatch)).all()
        assert [row.batch_id for row in remaining] == ["current-live-part1"]
    engine.dispose()


def test_confirmed_high_state_test_checkpoints_forward_and_persists(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'confirmed-high-test.sqlite3'}")
    migrate(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session, session.begin():
        cohort = session.get(HybridCohort, 1)
        cohort_id = cohort.cohort_id
        decision = _canonical(100, "H", now)
        _update_confirmed_high_state_test(
            session, cohort=cohort, row=decision, position=10,
            previous_state="TRANSITION", confirmed_state="HIGH_DOMINANT", now=now)
        cohort.confirmed_state = "HIGH_DOMINANT"
        episode = session.scalar(select(HighDominanceStateTest))
        assert episode.status == "COLLECTING"
        assert episode.decision_position == 10
        assert episode.outcomes == []
        for offset in range(1, 51):
            outcome = "E" if offset == 5 else "H" if offset % 2 else "L"
            row = _canonical(100 + offset, outcome, now + timedelta(seconds=offset))
            _update_confirmed_high_state_test(
                session, cohort=cohort, row=row, position=10 + offset,
                previous_state="HIGH_DOMINANT", confirmed_state="HIGH_DOMINANT",
                now=now + timedelta(seconds=offset))
        assert episode.status == "COMPLETED_50"
        assert len(episode.outcomes) == 50
        assert episode.outcomes[0]["cohort_position"] == 11
        assert episode.checkpoint_results.keys() == {"5", "10", "30", "50"}
        assert episode.checkpoint_results["5"]["exact_count"] == 1
        assert episode.checkpoint_results["5"]["wins"] == 3
        assert episode.checkpoint_results["5"]["losses"] == 2
        assert episode.checkpoint_results["5"]["net_units"] == 1
    # A new session sees the persisted completed episode; processing one state
    # edge while HIGH remains active does not create a duplicate episode.
    with Session(engine) as session, session.begin():
        cohort = session.get(HybridCohort, 1)
        row = _canonical(200, "H", now + timedelta(seconds=60))
        _update_confirmed_high_state_test(
            session, cohort=cohort, row=row, position=61,
            previous_state="HIGH_DOMINANT", confirmed_state="HIGH_DOMINANT",
            now=now + timedelta(seconds=60))
        assert session.scalar(select(func.count()).select_from(HighDominanceStateTest)
                              .where(HighDominanceStateTest.cohort_id == cohort_id)) == 1
    with TestClient(create_app(engine)) as client:
        audit = client.get("/api/v1/behaviour/hybrid").json()["high_dominance_state_test"]
        assert audit["status"] == "WAITING_FOR_NEXT_HIGH_EPISODE"
        assert audit["summary"]["episode_count"] == 1
        assert audit["summary"]["observed_rounds"] == 50
        assert audit["episodes"][0]["checkpoints"]["50"]["status"] == "REACHED"
        assert audit["episodes"][0]["policy_version"] == "confirmed-high-forward-checkpoints-5-10-30-50-v1"
    engine.dispose()


def test_confirmed_high_state_exit_stops_and_next_high_reopens(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'confirmed-high-exit.sqlite3'}")
    migrate(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session, session.begin():
        cohort = session.get(HybridCohort, 1)
        cohort_id = cohort.cohort_id
        _update_confirmed_high_state_test(
            session, cohort=cohort, row=_canonical(1, "H", now), position=1,
            previous_state="BALANCED", confirmed_state="HIGH_DOMINANT", now=now)
        for position in range(2, 6):
            _update_confirmed_high_state_test(
                session, cohort=cohort, row=_canonical(position, "H", now),
                position=position, previous_state="HIGH_DOMINANT",
                confirmed_state="HIGH_DOMINANT", now=now)
        exit_row = _canonical(6, "L", now)
        _update_confirmed_high_state_test(
            session, cohort=cohort, row=exit_row, position=6,
            previous_state="HIGH_DOMINANT", confirmed_state="TRANSITION", now=now)
        first = session.scalar(select(HighDominanceStateTest).where(
            HighDominanceStateTest.cohort_id == cohort_id))
        assert first.status == "STOPPED_STATE_EXIT"
        assert first.exit_state == "TRANSITION"
        assert len(first.outcomes) == 5
        assert first.outcomes[-1]["cohort_position"] == 6
        assert first.checkpoint_results["5"]["wins"] == 4
        assert first.checkpoint_results["5"]["losses"] == 1
        assert "10" not in first.checkpoint_results
        _update_confirmed_high_state_test(
            session, cohort=cohort, row=_canonical(7, "H", now), position=7,
            previous_state="TRANSITION", confirmed_state="HIGH_DOMINANT", now=now)
        _update_confirmed_high_state_test(
            session, cohort=cohort, row=_canonical(8, "H", now), position=8,
            previous_state="HIGH_DOMINANT", confirmed_state="HIGH_DOMINANT", now=now)
        episodes = session.scalars(select(HighDominanceStateTest).where(
            HighDominanceStateTest.cohort_id == cohort_id
        ).order_by(HighDominanceStateTest.decision_position)).all()
        assert len(episodes) == 2
        assert episodes[1].decision_position == 7
        assert len(episodes[1].outcomes) == 1
    engine.dispose()


def test_confirmed_high_state_test_bootstraps_once_without_backfilling(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'confirmed-high-bootstrap.sqlite3'}")
    migrate(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session, session.begin():
        cohort = session.get(HybridCohort, 1)
        _update_confirmed_high_state_test(
            session, cohort=cohort, row=_canonical(20, "H", now), position=20,
            previous_state="HIGH_DOMINANT", confirmed_state="HIGH_DOMINANT", now=now)
        episode = session.scalar(select(HighDominanceStateTest))
        assert episode.decision_position == 20
        assert episode.outcomes == []
        _update_confirmed_high_state_test(
            session, cohort=cohort, row=_canonical(21, "L", now), position=21,
            previous_state="HIGH_DOMINANT", confirmed_state="HIGH_DOMINANT", now=now)
        assert len(episode.outcomes) == 1
        assert episode.outcomes[0]["cohort_position"] == 21
    engine.dispose()


def test_delete_earlier_high_episodes_preserves_latest_and_verified_rounds(tmp_path, monkeypatch):
    monkeypatch.setenv("BEHAVIOUR_CORS_ORIGINS", '["http://testserver"]')
    engine = make_engine(f"sqlite:///{tmp_path / 'earlier-high-delete.sqlite3'}")
    migrate(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session, session.begin():
        cohort = session.get(HybridCohort, 1)
        cohort_id = cohort.cohort_id
        session.add(_canonical(99, "H", now))
        for position, status, wins in [(10, "COMPLETED_50", 3),
                                       (20, "STOPPED_STATE_EXIT", 1),
                                       (30, "COLLECTING", 0)]:
            session.add(HighDominanceStateTest(
                episode_id=f"{cohort.cohort_id}:high-state:{position:010d}",
                cohort_id=cohort.cohort_id, decision_position=position,
                decision_identity_key=None, decision_round_id=str(position),
                status=status, exit_position=None, exit_state=None,
                outcomes=[], checkpoint_results={}, wins=wins, losses=0,
                exact_count=0, policy_version="test-policy", opened_at=now,
                completed_at=None if status == "COLLECTING" else now, updated_at=now))
    endpoint = "/api/v1/behaviour/hybrid/high-dominance-state-test/earlier"
    headers = {"Origin": "http://testserver"}
    first_id = f"{cohort_id}:high-state:0000000010"
    latest_id = f"{cohort_id}:high-state:0000000030"
    with TestClient(create_app(engine)) as client:
        assert client.request("DELETE", endpoint, json={"delete_all": True}).status_code == 403
        assert client.request("DELETE", endpoint, headers=headers,
                              json={"episode_ids": [latest_id]}).status_code == 409
        selected = client.request("DELETE", endpoint, headers=headers,
                                  json={"episode_ids": [first_id]})
        assert selected.status_code == 200
        assert selected.json()["deleted_count"] == 1
        assert selected.json()["remaining_summary"]["episode_count"] == 2
        remaining = client.request("DELETE", endpoint, headers=headers,
                                   json={"delete_all": True})
        assert remaining.status_code == 200
        assert remaining.json()["deleted_count"] == 1
        assert remaining.json()["remaining_summary"]["episode_count"] == 1
    with Session(engine) as session:
        assert session.get(HighDominanceStateTest, latest_id) is not None
        assert session.scalar(select(func.count()).select_from(CanonicalRound)) == 1
    engine.dispose()


def test_hybrid_api_exposes_time_and_round_progress_separately(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'hybrid-api.sqlite3'}")
    migrate(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session, session.begin():
        cohort = session.get(HybridCohort, 1)
        for index in range(1, 23):
            row = _canonical(index, "H" if index % 2 else "L",
                             cohort.activated_at - timedelta(seconds=22 - index))
            event_time = now - timedelta(seconds=(22 - index) * 20)
            row.event_timestamp = event_time
            row.source_observed_at = event_time + timedelta(milliseconds=200)
            row.ingestion_timestamp = event_time + timedelta(milliseconds=400)
            session.add(row)
    with TestClient(create_app(engine)) as client:
        response = client.get("/api/v1/behaviour/hybrid")
        assert response.status_code == 200
        payload = response.json()
        assert payload["available"] is True
        assert payload["cohort"]["status"] == "PROSPECTIVE"
        assert payload["current_window"]["round_progress"]["required"] == 100
        assert payload["current_window"]["time_progress"]["never_completes_observed_window"] is True
        assert payload["rolling"]["policy_version"] == "hybrid-dominance-v1"
        assert payload["cohort"]["last_cohort_position"] == 0
        assert payload["coverage"]["cadence_calibration_round_count"] == 22
        assert payload["coverage"]["delivery_cadence"]["sample_count"] == 21
    engine.dispose()


def test_high_dominance_history_totals_and_phase_pages_exclude_current_series(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'high-history-pages.sqlite3'}")
    migrate(engine)
    now = datetime.now(timezone.utc)
    with Session(engine) as session, session.begin():
        cohort = session.get(HybridCohort, 1)
        for anchor, number, phase, wins, losses in [
            (10, 1, "INITIAL", 3, 2),
            (10, 2, "CONTINUATION", 1, 4),
            (10, 3, "CONTINUATION", 4, 1),
            (20, 1, "INITIAL", 0, 0),  # newest series stays in the live card
        ]:
            session.add(HighDominance2xBatch(
                batch_id=f"{cohort.cohort_id}:history:{anchor}:{number}",
                cohort_id=cohort.cohort_id, series_anchor_position=anchor,
                batch_number=number, decision_position=anchor + number,
                decision_identity_key=None, decision_round_id=str(anchor + number),
                decision_source_name="winner_predict_postgres", decision_source_sequence=None,
                trigger_horizon="10", phase=phase, start_position=anchor + number + 1,
                end_position=None, state_at_decision="HIGH_DOMINANT", state_at_end=None,
                lifecycle_status="COMPLETED", coverage_issue=None, outcomes=[],
                wins=wins, losses=losses, opened_at=now, completed_at=now, updated_at=now,
            ))
        for number in range(2, 37):
            session.add(HighDominance2xBatch(
                batch_id=f"{cohort.cohort_id}:history:20:{number}",
                cohort_id=cohort.cohort_id, series_anchor_position=20,
                batch_number=number, decision_position=20 + number,
                decision_identity_key=None, decision_round_id=str(20 + number),
                decision_source_name="winner_predict_postgres", decision_source_sequence=None,
                trigger_horizon="10", phase="CONTINUATION", start_position=21 + number,
                end_position=None, state_at_decision="HIGH_DOMINANT", state_at_end=None,
                lifecycle_status="COMPLETED", coverage_issue=None, outcomes=[],
                wins=2, losses=3, opened_at=now, completed_at=now, updated_at=now,
            ))
    with TestClient(create_app(engine)) as client:
        overview = client.get("/api/v1/behaviour/hybrid").json()["high_dominance_2x"]
        assert overview["phase_summaries"]["INITIAL"] == {
            "batch_count": 1, "wins": 3, "losses": 2, "net_units": 1}
        assert overview["phase_summaries"]["CONTINUATION"] == {
            "batch_count": 2, "wins": 5, "losses": 5, "net_units": 0}
        assert overview["history_count"] == 3
        assert overview["current_series"]["initial_batch"]["batch_number"] == 1
        assert overview["current_series"]["continuation_batch_count"] == 35
        assert overview["current_series"]["continuation_batches"][0]["batch_number"] == 36
        initial = client.get("/api/v1/behaviour/hybrid/high-dominance-2x/history",
                             params={"phase": "INITIAL", "offset": 0, "limit": 1}).json()
        continuation = client.get("/api/v1/behaviour/hybrid/high-dominance-2x/history",
                                  params={"phase": "CONTINUATION", "offset": 0, "limit": 1}).json()
        older_continuation = client.get("/api/v1/behaviour/hybrid/high-dominance-2x/history",
                                        params={"phase": "CONTINUATION", "offset": 1, "limit": 1}).json()
        assert (initial["total"], initial["items"][0]["wins"]) == (1, 3)
        assert (continuation["total"], continuation["items"][0]["batch_number"],
                continuation["items"][0]["wins"]) == (2, 3, 4)
        assert (older_continuation["items"][0]["batch_number"],
                older_continuation["items"][0]["losses"]) == (2, 4)
    engine.dispose()


def test_phase_nets_plus_current_batch_equal_all_batches_hypothetical_net(tmp_path):
    engine = make_engine(f"sqlite:///{tmp_path / 'high-hypothetical-total.sqlite3'}")
    migrate(engine)
    now = datetime.now(timezone.utc)

    def outcomes(wins, losses, anchor, batch_number):
        result = []
        for index in range(wins + losses):
            is_win = index < wins
            result.append({
                "cohort_position": anchor + batch_number + index + 1,
                "identity_key": f"synthetic:{anchor}:{batch_number}:{index}",
                "round_id": f"{anchor}:{batch_number}:{index}",
                "multiplier": "2.10" if is_win else "1.90",
                "classification": "H" if is_win else "L",
                "result_at_2x": "WIN" if is_win else "LOSS",
            })
        return result

    with Session(engine) as session, session.begin():
        cohort = session.get(HybridCohort, 1)

        def add_batch(anchor, batch_number, phase, wins, losses):
            session.add(HighDominance2xBatch(
                batch_id=f"{cohort.cohort_id}:net-check:{anchor}:{batch_number}",
                cohort_id=cohort.cohort_id, series_anchor_position=anchor,
                batch_number=batch_number, decision_position=anchor,
                decision_identity_key=None, decision_round_id=str(anchor),
                decision_source_name="winner_predict_postgres", decision_source_sequence=None,
                trigger_horizon="10", phase=phase, start_position=anchor + 1,
                end_position=anchor + wins + losses,
                state_at_decision="HIGH_DOMINANT", state_at_end="HIGH_DOMINANT",
                lifecycle_status="COMPLETED", coverage_issue=None,
                outcomes=outcomes(wins, losses, anchor, batch_number),
                wins=wins, losses=losses, opened_at=now, completed_at=now, updated_at=now,
            ))

        remaining_wins, remaining_losses = 67, 58
        for anchor in range(1, 26):
            wins = min(5, remaining_wins)
            losses = 5 - wins
            remaining_wins -= wins
            remaining_losses -= losses
            add_batch(anchor, 1, "INITIAL", wins, losses)
        assert (remaining_wins, remaining_losses) == (0, 0)

        remaining_wins, remaining_losses = 67, 58
        for anchor in range(1, 26):
            for continuation_index in range(2 if anchor <= 6 else 1):
                batch_number = continuation_index + 2
                sample_size = 5 if anchor == 1 and continuation_index == 0 else 4
                wins = min(sample_size, remaining_wins)
                losses = sample_size - wins
                remaining_wins -= wins
                remaining_losses -= losses
                add_batch(anchor, batch_number, "CONTINUATION", wins, losses)
        assert (remaining_wins, remaining_losses) == (0, 0)

        # The latest series is deliberately separate from the historical phase
        # headings: its one initial batch contributes -3u to the all-batch net.
        add_batch(1000, 1, "INITIAL", 1, 4)

    with TestClient(create_app(engine)) as client:
        audit = client.get("/api/v1/behaviour/hybrid").json()["high_dominance_2x"]
        assert audit["phase_summaries"]["INITIAL"] == {
            "batch_count": 25, "wins": 67, "losses": 58, "net_units": 9}
        assert audit["phase_summaries"]["CONTINUATION"] == {
            "batch_count": 31, "wins": 67, "losses": 58, "net_units": 9}
        assert audit["current_series"]["initial_batch"]["net_units"] == -3
        assert audit["summary"]["batch_count"] == 57
        assert audit["summary"]["wins"] == 135
        assert audit["summary"]["losses"] == 120
        assert audit["summary"]["net_units"] == 15
        assert audit["summary"]["net_units"] == sum(
            audit["phase_summaries"][phase]["net_units"]
            for phase in ("INITIAL", "CONTINUATION")
        ) + audit["current_series"]["initial_batch"]["net_units"]
    engine.dispose()
