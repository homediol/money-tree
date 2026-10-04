from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pandas as pd
import pytest
from fastapi import HTTPException

from app.api.readiness import router as readiness_router, status as readiness_http_status
from app.api.ml import train as ml_train
from app.api.models import train_models
from app.services.dataset_service import DatasetService
from app.services.system_readiness import SystemReadiness


@pytest.fixture(autouse=True)
def run_background_snapshot_inline(monkeypatch):
    # These tests exercise readiness state and scheduling. The default thread
    # pool can take minutes to shut down in the test environment.
    async def direct(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", direct)


class Registry:
    def __init__(self, *, deployable=False):
        self.deployable = deployable
        self._training = False
        self.train_calls = 0
        self.last_training_round_index = None
        self.trained_at = None

    def status(self, _dataset):
        return {
            "status": "READY" if self.deployable else "NOT_VALIDATED",
            "model_version": "model-1",
            "deployable": self.deployable,
            "validation_message": "candidate did not beat the frozen baseline",
        }

    def performance(self):
        return {
            "model_version": "model-1",
            "last_training_round_index": self.last_training_round_index,
            "trained_at": self.trained_at,
            "algorithm": "extra_trees",
            "selection_reason": "candidate did not beat the frozen baseline",
            "models": {"extra_trees": {
                "selection_brier_advantage": -0.001,
                "folds_beating_baseline": 0,
                "walk_forward": [{}, {}, {}],
            }},
            "test_metrics": {"accuracy": 0.5, "brier_score": 0.251,
                             "log_loss": 0.694, "roc_auc": 0.5, "pr_auc": 0.49},
            "overfitting_checks": {
                "deployable": self.deployable,
                "folds_beating_baseline": 0,
                "fold_count": 3,
                "best_test_baseline": "base_rate_probability",
                "best_baseline_test_uncertainty": {
                    "point": -0.0002, "ci95": [-0.001, 0.001],
                },
            },
        }

    def train(self, _dataset, progress=None):
        self.train_calls += 1
        self.last_training_round_index = len(_dataset.clean_rounds)
        return SimpleNamespace(
            model_version="trained-1", validated=False,
            selection_reason="still below baseline", message="not deployable",
        )


class Risk:
    def __init__(self, emergency=False, status="WAITING"):
        self.emergency_latched = emergency
        self._status = status

    def status(self, _betting):
        return {"risk_status": self._status, "reason": "fixture risk state"}


def valid_decision(*, expired=False):
    expiry = datetime.now(timezone.utc) + timedelta(seconds=-1 if expired else 60)
    return {
        "decision_id": "d1",
        "status": "READY_FOR_EXECUTION",
        "execution_status": "READY",
        "risk_status": "APPROVED",
        "risk_evaluation": {"approved": True, "approved_bet": 100},
        "expires_at": expiry.isoformat(),
        "source_round_id": "100",
        "target_round_id": "101",
    }


def make_app(*, continuous=20, deployable=False, decision=None,
             mode="OFF", automatic=False, collector_status="HEALTHY",
             emergency=False, health_allowed=True):
    dataset = SimpleNamespace(
        quality={"latest_contiguous_rounds": continuous},
        clean_rounds=pd.DataFrame({"round_id": [str(i) for i in range(1, 151)]}),
    )
    registry = Registry(deployable=deployable)
    betting_status = {"mode": mode, "automatic_enabled": automatic}
    state = SimpleNamespace(
        wp=SimpleNamespace(dataset_service=dataset, model_registry=registry),
        history_collector=SimpleNamespace(status=lambda: {
            "status": collector_status,
            "running": collector_status == "HEALTHY",
            "count": 10137,
            "latest": {"round_id": "100"},
        }),
        betting=SimpleNamespace(status=lambda: dict(betting_status)),
        decision_engine=SimpleNamespace(current=lambda: decision),
        risk=Risk(emergency=emergency, status="APPROVED" if decision else "WAITING"),
        shadow=SimpleNamespace(status=lambda: {"mode": "STOPPED"}),
        system_health=SimpleNamespace(can_bet_now=lambda mode: {
            "allowed": health_allowed,
            "reasons": [] if health_allowed else ["browser:not ready"],
        }),
    )
    return SimpleNamespace(state=state), registry


def test_history_warmup_calculation_and_model_readiness_are_independent():
    app, _ = make_app(continuous=20, deployable=True)
    snapshot = SystemReadiness(app, automatic_training=False).status()
    assert snapshot["history"] == {
        "total_rounds": 10137,
        "continuous_rounds": 20,
        "required_rounds": 100,
        "progress_percentage": 20.0,
        "history_ready": False,
        "status": "WARMING_UP",
    }
    assert snapshot["ml"]["status"] == "DEPLOYABLE"
    assert snapshot["overall"]["status"] == "WARMING_UP"


def timed_app(**options):
    app, registry = make_app(continuous=10, **options)
    now = datetime.now(timezone.utc)
    app.state.wp.dataset_service.clean_rounds = pd.DataFrame([
        {"round_id": str(i), "round_index": i,
         "timestamp": (now - timedelta(seconds=(10-i)*10)).isoformat(), "multiplier": 1.5}
        for i in range(1, 11)
    ])
    app.state.history_collector.status = lambda: {
        "status": options.get("collector_status", "HEALTHY"),
        "running": options.get("collector_status", "HEALTHY") == "HEALTHY",
        "count": 10, "latest": {"round_id": "10"},
    }
    return app, registry


def test_completion_estimate_uses_recent_pace_and_allows_cooldown_to_dominate():
    app, registry = timed_app()
    registry.last_training_round_index = 0
    registry.trained_at = datetime.now(timezone.utc).isoformat()
    result = SystemReadiness(app, required_rounds=20, min_new_rounds=20).snapshot()["completion_estimate"]
    assert result["sample_intervals"] == 9
    assert result["average_round_seconds"] == 10
    assert 99 <= result["warmup_remaining_seconds"] <= 100
    assert 3598 <= result["evaluation_start_remaining_seconds"] <= 3600
    assert result["warmup_expected_at"]


@pytest.mark.parametrize("offline", [False, True])
def test_stale_or_disconnected_collector_has_no_completion_countdown(offline):
    app, _ = timed_app(collector_status="DISCONNECTED" if offline else "HEALTHY")
    if not offline:
        dataset = app.state.wp.dataset_service
        dataset.clean_rounds["timestamp"] = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    result = SystemReadiness(app).snapshot()["completion_estimate"]
    assert result["warmup_remaining_seconds"] is None
    assert result["evaluation_start_expected_at"] is None
    assert result["unavailable_reason"]


def test_training_completion_uses_recorded_duration_and_never_claims_overdue_work_finished():
    app, registry = timed_app()
    original_performance = registry.performance
    registry.performance = lambda: {**original_performance(), "cycle_time_seconds": 120}
    service = SystemReadiness(app)
    service._training["status"] = "EVALUATING"
    service._training_started_monotonic = time.monotonic() - 70
    estimate = service.snapshot()["completion_estimate"]
    assert estimate["training_elapsed_seconds"] == 70
    assert estimate["training_remaining_seconds"] == 50
    assert estimate["evaluation_start_remaining_seconds"] is None
    service._training_started_monotonic = time.monotonic() - 130
    assert service.snapshot()["completion_estimate"]["training_expected_at"] is None


def test_real_history_gap_overrides_older_persisted_continuity():
    app, _ = make_app(continuous=2, deployable=False)
    app.state.wp.collector_state = {"contiguous_rounds": 316, "total_history": 13142}
    snapshot = SystemReadiness(app, automatic_training=False).status()
    assert snapshot["history"]["continuous_rounds"] == 2
    assert snapshot["history"]["progress_percentage"] == 2.0
    assert snapshot["history"]["history_ready"] is False


def test_cached_readiness_is_invalidated_when_history_source_changes(tmp_path):
    raw = tmp_path / "roundhistory.json"
    raw.write_text("[]", encoding="utf-8")
    app, _ = make_app()
    app.state.wp.dataset_service.raw_path = raw
    readiness = SystemReadiness(app, automatic_training=False)

    asyncio.run(readiness.refresh(trigger_training=False))
    assert readiness.cached_status() is not None

    raw.write_text("[{}]", encoding="utf-8")
    assert readiness.cached_status() is None


def test_history_reaches_ready_without_promoting_rejected_model():
    app, _ = make_app(continuous=100, deployable=False, decision=valid_decision(),
                      mode="REAL", automatic=True)
    snapshot = SystemReadiness(app, automatic_training=False).status()
    assert snapshot["history"]["history_ready"] is True
    assert snapshot["history"]["status"] == "READY"
    assert snapshot["history"]["progress_percentage"] == 100
    assert snapshot["ml"]["status"] == "NOT_DEPLOYABLE"
    assert snapshot["ml"]["validation_folds"] == {"passed": 0, "total": 3}
    assert snapshot["overall"]["status"] == "NOT_READY"
    assert "model_not_deployable:NOT_VALIDATED" in snapshot["overall"]["reasons"]


def test_platform_timestamp_gap_does_not_reset_verified_continuity(tmp_path):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [{
        "round_id": str(i), "round_index": i,
        "timestamp": (start + timedelta(seconds=i * 10)).isoformat(),
        "multiplier": 1.5,
        "round_identity_type": "TEST_FIXTURE_SEQUENCE",
        "round_index_source": "TEST_FIXTURE_SEQUENCE",
        "continuity_verified": True, "gap_before": False,
        "continuity_proof": "TEST_FIXTURE_ADJACENT_INDEX",
    } for i in range(1, 106)]
    rows[-1]["timestamp"] = (start + timedelta(seconds=105 * 10 + 180)).isoformat()
    raw = tmp_path / "roundhistory.json"
    raw.write_text(json.dumps(rows), encoding="utf-8")
    dataset = DatasetService(raw, tmp_path / "processed", tmp_path / "features")
    dataset.build_training_dataset(persist=False)
    assert dataset.quality["latest_contiguous_rounds"] == 105
    assert dataset.quality["timestamp_anomalies"] == 1

    app, _ = make_app(continuous=105)
    app.state.wp.dataset_service = dataset
    snapshot = SystemReadiness(app, automatic_training=False).status()
    assert snapshot["history"]["continuous_rounds"] == 105
    assert snapshot["history"]["history_ready"] is True


def test_unverified_latest_overlap_keeps_a_real_continuity_gap(tmp_path):
    start = datetime.now(timezone.utc) - timedelta(seconds=1200)
    rows = [{
        "round_id": str(i), "round_index": i,
        "timestamp": (start + timedelta(seconds=i * 10)).isoformat(),
        "multiplier": 1.5,
        "round_identity_type": "TEST_FIXTURE_SEQUENCE",
        "round_index_source": "TEST_FIXTURE_SEQUENCE",
        "continuity_verified": True, "gap_before": False,
        "continuity_proof": "TEST_FIXTURE_ADJACENT_INDEX",
    } for i in range(1, 106)]
    rows[-1]["gap_before"] = True
    raw = tmp_path / "roundhistory.json"
    raw.write_text(json.dumps(rows), encoding="utf-8")
    dataset = DatasetService(raw, tmp_path / "processed", tmp_path / "features")
    dataset.build_training_dataset(persist=False)
    assert dataset.quality["latest_contiguous_rounds"] == 1


def test_expired_decision_and_off_mode_block_real_execution():
    app, _ = make_app(continuous=100, deployable=True,
                      decision=valid_decision(expired=True), mode="OFF")
    gate = SystemReadiness(app, automatic_training=False).execution_gate()
    assert gate["allowed"] is False
    assert gate["readiness"]["decision"]["status"] == "EXPIRED"
    assert "decision_expired" in gate["reasons"]
    assert "mode_not_real" in gate["reasons"]


def test_all_mandatory_real_conditions_are_required():
    app, _ = make_app(continuous=100, deployable=True, decision=valid_decision(),
                      mode="REAL", automatic=True)
    gate = SystemReadiness(app, automatic_training=False).execution_gate()
    assert gate["allowed"] is True
    assert gate["state"] == "READY"

    app.state.risk.emergency_latched = True
    blocked = SystemReadiness(app, automatic_training=False).execution_gate()
    assert blocked["allowed"] is False
    assert "emergency_stop" in blocked["reasons"]


def test_readiness_websocket_event_emits_when_round_progress_changes():
    events = []

    async def capture(payload):
        events.append(payload)

    app, _ = make_app(continuous=20)
    service = SystemReadiness(app, broadcaster=capture, automatic_training=False)

    async def scenario():
        await service.refresh(trigger_training=False)
        app.state.wp.dataset_service.quality["latest_contiguous_rounds"] = 21
        await service.refresh(trigger_training=False)

    asyncio.run(scenario())
    readiness_events = [event for event in events if event["type"] == "readiness:updated"]
    assert [event["readiness"]["history"]["continuous_rounds"]
            for event in readiness_events] == [20, 21]


def test_http_readiness_uses_cached_snapshot_without_running_training(monkeypatch):
    app, registry = make_app(continuous=100)
    service = SystemReadiness(app, automatic_training=True)
    service._last_snapshot = service.snapshot()
    registry._training = True
    monkeypatch.setattr(service, "snapshot", lambda: (_ for _ in ()).throw(
        AssertionError("HTTP readiness must not compute a fresh snapshot")))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(readiness=service)))
    started = time.perf_counter()
    response = asyncio.run(readiness_http_status(request))
    elapsed = time.perf_counter() - started
    assert elapsed < 0.5
    assert response["readiness"]["history"]["continuous_rounds"] == 100
    assert any(route.path == "/api/readiness" for route in readiness_router.routes)
    assert service._training_task is None


def test_http_readiness_reports_starting_before_first_snapshot():
    app, _ = make_app()
    request = SimpleNamespace(app=SimpleNamespace(
        state=SimpleNamespace(readiness=SystemReadiness(app))))
    response = asyncio.run(readiness_http_status(request))
    assert response.status_code == 503
    assert json.loads(response.body)["status"] == "STARTING"

    service = request.app.state.readiness
    service._last_snapshot = service.snapshot()
    service._last_snapshot_monotonic = time.monotonic() - 16
    stale = asyncio.run(readiness_http_status(request))
    assert stale.status_code == 503
    assert json.loads(stale.body)["status"] == "STALE"


def test_both_training_routes_refuse_early_manual_retraining():
    app, registry = make_app(continuous=100)
    registry.last_training_round_index = 149
    app.state.readiness = SystemReadiness(app, min_new_rounds=250, cooldown_s=0)
    request = SimpleNamespace(app=app)
    for handler in (ml_train, train_models):
        with pytest.raises(HTTPException) as error:
            asyncio.run(handler(request))
        assert error.value.status_code == 409
        assert error.value.detail["reason"] == "evaluation_not_eligible"
        assert "new_processed_rounds:1/250" in error.value.detail["blockers"]
    assert registry.train_calls == 0


def test_evaluation_requires_all_four_gates_and_250_new_rounds(monkeypatch):
    async def direct(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", direct)
    app, registry = make_app(continuous=99)
    registry.last_training_round_index = 0
    registry.trained_at = datetime.now(timezone.utc).isoformat()
    app.state.wp.dataset_service.clean_rounds = pd.DataFrame({
        "round_id": [str(i) for i in range(249)]})
    service = SystemReadiness(app, min_new_rounds=250, cooldown_s=3600)

    async def scenario():
        first = await service.refresh()
        assert first["automatic_training"]["next_evaluation"] == "WAITING"
        assert "new_processed_rounds:249/250" in first["automatic_training"]["blockers"]
        assert "contiguous_rounds:99/100" in first["automatic_training"]["blockers"]
        assert service._training_task is None

        app.state.wp.dataset_service.clean_rounds = pd.DataFrame({
            "round_id": [str(i) for i in range(250)]})
        app.state.wp.dataset_service.quality["latest_contiguous_rounds"] = 100
        second = await service.refresh()
        assert any(reason.startswith("cooldown_remaining:")
                   for reason in second["automatic_training"]["blockers"])
        assert service._training_task is None

        registry.trained_at = (datetime.now(timezone.utc) - timedelta(seconds=3601)).isoformat()
        registry._training = True
        third = await service.refresh()
        assert third["automatic_training"]["training_lock"] == "BUSY"
        assert "training_lock_busy" in third["automatic_training"]["blockers"]
        assert service._training_task is None

        registry._training = False
        fourth = await service.refresh()
        assert fourth["automatic_training"]["next_evaluation"] == "TRAINING"
        await service._training_task
        done = service.status()["automatic_training"]
        assert done["next_evaluation"] == "COMPLETED"
        assert done["new_rounds_since_training"] == 0
    asyncio.run(scenario())
    assert registry.train_calls == 1


def test_automatic_training_runs_once_when_window_first_becomes_ready(monkeypatch):
    events = []

    async def capture(payload):
        events.append(payload)

    async def direct(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", direct)

    app, registry = make_app(continuous=100, deployable=False)
    registry.last_training_round_index = 147
    service = SystemReadiness(app, broadcaster=capture, automatic_training=True,
                              min_new_rounds=3)

    async def scenario():
        await service.refresh()
        assert service._training_task is not None
        await asyncio.wait_for(service._training_task, timeout=3)
        await service.refresh()

    asyncio.run(scenario())
    assert registry.train_calls == 1
    assert service.status()["automatic_training"]["status"] == "IDLE"
    assert any(event["type"] == "model:training_completed" for event in events)


def test_automatic_retraining_waits_for_new_rounds_and_cooldown(monkeypatch):
    async def direct(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", direct)
    app, registry = make_app(continuous=100)
    registry.last_training_round_index = 147
    service = SystemReadiness(app, automatic_training=True, min_new_rounds=3, cooldown_s=3600)

    async def scenario():
        await service.refresh()
        await service._training_task
        app.state.wp.dataset_service.clean_rounds = pd.DataFrame(
            {"round_id": [str(i) for i in range(153)]})
        await service.refresh()
        assert service._training_task.done()
        assert registry.train_calls == 1
        assert service.status()["automatic_training"]["new_rounds_since_training"] == 3
        service._last_attempt_at -= timedelta(seconds=3601)
        await service.refresh()
        await service._training_task
    asyncio.run(scenario())
    assert registry.train_calls == 2


def test_raw_hash_change_without_new_processed_rounds_does_not_retrain(monkeypatch, tmp_path):
    async def direct(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", direct)
    app, registry = make_app(continuous=100)
    registry.last_training_round_index = 147
    raw = tmp_path / "history.json"
    raw.write_text("first", encoding="utf-8")
    app.state.wp.dataset_service.raw_path = raw
    service = SystemReadiness(app, automatic_training=True, min_new_rounds=3, cooldown_s=0)

    async def scenario():
        await service.refresh()
        await service._training_task
        raw.write_text("second version", encoding="utf-8")
        status = await service.refresh()
        assert status["automatic_training"]["dataset_changed_same_size"] is True
        assert status["automatic_training"]["new_rounds_since_training"] == 0
        assert status["automatic_training"]["due"] is False
    asyncio.run(scenario())
    assert registry.train_calls == 1


def test_concurrent_training_does_not_schedule_second_task(monkeypatch):
    async def direct(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", direct)
    app, registry = make_app(continuous=100)
    registry.last_training_round_index = 147
    registry._training = True
    service = SystemReadiness(app, automatic_training=True, min_new_rounds=3)

    async def scenario():
        await service.refresh()
        assert service._training_task is None
        registry._training = False
        await service.refresh()
        task = service._training_task
        await service.refresh()
        assert service._training_task is task
        await task
    asyncio.run(scenario())
    assert registry.train_calls == 1
