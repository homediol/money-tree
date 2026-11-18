"""Replay the verified Winner round store through Behaviour's saved cursor."""

from datetime import datetime, timezone
from decimal import Decimal
import time

from sqlalchemy import create_engine, select, text
from sqlalchemy.orm import Session
from fastapi.testclient import TestClient

from behaviour_detection.api import create_app
from behaviour_detection.db import make_engine, migrate
from behaviour_detection.live_strategy_processor import LiveStrategyProcessor
from behaviour_detection.models import (BehaviourApiRound, CanonicalRound,
                                        WinnerPredictSyncState)
from behaviour_detection.winner_source import (insert_verified_api_observation,
                                               sync_once)


def test_api_round_inbox_buffers_gap_then_promotes_in_order(tmp_path):
    target = make_engine(f"sqlite:///{tmp_path / 'behaviour-api-inbox.sqlite3'}")
    migrate(target)
    now = datetime.now(timezone.utc).isoformat()

    def event(index, multiplier):
        return {"event_type": "round.observed", "durability": "PENDING",
                "continuity_status": "VERIFIED", "delivery_mode": "LIVE",
                "stream_id": "stream-a", "round_id": f"api-{index}",
                "round_index": index, "multiplier": multiplier,
                "observed_at": now, "platform_timestamp": None}

    assert insert_verified_api_observation(target, event(2, 2.4)) == "STAGED_GAP"
    with Session(target) as session:
        assert session.scalar(select(CanonicalRound.round_id)) is None
        assert session.scalar(select(BehaviourApiRound.status)) == "STAGED"

    assert insert_verified_api_observation(target, event(1, 1.4)) == "PROMOTED"
    with Session(target) as session:
        rows = session.scalars(select(CanonicalRound).order_by(
            CanonicalRound.source_sequence)).all()
        assert [(row.round_id, row.source_sequence) for row in rows] == [
            ("api-1", 1), ("api-2", 2)]
        assert session.scalars(select(BehaviourApiRound.status).order_by(
            BehaviourApiRound.round_index)).all() == ["PROMOTED", "PROMOTED"]
    assert insert_verified_api_observation(target, event(2, 2.4)) == "DUPLICATE"
    with TestClient(create_app(target), client=("127.0.0.1", 12345)) as client:
        checkpoint = client.get("/internal/round-checkpoint", params={"stream_id": "stream-a"})
    assert checkpoint.status_code == 200
    assert checkpoint.json() == {"ok": True, "stream_id": "stream-a",
                                 "round_id": "api-2", "round_index": 2}
    target.dispose()


def test_live_api_promotion_ignores_ahead_of_stream_reporting_mirror(tmp_path):
    target = make_engine(f"sqlite:///{tmp_path / 'behaviour-api-mirror-ahead.sqlite3'}")
    migrate(target)
    now = datetime.now(timezone.utc)
    with Session(target) as session, session.begin():
        session.add(CanonicalRound(identity_key="winner-predict:mirror-50",
            round_id="mirror-50", source_round_index="50", source_sequence=50,
            analysis_position=1, multiplier_raw="2.4", multiplier_value=Decimal("2.4"),
            outcome="H", event_timestamp=None, source_observed_at=now,
            ingestion_timestamp=now, source_name="winner_predict_database",
            timestamp_quality="observation_time_only",
            order_quality="winner_predict_canonical_order_verified",
            import_snapshot_id=None, inserted_at=now))

    event = {"event_type": "round.observed", "durability": "PENDING",
        "continuity_status": "VERIFIED", "delivery_mode": "LIVE",
        "behaviour_provenance": "LIVE_VERIFIED", "stream_id": "mirror-ahead-stream",
        "round_id": "live-1", "round_uid": "live-1", "round_index": 1,
        "canonical_sequence": 1, "multiplier": 2.4, "observed_at": now.isoformat()}
    assert insert_verified_api_observation(target, event) == "PROMOTED"
    processor = LiveStrategyProcessor(target)
    decision = processor.process(event)
    assert decision["status"] == "PROCESSED"
    assert decision["round_id"] == "live-1"
    with Session(target) as session:
        inbox = session.get(BehaviourApiRound, "mirror-ahead-stream:live-1")
        assert inbox.status == "PROMOTED"
        assert session.get(CanonicalRound, "winner-predict:live-1") is None
    target.dispose()


def test_explicit_gap_boundary_promotes_new_segment_without_inventing_rounds(tmp_path):
    target = make_engine(f"sqlite:///{tmp_path / 'behaviour-api-explicit-gap.sqlite3'}")
    migrate(target)
    now = datetime.now(timezone.utc).isoformat()
    gap_event = {"event_type": "round.observed", "durability": "PENDING",
        "continuity_status": "GAP_BEFORE", "delivery_mode": "RECOVERED",
        "behaviour_provenance": "RECOVERY_REPLAY", "stream_id": "gap-stream",
        "round_id": "round-8", "round_uid": "round-8", "round_index": 8,
        "multiplier": 1.5, "observed_at": now}
    assert insert_verified_api_observation(target, gap_event) == "PROMOTED"
    with Session(target) as session:
        row = session.get(BehaviourApiRound, "gap-stream:round-8")
        assert row.status == "PROMOTED"
        assert session.scalar(select(BehaviourApiRound.round_index)) == 8
        assert session.scalar(select(CanonicalRound.round_id)) == "round-8"
    target.dispose()


def test_verified_round_replay_recovers_gap_and_deduplicates(tmp_path):
    source = create_engine(f"sqlite:///{tmp_path / 'winner.sqlite3'}")
    target = make_engine(f"sqlite:///{tmp_path / 'behaviour.sqlite3'}")
    migrate(target)
    now = datetime.now(timezone.utc)
    with source.begin() as connection:
        connection.execute(text("""CREATE TABLE aviator_rounds (
            round_id TEXT UNIQUE, round_index INTEGER UNIQUE, multiplier TEXT,
            platform_timestamp TEXT, observed_at TEXT, stored_at TEXT, created_at TEXT,
            continuity_verified BOOLEAN, gap_before BOOLEAN, identity_confidence TEXT,
            round_identity_type TEXT, round_index_source TEXT, continuity_proof TEXT)"""))

    def append(index, multiplier):
        with source.begin() as connection:
            connection.execute(text("""INSERT INTO aviator_rounds VALUES
                (:round_id,:round_index,:multiplier,NULL,:observed_at,:stored_at,:created_at,
                 1,0,'OVERLAP_VERIFIED_ORDER_ONLY','COLLECTOR_OBSERVATION_HASH',
                 'LOCAL_SEQUENCE','HISTORY_SNAPSHOT_OVERLAP')"""), {
                    "round_id": f"verified-{index}", "round_index": index,
                    "multiplier": multiplier, "observed_at": now.isoformat(),
                    "stored_at": now.isoformat(), "created_at": now.isoformat(),
                })

    append(1, "1.50")
    with Session(target) as session, session.begin():
        session.add(WinnerPredictSyncState(singleton_id=1, last_round_index=1,
            last_round_id="verified-1", anchor_round_index=1,
            anchor_round_id="verified-1", overlap_rounds=1,
            synced_round_count=0, source_latest_round_index=1,
            source_latest_stored_at=now, updated_at=now))
    append(2, "2.10")
    append(3, "2.00")
    assert sync_once(target, source)["inserted"] == 2
    assert sync_once(target, source)["inserted"] == 0
    append(5, "1.20")
    assert sync_once(target, source)["error_code"] == "SOURCE_ROUND_INDEX_GAP"
    append(4, "1.90")
    assert sync_once(target, source)["inserted"] == 2
    with Session(target) as session:
        rows = session.scalars(select(CanonicalRound).order_by(CanonicalRound.analysis_position)).all()
        assert [row.round_id for row in rows] == ["verified-2", "verified-3", "verified-4", "verified-5"]
        assert [row.outcome for row in rows] == ["H", "E", "L", "L"]
        assert [row.source_sequence for row in rows] == [2, 3, 4, 5]
        assert session.get(WinnerPredictSyncState, 1).last_round_index == 5
    source.dispose()
    target.dispose()


def test_verified_round_replay_handles_live_batch_throughput(tmp_path):
    source = create_engine(f"sqlite:///{tmp_path / 'winner-throughput.sqlite3'}")
    target = make_engine(f"sqlite:///{tmp_path / 'behaviour-throughput.sqlite3'}")
    migrate(target)
    now = datetime.now(timezone.utc)
    with source.begin() as connection:
        connection.execute(text("""CREATE TABLE aviator_rounds (
            round_id TEXT UNIQUE, round_index INTEGER UNIQUE, multiplier TEXT,
            platform_timestamp TEXT, observed_at TEXT, stored_at TEXT, created_at TEXT,
            continuity_verified BOOLEAN, gap_before BOOLEAN, identity_confidence TEXT,
            round_identity_type TEXT, round_index_source TEXT, continuity_proof TEXT)"""))
        connection.execute(text("""INSERT INTO aviator_rounds VALUES
            ('throughput-0',0,'1.50',NULL,:observed_at,:stored_at,:created_at,
             1,0,'OVERLAP_VERIFIED_ORDER_ONLY','COLLECTOR_OBSERVATION_HASH',
             'LOCAL_SEQUENCE','HISTORY_SNAPSHOT_OVERLAP')"""), {
                "observed_at": now.isoformat(), "stored_at": now.isoformat(),
                "created_at": now.isoformat(),
            })
        connection.execute(text("""INSERT INTO aviator_rounds VALUES
            (:round_id,:round_index,:multiplier,NULL,:observed_at,:stored_at,:created_at,
             1,0,'OVERLAP_VERIFIED_ORDER_ONLY','COLLECTOR_OBSERVATION_HASH',
             'LOCAL_SEQUENCE','HISTORY_SNAPSHOT_OVERLAP')"""), [
                {"round_id": f"throughput-{index}", "round_index": index,
                 "multiplier": "2.00" if index % 11 == 0 else "1.50" if index % 2 else "2.10",
                 "observed_at": now.isoformat(), "stored_at": now.isoformat(),
                 "created_at": now.isoformat()}
                for index in range(1, 501)
            ])
    with Session(target) as session, session.begin():
        session.add(WinnerPredictSyncState(singleton_id=1, last_round_index=0,
            last_round_id="throughput-0", anchor_round_index=0,
            anchor_round_id="throughput-0", overlap_rounds=100,
            synced_round_count=0, source_latest_round_index=0,
            source_latest_stored_at=now, updated_at=now))

    started = time.perf_counter()
    result = sync_once(target, source)
    elapsed = time.perf_counter() - started
    assert result["inserted"] == 500
    assert result["sync_cursor_round_index"] == 500
    assert elapsed < 15, f"500 verified rounds took {elapsed:.2f}s to replay"
    with Session(target) as session:
        rows = session.scalars(select(CanonicalRound).order_by(
            CanonicalRound.analysis_position)).all()
        assert len(rows) == 500
        assert rows[0].round_id == "throughput-1"
        assert rows[-1].round_id == "throughput-500"
        assert [row.source_sequence for row in rows] == list(range(1, 501))
    source.dispose()
    target.dispose()
