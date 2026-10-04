from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

import main as backend_main
from app.api.ml import estimate, latest_prediction
from app.core.config import Settings
from app.ml.model_registry import ModelRegistry
from app.ml.trainer import feature_schema
from main import manager, process_frozen_opportunity_marker, process_history_update


def _ready_collector_status(round_id="r10"):
    return {"collectorRunning": True, "browserConnected": True, "pageConnected": True,
            "frameConnected": True, "loggedIn": True, "authRequired": False,
            "state": "COLLECTING", "health": "HEALTHY", "lastRoundId": round_id,
            "lastSuccessfulCollection": datetime.now(timezone.utc).isoformat()}


def test_json_timestamp_normalizes_database_datetime_values():
    naive = datetime(2026, 10, 4, 11, 0, 0)
    aware = datetime(2026, 10, 4, 14, 0, 0, tzinfo=timezone(timedelta(hours=3)))

    assert backend_main._json_timestamp(naive) == "2026-10-04T11:00:00+00:00"
    assert backend_main._json_timestamp(aware) == "2026-10-04T11:00:00+00:00"
    assert backend_main._json_timestamp("2026-10-04T11:00:00Z") == "2026-10-04T11:00:00Z"


def test_alignment_audit_persists_json_safe_warmup_identity_proof(monkeypatch):
    now = datetime.now(timezone.utc)
    config = {"configuration_hash": "frozen-hash"}

    class Repository:
        @staticmethod
        def latest_round_live_details():
            return {"round_id": "round-19434", "round_index": 19434,
                    "multiplier": 1.25, "stored_at": now - timedelta(seconds=2),
                    "continuity_verified": True, "gap_before": False}

        @staticmethod
        def load_application_state(_key):
            return config

    class Engine:
        @staticmethod
        def _assessment_order_proof_verified(_assessment, _configuration_hash):
            return True

    app = SimpleNamespace(state=SimpleNamespace(
        wp=SimpleNamespace(repository=Repository()),
        frozen_observer_runtime={"alignment_audit_started_at": (now - timedelta(seconds=10)).isoformat(),
                                 "alignment_audit_rounds": []},
        opportunity_v4_engine=Engine(),
    ))
    monkeypatch.setattr(backend_main, "_collector_alignment_status", lambda: {
        "lastRoundId": "round-19434", "collectorRunning": True, "state": "COLLECTING",
        "health": "HEALTHY", "browserConnected": True, "pageConnected": True,
        "frameConnected": True, "loggedIn": True, "authRequired": False,
        "lastSuccessfulCollection": now.isoformat(),
    })
    async def direct_observer_call(_app, function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(backend_main, "_frozen_observer_call", direct_observer_call)
    assessment = {"assessment_id": "assessment-19434", "round_id": "round-19434",
                  "round_index": 19434, "created_at": (now - timedelta(seconds=1)).isoformat(),
                  "model_configuration_hash": "frozen-hash", "assessment_immutable": True,
                  "scorable": False, "failed_gate": "CONTINUITY_WARMUP",
                  "feature_snapshot": {"verified_segment_rounds": 26}}

    result = asyncio.run(backend_main._record_alignment_audit_round(
        app, (19434, "round-19434", now.isoformat(), 1.25), assessment))

    assert result["real_data_alignment"]["consecutive_rounds"] == 1
    assert result["alignment_audit_rounds"][0]["scorable"] is False
    assert result["alignment_audit_rounds"][0]["warmup_rounds"] == 26
    import json
    json.dumps(result)


def _service(*, count=350, age_seconds=20, contiguous=None):
    stamp = datetime.now(timezone.utc) - timedelta(seconds=age_seconds)
    rounds = pd.DataFrame([{"round_id": str(i + 1), "round_index": i + 1,
                            "timestamp": stamp.isoformat(), "timestamp_dt": pd.Timestamp(stamp),
                            "multiplier": 2.5 if i % 2 else 1.2}
                           for i in range(count)])
    return SimpleNamespace(clean_rounds=rounds, dataset=pd.DataFrame({"known_feature": [1.0]}),
                           quality={"latest_contiguous_rounds": count if contiguous is None else contiguous},
                           latest_features=lambda: {"known_feature": 1.0,
                                                    "pattern_streak_count": 4, "pattern_streak_rate": .5,
                                                    "pattern_sequence_2_count": 6, "pattern_sequence_2_rate": .5,
                                                    "pattern_sequence_3_count": 3, "pattern_sequence_3_rate": .5})


def _validated_registry(tmp_path, service):
    registry = ModelRegistry(model_dir=tmp_path / "models", max_feature_age_s=600)
    names, schema_hash, version = feature_schema(service.dataset)

    class StubModel:
        def predict_proba(self, features):
            return np.array([[.4, .6] for _ in features])

    registry._model = StubModel()
    registry._metadata = {"model_version": "fixture-v1", "feature_version": version,
                          "feature_schema_hash": schema_hash, "feature_names": names,
                          "overfitting_checks": {"deployable": True},
                          "test_metrics": {"brier_score": .20, "log_loss": .6},
                          "baselines": {"test": {"base_rate_probability": {"brier_score": .25}}}}
    return registry


def _shape(payload, reason):
    assert {"usable", "prediction", "confidence", "reason", "latest_round_timestamp",
            "history_age", "staleness_threshold"} <= payload.keys()
    assert payload["reason"] == reason
    assert payload["staleness_threshold"] == 600
    assert payload["history_age_unit"] == "seconds"
    if not payload["usable"]:
        assert payload["prediction"] is None


def test_ok_payload_requires_fresh_validated_model(tmp_path):
    service = _service()
    registry = _validated_registry(tmp_path, service)
    payload = registry.prediction_payload(service)
    _shape(payload, "OK")
    assert payload["usable"] is True
    assert payload["prediction"]["probability_2x"] == pytest.approx(.6)
    assert payload["prediction"]["usable"] is True


def test_stale_history_suppresses_all_numeric_predictions_and_warns(tmp_path, caplog):
    service = _service(age_seconds=601)
    registry = _validated_registry(tmp_path, service)
    with caplog.at_level("WARNING", logger="APP.ML"):
        payload = registry.prediction_payload(service)
    _shape(payload, "STALE_HISTORY")
    assert registry.predict_latest(service, allow_stale=True) is None
    fallback = registry.fallback_estimate(service)
    assert fallback["reason"] == "STALE_HISTORY"
    assert fallback["probability_2x"] is None
    assert fallback["informational_frequency_2x"] is None
    assert "STALE_HISTORY" in caplog.text
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        wp=SimpleNamespace(model_registry=registry, dataset_service=service))))
    api_payload = estimate(request)
    assert api_payload["prediction"] is None
    assert api_payload["estimate"]["informational_frequency_2x"] is None
    assert latest_prediction(request)["prediction"] is None


def test_no_model_has_only_labeled_informational_frequency(tmp_path):
    service = _service()
    registry = ModelRegistry(model_dir=tmp_path / "none", max_feature_age_s=600)
    payload = registry.prediction_payload(service)
    _shape(payload, "NO_DEPLOYED_MODEL")
    fallback = registry.fallback_estimate(service)
    assert fallback["probability_2x"] is None
    assert fallback["informational_frequency_2x"] == pytest.approx(.5)
    assert fallback["usable"] is False


def test_insufficient_history_reason_including_recent_gap(tmp_path):
    for service in (_service(count=120), _service(contiguous=20), _service(count=0)):
        registry = _validated_registry(tmp_path, service)
        payload = registry.prediction_payload(service)
        _shape(payload, "INSUFFICIENT_HISTORY")
        assert registry.fallback_estimate(service)["informational_frequency_2x"] is None


def test_ml_max_history_age_env_configuration(monkeypatch):
    monkeypatch.setenv("ML_MAX_HISTORY_AGE", "42")
    assert Settings(_env_file=None).ml_max_history_age == 42


def test_live_downstream_receives_reason_but_no_evidence_or_decision(monkeypatch):
    events = []

    async def capture(payload):
        events.append(payload)

    async def direct(function, *args, **kwargs):
        return function(*args, **kwargs)

    monkeypatch.setattr(manager, "broadcast", capture)
    monkeypatch.setattr(asyncio, "to_thread", direct)
    service = _service(count=1)
    service.status = lambda: {"status": "READY"}
    registry = SimpleNamespace(prediction_payload=lambda _service: {
        "usable": False, "prediction": None, "confidence": 0.0, "reason": "STALE_HISTORY",
        "latest_round_timestamp": "old", "history_age": 1000.0, "staleness_threshold": 600})

    def forbidden(*_args, **_kwargs):
        raise AssertionError("unusable inference must not reach evidence or decision")

    wp = SimpleNamespace(reload=lambda: None, current_analysis=lambda: {"recent_multipliers": [1.2]},
                         rounds=[1], dataset_service=service, model_registry=registry,
                         pattern_report=lambda: {"baseline": {}, "patterns": []}, build_evidence=forbidden)
    history = SimpleNamespace(status=lambda: {"latest": {"round_id": "1"}, "count": 1},
                              stats=lambda: {"total": 1})
    app = SimpleNamespace(state=SimpleNamespace(wp=wp, history_collector=history))
    asyncio.run(process_history_update(app))
    statuses = [event for event in events if event["type"] == "prediction:status"]
    assert len(statuses) == 1 and statuses[0]["reason"] == "STALE_HISTORY"
    assert not any(event["type"] in {"prediction:new", "prediction:evidence_updated", "decision:updated"}
                   for event in events)


def test_frozen_opportunity_marker_assesses_authoritative_latest_round(monkeypatch):
    events = []
    calls = []

    async def capture(payload):
        events.append(payload)

    async def direct(function, *args, **kwargs):
        return function(*args, **kwargs)

    class Repository:
        database_url = "postgresql://configured"

        def load_rounds(self):
            return [{"round_id": "r10", "round_index": 10, "multiplier": 1.4,
                     "continuity_verified": True, "gap_before": False}]

    class Observer:
        def reconcile(self, rounds, target_round_index=None):
            calls.append(("reconcile", rounds.iloc[-1].round_id, target_round_index))

        def observe(self, rounds, collector_latest_round_id):
            calls.append(("observe", rounds.iloc[-1].round_id, collector_latest_round_id))
            return {"round_id": collector_latest_round_id, "scorable": True}

    monkeypatch.setattr(manager, "broadcast", capture)
    monkeypatch.setattr(asyncio, "to_thread", direct)
    monkeypatch.setattr(backend_main, "_collector_alignment_status", _ready_collector_status)
    app = SimpleNamespace(state=SimpleNamespace(
        wp=SimpleNamespace(repository=Repository()), opportunity_v4_engine=Observer()))

    processed = asyncio.run(process_frozen_opportunity_marker(app, (10, "r10", "stamp", 1.4)))

    assert processed is True
    assert calls == [("reconcile", "r10", 10), ("observe", "r10", "r10")]
    assert events == [{"type": "opportunity:v4_assessment", "assessment": {"round_id": "r10", "scorable": True}}]


def test_frozen_opportunity_marker_rejects_stale_snapshot(monkeypatch):
    calls = []

    async def direct(function, *args, **kwargs):
        return function(*args, **kwargs)

    class Repository:
        database_url = "postgresql://configured"

        def load_rounds(self):
            return [{"round_id": "r11", "round_index": 11, "multiplier": 1.4}]

    class Observer:
        def reconcile(self, rounds):
            calls.append("reconcile")

        def observe(self, rounds, collector_latest_round_id):
            calls.append("observe")

    monkeypatch.setattr(asyncio, "to_thread", direct)
    monkeypatch.setattr(backend_main, "_collector_alignment_status", _ready_collector_status)
    app = SimpleNamespace(state=SimpleNamespace(
        wp=SimpleNamespace(repository=Repository()), opportunity_v4_engine=Observer()))

    processed = asyncio.run(process_frozen_opportunity_marker(app, (10, "r10", "stamp", 1.4)))

    assert processed is False
    assert calls == []


def test_frozen_opportunity_marker_is_not_advanced_without_persisted_assessment(monkeypatch):
    async def direct(function, *args, **kwargs):
        return function(*args, **kwargs)

    class Repository:
        database_url = "postgresql://configured"

        def load_rounds(self):
            return [{"round_id": "r10", "round_index": 10, "multiplier": 1.4,
                     "continuity_verified": True, "gap_before": False}]

        def get_v4_assessment(self, _round_id):
            return None

    class Observer:
        def reconcile(self, _rounds, _target_round_index):
            return 0

        def observe(self, _rounds, _collector_latest_round_id):
            return None

    monkeypatch.setattr(asyncio, "to_thread", direct)
    monkeypatch.setattr(backend_main, "_collector_alignment_status", _ready_collector_status)
    app = SimpleNamespace(state=SimpleNamespace(
        wp=SimpleNamespace(repository=Repository()), opportunity_v4_engine=Observer()))

    assert asyncio.run(process_frozen_opportunity_marker(app, (10, "r10", "stamp", 1.4))) is False


def test_frozen_observer_refuses_to_score_when_collector_is_not_live():
    status = _ready_collector_status()
    status["authRequired"] = True
    ready, reason = backend_main._collector_marker_readiness(status, (10, "r10"))
    assert ready is False
    assert reason == "collector authentication is not verified"
