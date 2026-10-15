from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from app.database.repository import Repository
from main import (_collector_target_identity, _validate_collector_game_phase,
                  _validate_direct_collector_observations)


def _observation(round_id="direct-1", multiplier=2.35, observed_at="2026-10-06T17:00:00+00:00"):
    return {
        "round_id": round_id,
        "multiplier": multiplier,
        "observed_at": observed_at,
        "platform_round_id": "winner-1",
        "platform_round_index": 1,
        "round_index": 11,
        "continuity_verified": True,
        "gap_before": False,
    }


def test_direct_page_event_waits_for_exact_canonical_round_and_replays_idempotently(tmp_path):
    repository = Repository(tmp_path / "direct-observations.sqlite3")
    repository.init()

    assert repository.stage_collector_round_observations([_observation()]) == 1
    assert repository.stage_collector_round_observations([_observation()]) == 0
    assert repository.pending_collector_round_observations() == []

    with repository.connect() as connection:
        connection.execute(
            "INSERT INTO rounds(round_id,round_index,multiplier,timestamp,target) VALUES(?,?,?,?,?)",
            ("direct-1", 11, 2.35, "2026-10-06T17:00:00+00:00", 1),
        )

    pending = repository.pending_collector_round_observations()
    assert len(pending) == 1
    assert pending[0]["round_id"] == "direct-1"
    assert int(pending[0]["canonical_round_index"]) == 11
    assert float(pending[0]["direct_multiplier"]) == float(pending[0]["canonical_multiplier"])
    assert json.loads(pending[0]["payload"])["platform_round_id"] == "winner-1"

    repository.finish_collector_round_observation("direct-1", "PROCESSED", 11)
    assert repository.pending_collector_round_observations() == []


def test_direct_event_rejects_conflicting_replay_for_same_round_identity(tmp_path):
    repository = Repository(tmp_path / "direct-observation-conflict.sqlite3")
    repository.init()
    repository.stage_collector_round_observations([_observation()])

    with pytest.raises(ValueError, match="collector_round_identity_conflict"):
        repository.stage_collector_round_observations([_observation(multiplier=3.1)])


def test_direct_event_retries_are_backed_off_and_keep_the_observation(tmp_path):
    repository = Repository(tmp_path / "direct-observation-retry.sqlite3")
    repository.init()
    repository.stage_collector_round_observations([_observation()])
    with repository.connect() as connection:
        connection.execute(
            "INSERT INTO rounds(round_id,round_index,multiplier,timestamp,target) VALUES(?,?,?,?,?)",
            ("direct-1", 11, 2.35, "2026-10-06T17:00:00+00:00", 1),
        )

    repository.retry_collector_round_observation("direct-1", "temporary_dispatch_error", 60)
    assert repository.pending_collector_round_observations() == []
    with repository.connect() as connection:
        row = connection.execute(
            "SELECT event_status,attempts,last_error FROM collector_round_observation_events WHERE round_id=?",
            ("direct-1",),
        ).fetchone()
    assert row["event_status"] == "RETRY"
    assert row["attempts"] == 1
    assert row["last_error"] == "temporary_dispatch_error"


def test_direct_event_retry_budget_expires_stale_work_and_preserves_reason(tmp_path):
    repository = Repository(tmp_path / "direct-observation-expiry.sqlite3")
    repository.init()
    repository.stage_collector_round_observations([_observation()])
    with repository.connect() as connection:
        connection.execute(
            "INSERT INTO rounds(round_id,round_index,multiplier,timestamp,target) VALUES(?,?,?,?,?)",
            ("direct-1", 11, 2.35, "2026-10-06T17:00:00+00:00", 1),
        )

    for _ in range(6):
        repository.retry_collector_round_observation("direct-1", "temporary_dispatch_error", 0)
    with repository.connect() as connection:
        row = connection.execute(
            "SELECT event_status,attempts,next_attempt_at,last_error FROM collector_round_observation_events WHERE round_id=?",
            ("direct-1",),
        ).fetchone()
    assert row["event_status"] == "EXPIRED"
    assert row["attempts"] == 6
    assert row["next_attempt_at"] is None
    assert row["last_error"].startswith("RETRY_LIMIT_EXHAUSTED:")


def test_collector_event_payload_preserves_direct_identity_and_timing():
    payload = _validate_direct_collector_observations({
        "source": "WINNER_DIRECT_PAGE_HISTORY", "rounds": [_observation()],
    })
    assert payload[0]["round_id"] == "direct-1"
    assert payload[0]["platform_round_id"] == "winner-1"
    assert payload[0]["observed_at"] == "2026-10-06T17:00:00+00:00"


def _game_phase(observed_at):
    return {
        "phase": "UNKNOWN",
        "event_id": "phase-1",
        "observed_at": observed_at,
        "evidence": {"source": "AVIATOR_EXPLICIT_BETTING_PHASE_DOM",
                     "explicit_window_state": "UNKNOWN"},
        "panel_observations": [
            {"panel_id": panel_id, "state": "BET_AVAILABLE", "observed_at": observed_at,
             "frame_identity": "https://game.example/frame", "confidence": 0.98,
             "displayed_stake": 100, "currency": "BIF"}
            for panel_id in ("PANEL_1", "PANEL_2")
        ],
    }


def test_stale_attached_panel_snapshot_does_not_block_durable_round_handoff():
    stale_at = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    payload = _validate_direct_collector_observations({
        "source": "WINNER_DIRECT_PAGE_HISTORY",
        "rounds": [{**_observation(), "game_phase": _game_phase(stale_at)}],
    })
    assert payload[0]["round_id"] == "direct-1"
    assert payload[0]["game_phase"] is None


def test_stale_panel_snapshot_remains_rejected_on_live_phase_endpoint_validator():
    stale_at = (datetime.now(timezone.utc) - timedelta(minutes=3)).isoformat()
    with pytest.raises(ValueError, match="panel observation is stale"):
        _validate_collector_game_phase(_game_phase(stale_at), require_source=True)


def test_fresh_attached_panel_snapshot_is_preserved_with_round_observation():
    fresh_at = datetime.now(timezone.utc).isoformat()
    payload = _validate_direct_collector_observations({
        "source": "WINNER_DIRECT_PAGE_HISTORY",
        "rounds": [{**_observation(), "observed_at": fresh_at,
                    "game_phase": _game_phase(fresh_at)}],
    })
    assert payload[0]["game_phase"]["observed_at"] == fresh_at
    assert len(payload[0]["game_phase"]["panel_observations"]) == 2


def test_rule_target_identity_is_only_forwarded_with_explicit_collector_proof():
    assert _collector_target_identity({"targetRoundId": "target-12"}) == {
        "target_round_id": "target-12", "target_identity_verified": False,
        "target_started": None, "target_identity_proof": None,
    }
    proof = {"target_round_id": "target-12", "target_not_started": True,
             "proof_method": "platform_target_id_before_start"}
    assert _collector_target_identity({
        "targetRoundId": "target-12", "targetIdentityVerified": True,
        "targetStarted": False, "targetIdentityProof": proof,
    }) == {
        "target_round_id": "target-12", "target_identity_verified": True,
        "target_started": False, "target_identity_proof": proof,
    }


def test_observation_queue_snapshot_handles_text_created_at_without_postgres_timestamp_math():
    now = datetime.now(timezone.utc)

    class Cursor:
        def fetchone(self):
            return {"depth": 2, "expired_jobs": 1,
                    "oldest_job_created_at": (now - timedelta(seconds=12)).isoformat()}

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql):
            assert "MIN(created_at) FILTER (WHERE event_status IN ('PENDING','RETRY'))" in sql
            return Cursor()

    repository = Repository.__new__(Repository)
    repository.database_url = "postgresql://test"
    repository.connect = lambda: Connection()

    snapshot = repository.collector_observation_queue_snapshot()
    assert snapshot["depth"] == 2
    assert snapshot["expired_jobs"] == 1
    assert 11 <= snapshot["oldest_job_age_seconds"] <= 13
