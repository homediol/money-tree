from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.database.repository import Repository
from app.rules_engine import RulesEngine
from app.services.demo_rules_session import DemoRulesSessionManager


def setup_demo(tmp_path, rule_ids=("SOURCE_H_MULT_2_3",), now_provider=None):
    repository = Repository(Path(tmp_path) / "demo.sqlite3")
    repository.init()
    engine = RulesEngine()
    for rule_id in rule_ids:
        engine.configure(rule_id, enabled=True)
    manager = DemoRulesSessionManager(repository, engine, now_provider=now_provider or
                                      (lambda: datetime.now(timezone.utc)))
    session = manager.start(list(rule_ids), execution_panel="PANEL_1")
    return repository, engine, manager, session


def demo_round(index, multiplier, *, continuity="VERIFIED", round_id=None):
    return {
        "environment": "DEMO", "demo_origin": "https://aviator-demo.spribegaming.com",
        "delivery_mode": "DEMO", "round_id": round_id or f"demo-{index}",
        "round_index": index, "multiplier": multiplier,
        "observed_at": datetime.now(timezone.utc).isoformat(),
        "continuity_verified": continuity == "VERIFIED",
        "continuity_status": continuity, "event_type": "round.observed",
        "durability": "IN_MEMORY", "platform_round_id": None,
    }


def test_demo_select_is_persisted_and_dispatched_once_without_live_execution(tmp_path):
    repository, _, manager, session = setup_demo(tmp_path)
    first = demo_round(1, 2.5)
    result = manager.ingest_committed_round(first)
    duplicate = manager.ingest_committed_round(first)

    detail = repository.demo_rule_session(session["session_id"])
    evaluations = repository.demo_session_evaluations(session["session_id"])
    decisions = repository.demo_session_decisions(session["session_id"])
    attempts = repository.demo_execution_attempts(session["session_id"])

    assert result["inserted"] is True
    assert duplicate["inserted"] is False
    assert len(evaluations) == 1 and evaluations[0]["eligible"] is True
    assert len(decisions) == 1 and decisions[0]["action"] == "SELECT"
    assert len(attempts) == 1
    assert attempts[0]["state"] == "WAITING_FOR_WINDOW"
    assert "DEMO_TARGET_PROVIDER_IDENTITY_UNAVAILABLE" in attempts[0]["blockers"]
    assert detail["environment"] == "DEMO"
    assert detail["status"] == "ACTIVE"
    assert detail["real_money_action_submitted"] is False
    assert manager.status()["counts"]["bets_attempted"] == 0
    with repository.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM rounds").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM demo_rounds").fetchone()["n"] == 0


def test_independent_rules_can_produce_one_select_without_round_persistence(tmp_path):
    repository, _, manager, session = setup_demo(
        tmp_path, rule_ids=("SOURCE_H_MULT_2_3", "PATTERN_HH"))
    result = manager.ingest_committed_round(demo_round(1, 2.5))

    decisions = repository.demo_session_decisions(session["session_id"])
    evaluations = repository.demo_session_evaluations(session["session_id"])
    assert result["session_dispatch"] == "PROCESSED"
    assert len(evaluations) == 2
    assert {row["rule_id"] for row in evaluations} == {"SOURCE_H_MULT_2_3", "PATTERN_HH"}
    assert len(decisions) == 1
    assert decisions[0]["action"] == "SELECT"
    assert decisions[0]["true_rule_ids"] == ["SOURCE_H_MULT_2_3"]
    assert manager.round_buffer.snapshot()["count"] == 1
    assert repository.load_rounds() == []
    with repository.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM rounds").fetchone()["n"] == 0
        assert conn.execute("SELECT COUNT(*) AS n FROM demo_rounds").fetchone()["n"] == 0


def test_false_rule_persists_no_select_and_unknown_continuity_blocks(tmp_path):
    repository, _, manager, session = setup_demo(tmp_path)
    manager.ingest_committed_round(demo_round(1, 3.0))
    manager.ingest_committed_round(demo_round(2, 2.5, continuity="UNVERIFIED"))

    decisions = repository.demo_session_decisions(session["session_id"])
    evaluations = repository.demo_session_evaluations(session["session_id"])
    assert [row["action"] for row in decisions] == ["NO_SELECT", "BLOCKED"]
    assert evaluations[0]["eligible"] is False
    assert evaluations[1]["eligible"] is None
    assert evaluations[1]["status"] == "DEMO_ROUND_CONTINUITY_UNKNOWN"
    assert repository.demo_execution_attempts(session["session_id"]) == []


def test_unknown_phase_keeps_demo_attempt_blocked_and_does_not_submit(tmp_path):
    repository, _, manager, session = setup_demo(tmp_path)
    manager.ingest_committed_round(demo_round(1, 2.25))
    manager.observe_game_phase({
        "phase": "UNKNOWN", "round_phase": "UNKNOWN",
        "environment": "DEMO", "observed_at": datetime.now(timezone.utc).isoformat(),
        "evidence": {"explicit_window_state": "UNKNOWN", "countdown_seconds": None,
                     "deadline_at": None},
        "panel_observations": [],
    })
    attempt = repository.demo_execution_attempts(session["session_id"])[0]
    assert attempt["state"] == "WAITING_FOR_WINDOW"
    assert attempt["reason"] == "DEMO_TARGET_PROVIDER_IDENTITY_UNAVAILABLE"
    assert attempt["acceptance_receipt"] is None
    assert attempt["settlement_receipt"] is None
    assert attempt["real_money_action_submitted"] is False
    assert manager.status()["counts"]["bets_attempted"] == 0


def test_demo_restart_enters_recovery_and_requires_explicit_recovery(tmp_path):
    repository, engine, manager, session = setup_demo(tmp_path)
    restarted = DemoRulesSessionManager(repository, engine)
    assert restarted.get(session["session_id"])["status"] == "RECOVERY"
    recovered = restarted.recover(session["session_id"])
    assert recovered["status"] == "ACTIVE"
    stopped = restarted.stop(session["session_id"])
    assert stopped["status"] == "STOP"
    assert {row["event_type"] for row in repository.demo_session_events(session["session_id"])} >= {
        "START", "ACTIVE", "RECOVERY", "STOP",
    }


def test_demo_session_expires_and_cannot_be_recovered(tmp_path):
    clock = [datetime.now(timezone.utc)]
    now_provider = lambda: clock[0]
    repository, engine, manager, session = setup_demo(tmp_path, now_provider=now_provider)
    clock[0] += timedelta(hours=9)
    assert manager.status()["status"] == "EXPIRED"
    with pytest.raises(ValueError, match="DEMO_SESSION_NOT_IN_RECOVERY"):
        manager.recover(session["session_id"])
