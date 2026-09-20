from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.api.ml import estimate, latest_prediction
from app.core.config import Settings
from app.ml.model_registry import ModelRegistry
from app.ml.trainer import feature_schema
from main import manager, process_history_update


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
