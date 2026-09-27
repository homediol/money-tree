from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.database.repository import Repository


def _repo(path) -> Repository:
    repository = Repository(path)
    repository.init()
    return repository


def _rounds(count: int = 150) -> list[dict]:
    start = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)
    return [
        {
            "round_id": f"platform-{index}",
            "round_index": index,
            "multiplier": 1.25 + (index % 7) / 10,
            "timestamp": (start + timedelta(seconds=index * 8)).isoformat(),
            "target": int(index % 3 == 0),
        }
        for index in range(1, count + 1)
    ]


def test_collector_ml_and_execution_state_survive_three_restarts(tmp_path):
    db_path = tmp_path / "restart.sqlite3"
    repository = _repo(db_path)
    repository.upsert_rounds(_rounds(150))
    first_state = repository.rebuild_collector_state(required_rounds=100, collector_session_id="session-a")
    repository.save_readiness_snapshot({
        "history": {"total_rounds": 150, "continuous_rounds": 150, "required_rounds": 100},
        "ml": {"status": "NOT_DEPLOYABLE", "model_id": "candidate-1"},
        "overall": {"status": "WARMING_UP", "reasons": []},
        "updated_at": "2026-09-26T12:30:00+00:00",
    }, restored=True)
    repository.save_model_candidate({
        "model_version": "candidate-1",
        "algorithm": "extra_trees",
        "validation_folds": [{"fold": 1, "passed": False}],
        "metrics": {"brier_score": 0.24},
        "rejection_reason": "failed_baseline_gate",
        "deployment_status": "NOT_DEPLOYABLE",
        "next_evaluation_checkpoint": 150,
        "updated_at": "2026-09-26T12:30:00+00:00",
    })
    repository.save_application_state("risk_runtime", {
        "selected_profile": "PROFILE_B",
        "emergency_latched": True,
        "last_evaluation": {"risk_status": "BLOCKED"},
    }, "2026-09-26T12:30:00+00:00")
    execution = {
        "execution_id": "exec-1",
        "decision_id": "decision-1",
        "target_round_id": "151",
        "profile": "PROFILE_B",
        "bet_amount": 100,
        "cashout_target": 2.0,
        "status": "PLACED",
        "simulated": False,
        "created_at": "2026-09-26T12:30:00+00:00",
        "updated_at": "2026-09-26T12:30:00+00:00",
        "balance_before": 5000,
    }
    assert repository.create_execution(execution) is True
    assert repository.create_execution({**execution, "execution_id": "exec-duplicate"}) is False
    assert first_state["contiguous_rounds"] == 150

    for restart in range(3):
        restarted = _repo(db_path)
        restarted.upsert_rounds(_rounds(150))
        state = restarted.rebuild_collector_state(required_rounds=100, collector_session_id=f"session-{restart}")
        candidates = restarted.list_model_candidates()
        recovered_execution = restarted.execution_for("decision-1", "151")
        reconciled = restarted.reconcile_incomplete_executions()
        after_reconcile = restarted.execution_for("decision-1", "151")

        assert state["contiguous_rounds"] >= 100
        assert state["total_history"] == 150
        assert len(restarted.load_rounds()) == 150
        assert restarted.load_readiness_snapshot()["restored_from_persistence"] is True
        assert candidates[0]["model_version"] == "candidate-1"
        assert candidates[0]["metrics"]["brier_score"] == 0.24
        assert restarted.load_application_state("risk_runtime")["selected_profile"] == "PROFILE_B"
        assert recovered_execution["execution_id"] == "exec-1"
        assert restarted.create_execution({**execution, "execution_id": f"exec-restart-{restart}"}) is False
        assert reconciled in {0, 1}
        assert after_reconcile["status"] == "UNKNOWN"
