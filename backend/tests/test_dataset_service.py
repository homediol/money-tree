from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import numpy as np
import pandas as pd

from app.api.data import data_quality, data_status, dataset_status, features, latest_features
from app.services.dataset_service import DatasetService, multiplier_bucket
from main import manager, process_history_update


def records(count=130):
    return [
        {
            "round_id": f"r{i}", "round_index": i,
            "timestamp": f"2026-01-01T00:{(i // 60):02d}:{(i % 60):02d}Z",
            "multiplier": float(i),
        }
        for i in range(1, count + 1)
    ]


def service_for(tmp_path, rows):
    tmp_path.mkdir(parents=True, exist_ok=True)
    raw = tmp_path / "roundhistory.json"
    raw.write_text(json.dumps(rows), encoding="utf-8")
    return DatasetService(raw, tmp_path / "processed", tmp_path / "features")


def test_raw_loading_validation_quarantine_dedup_order_and_quality(tmp_path):
    rows = [
        {"round_id": "b", "round_index": 2, "timestamp": "2026-01-01T00:00:02Z", "multiplier": "2.5x"},
        {"round_id": "a", "round_index": 1, "timestamp": "2026-01-01T00:00:01Z", "multiplier": 1.2},
        {"round_id": "a", "round_index": 3, "timestamp": "2026-01-01T00:00:03Z", "multiplier": 8},
        {"round_id": "c", "round_index": 4, "timestamp": "bad", "multiplier": 2},
        {"round_id": "d", "round_index": 5, "timestamp": "2026-01-01T00:00:05Z", "multiplier": "NaN"},
        {"round_id": "e", "round_index": 6, "timestamp": "2026-01-01T00:00:06Z", "multiplier": 0},
        "corrupt",
    ]
    svc = service_for(tmp_path, rows)
    frame, quality, quarantine = svc.load_validate()
    assert frame["round_id"].tolist() == ["a", "b"]
    assert frame["multiplier"].tolist() == [1.2, 2.5]
    assert quality["total_rounds"] == 7
    assert quality["valid_rounds"] == 2
    assert quality["duplicates"] == 1
    assert quality["invalid_rounds"] == 4
    assert quality["count_2x_plus"] == 1
    assert quality["rate_2x_plus"] == 0.5
    assert len(quarantine) == 5


def test_missing_history_is_reported_without_fake_rows(tmp_path):
    svc = DatasetService(tmp_path / "missing.json", tmp_path / "p", tmp_path / "f")
    dataset = svc.build_training_dataset()
    assert dataset.empty
    assert svc.quality["total_rounds"] == 0
    assert svc.quarantine[0]["reason"] == "missing_history"


def test_targets_lags_windows_statistics_thresholds_and_sequences(tmp_path):
    svc = service_for(tmp_path, records())
    dataset = svc.build_training_dataset()
    row = dataset.iloc[0]  # target r101; all features use r1..r100
    assert row["round_id"] == "r101"
    assert row["target_2x"] == 1
    assert [row[f"last_{i}"] for i in range(1, 6)] == [100, 99, 98, 97, 96]
    assert row["mean_last_100"] == 50.5
    assert row["median_last_100"] == 50.5
    assert row["min_last_100"] == 1
    assert row["max_last_100"] == 100
    assert np.isclose(row["std_last_100"], np.std(np.arange(1, 101), ddof=0))
    assert "variance_last_100" not in dataset
    assert "range_last_100" not in dataset
    assert "count_2x_last_10" not in dataset
    assert row["rate_2x_last_10"] == 1
    assert row["rate_eq_10_0_last_100"] == .01
    assert row["sequence_last_10"] == "|".join(["HIGH"] * 10)
    assert multiplier_bucket(1.49) == "LOW"
    assert multiplier_bucket(1.5) == "MEDIUM"
    assert multiplier_bucket(4) == "HIGH"


def test_streaks_are_strictly_prior_to_target(tmp_path):
    rows = records()
    for i in range(95, 101):
        rows[i - 1]["multiplier"] = 1.1
    svc = service_for(tmp_path, rows)
    row = svc.build_training_dataset().iloc[0]
    assert row["round_id"] == "r101"
    assert row["streak_below_1_5"] == 6
    assert row["streak_below_2"] == 6
    assert row["streak_below_3"] == 6


def test_no_future_leakage_when_target_or_later_round_changes(tmp_path):
    base = records()
    changed = records()
    changed[100]["multiplier"] = 1.01  # target r101 changes
    changed[101]["multiplier"] = 9999  # future relative to r101
    svc_a = service_for(tmp_path / "a", base)
    svc_b = service_for(tmp_path / "b", changed)
    a = svc_a.build_training_dataset(persist=False).iloc[0]
    b = svc_b.build_training_dataset(persist=False).iloc[0]
    feature_columns = [column for column in a.index if column not in {"target_2x"}]
    assert a[feature_columns].to_dict() == b[feature_columns].to_dict()
    assert a["target_2x"] == 1 and b["target_2x"] == 0
    assert not any(item.uses_future_data for item in svc_a.metadata)


def test_chronological_split_metadata_persistence_and_incremental_update(tmp_path):
    rows = records(110)
    svc = service_for(tmp_path, rows)
    initial = svc.build_training_dataset()
    train, validation, test = svc.chronological_split(0.6, 0.2)
    assert len(train) + len(validation) + len(test) == len(initial)
    assert train.iloc[-1]["round_index"] < validation.iloc[0]["round_index"] < test.iloc[0]["round_index"]
    assert all(item.uses_future_data is False for item in svc.metadata)
    assert svc.dataset_path.exists() and svc.metadata_path.exists() and svc.quarantine_path.exists()
    rows.append(records(111)[-1])
    svc.raw_path.write_text(json.dumps(rows), encoding="utf-8")
    updated = svc.process_incremental()
    assert len(updated) == len(initial) + 1
    assert updated.iloc[-1]["round_id"] == "r111"
    assert svc.last_mode == "incremental"


def test_corrected_historical_outcome_rebuilds_features(tmp_path):
    rows = records(130)
    svc = service_for(tmp_path, rows)
    first = svc.build_training_dataset(persist=False)
    rows[99]["multiplier"] = 1.01
    svc.raw_path.write_text(json.dumps(rows), encoding="utf-8")
    corrected = svc.process_incremental()
    assert svc.last_mode == "full"
    assert first.iloc[0]["last_1"] == 100
    assert corrected.iloc[0]["last_1"] == 1.01


def test_data_apis_return_service_state(tmp_path):
    svc = service_for(tmp_path, records(105))
    svc.build_training_dataset()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(wp=SimpleNamespace(dataset_service=svc))))
    assert data_quality(request)["valid_rounds"] == 105
    assert data_status(request)["dataset"]["status"] == "READY"
    assert features(request)["features"][0]["uses_future_data"] is False
    assert latest_features(request)["features"]["last_1"] == 105
    assert dataset_status(request)["feature_rows"] == 5


def test_realtime_history_update_emits_data_and_feature_events(monkeypatch):
    emitted = []
    monkeypatch.setattr(manager, "broadcast", lambda payload: _capture(emitted, payload))
    async def direct(function, *args, **kwargs):
        return function(*args, **kwargs)
    monkeypatch.setattr(asyncio, "to_thread", direct)

    dataset = SimpleNamespace(
        quality={"valid_rounds": 101}, status=lambda: {"status": "READY"},
        latest_features=lambda: {"last_1": 2.5},
        clean_rounds=pd.DataFrame([{"round_id": "101"}]),
    )
    wp = SimpleNamespace(
        reload=lambda: None, current_analysis=lambda: {"recent_multipliers": [2.5]},
        rounds=[1], dataset_service=dataset,
        analytics_report_engine=SimpleNamespace(report_progress_snapshot=lambda: {"persisted": True}),
        pattern_report=lambda: {"baseline": {"rate": 0.5}, "patterns": []},
        model_registry=SimpleNamespace(predict_latest=lambda _dataset: {"prediction_id": "p1"},
                                       status=lambda _dataset: {"status": "READY"}),
        build_evidence=lambda _prediction: {"evidence_id": "e1"},
    )
    history = SimpleNamespace(
        status=lambda: {"latest": {"round_id": "101"}, "count": 101}, stats=lambda: {"total": 101},
    )
    async def decide(*args, **kwargs):
        return {"decision_id": "d1", "status": "BLOCKED"}
    app = SimpleNamespace(state=SimpleNamespace(
        wp=wp, history_collector=history,
        betting=SimpleNamespace(status=lambda: {"automatic_enabled": False}),
        risk=SimpleNamespace(emergency_latched=False),
        decision_engine=SimpleNamespace(evaluate=decide),
    ))
    asyncio.run(process_history_update(app))
    types = [event["type"] for event in emitted]
    assert "data:updated" in types
    assert "features:updated" in types
    assert "patterns:updated" in types
    assert "prediction:new" in types
    assert "prediction:evidence_updated" in types
    assert types.index("features:updated") < types.index("patterns:updated") < types.index("prediction:new") < types.index("prediction:evidence_updated")
    assert "decision:updated" in types
    assert types.index("prediction:evidence_updated") < types.index("decision:updated")


async def _capture(target, payload):
    target.append(payload)
