from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pandas as pd
import pytest
from types import SimpleNamespace

from app.api import evidence as evidence_api
from app.database.repository import Repository
from app.services.evidence_engine import (
    EvidenceEngine, agreement_state, confidence_label, evidence_strength, normalized_stability,
)
from app.services.pattern_engine import wilson_interval


class Registry:
    def __init__(self, deployable=True, brier=.22, baseline=.25, ece=.04):
        self.metadata = {"model_version": "model-v1", "algorithm": "logistic_regression",
                         "test_metrics": {"brier_score": brier, "log_loss": .65},
                         "baselines": {"historical_probability": {"test_metrics": {"brier_score": baseline}}},
                         "calibration": {"test_ece": ece},
                         "walk_forward": {"mean_brier_score": brier},
                         "overfitting_checks": {"deployable": deployable}}

    def metadata_for_version(self, version):
        return self.metadata if version == "model-v1" else {}


class Dataset:
    def __init__(self, values):
        now = datetime.now(timezone.utc)
        self.clean_rounds = pd.DataFrame([{
            "round_id": f"r{i}", "round_index": i, "multiplier": value,
            "timestamp": (now - timedelta(seconds=len(values) - i)).isoformat(),
            "timestamp_dt": pd.Timestamp(now - timedelta(seconds=len(values) - i)),
        } for i, value in enumerate(values)])

    def features_after_round(self, source):
        index = int(source[1:])
        values = self.clean_rounds.iloc[:index + 1].multiplier.tolist()
        return {"last_1": values[-1], "last_2": values[-2], "last_3": values[-3],
                "last_4": values[-4], "last_5": values[-5], "streak_below_2": 0,
                "rate_2x_last_50": sum(v >= 2 for v in values[-50:]) / min(50, len(values)),
                "mean_last_25": sum(values[-25:]) / min(25, len(values)), "std_last_25": 0.5}


def prediction(source, probability=.75):
    return {"prediction_id": "prediction-1", "source_round_id": source,
            "target": "next_round_ge_2x", "probability_2x": probability, "predicted_class": 1,
            "model_version": "model-v1", "feature_version": "features-v1",
            "created_at": datetime.now(timezone.utc).isoformat(), "fresh": True}


@pytest.fixture
def repository(tmp_path):
    repo = Repository(tmp_path / "evidence.sqlite3")
    repo.init()
    return repo


def test_probability_is_not_confidence_and_small_samples_are_protected():
    ci = wilson_interval(3, 4)
    assert evidence_strength(4, 30, ci, "INSUFFICIENT_DATA") == "VERY_WEAK"
    assert confidence_label(19.99) == "VERY_LOW"
    assert .70 < ci["lower"] or ci["lower"] < .70  # estimate and uncertainty are distinct
    assert ci["lower"] < .75 < ci["upper"]


def test_wilson_interval_known_bounds():
    ci = wilson_interval(50, 100)
    assert ci["lower"] == pytest.approx(.4038, abs=.001)
    assert ci["upper"] == pytest.approx(.5962, abs=.001)
    assert ci["n"] == 100


def test_agreement_and_stability_classification():
    assert agreement_state(.52, .51, .50, True) == "STRONG_AGREEMENT"
    assert agreement_state(.65, .35, .50, True) == "HIGH_DISAGREEMENT"
    assert agreement_state(.75, .75, .50, False) == "INSUFFICIENT_DATA"
    assert normalized_stability("STABLE", 40, 30) == "STABLE"
    assert normalized_stability("VARIABLE", 40, 30) == "UNSTABLE"
    assert normalized_stability("STABLE", 4, 30) == "INSUFFICIENT_DATA"


def test_real_snapshot_has_recent_baselines_refs_and_distinct_confidence(repository):
    values = [1.2, 2.2, 1.4, 3.0, 1.1] * 30
    dataset = Dataset(values)
    snapshot = EvidenceEngine(repository, Registry(), min_sample_size=10).build(prediction("r149", .75), dataset)
    assert snapshot["ml_probability"] == .75
    assert snapshot["confidence"] in {"VERY_LOW", "LOW", "MEDIUM", "HIGH", "VERY_HIGH"}
    assert snapshot["baselines"]["recent_50"]["sample_size"] == 50
    assert snapshot["baselines"]["recent_250"]["sample_size"] == 150
    assert snapshot["baselines"]["historical"]["rate"] == pytest.approx(.4)
    assert all(item["evidence_ref"] in snapshot["evidence_index"] for item in snapshot["explanations"])


def test_model_and_data_quality_reduce_confidence(repository):
    values = [1.2, 2.2, 1.4, 3.0, 1.1] * 40
    good = EvidenceEngine(repository, Registry(True, .18, .25, .02), min_sample_size=10)
    first = good.build(prediction("r199", .55), Dataset(values))
    repo2 = Repository(repository.database_path.parent / "weak.sqlite3"); repo2.init()
    stale_prediction = {**prediction("r199", .55), "prediction_id": "weak", "fresh": False,
                        "created_at": (datetime.now(timezone.utc) + timedelta(seconds=1000)).isoformat()}
    weak = EvidenceEngine(repo2, Registry(False, .30, .25, .20), min_sample_size=10)
    second = weak.build(stale_prediction, Dataset(values))
    assert first["confidence_score"] > second["confidence_score"]
    assert second["model_quality"]["quality_state"] == "BELOW_BASELINE_OR_UNVALIDATED"
    assert second["data_quality"]["fresh"] is False
    assert second["confidence"] in {"VERY_LOW", "LOW"}


def test_no_future_leakage_and_snapshot_immutability(repository):
    initial = Dataset([1.1, 2.1] * 60)
    engine = EvidenceEngine(repository, Registry(), min_sample_size=10)
    pred = prediction("r99", .51)
    before = engine.build(pred, initial)
    expanded = Dataset([1.1, 2.1] * 60 + [100.0] * 50)
    after = engine.build(pred, expanded)
    assert after == before
    assert before["history_count_at_cutoff"] == 100
    assert before["baselines"]["historical"]["rate"] == .5
    assert before["immutable"] is True


def test_missing_prediction_or_source_does_not_fabricate(repository):
    engine = EvidenceEngine(repository, Registry())
    assert engine.build(None, Dataset([1.2] * 100)) is None
    assert engine.build(prediction("missing"), Dataset([1.2] * 100)) is None


def test_repository_snapshot_insert_is_immutable(repository):
    base = {"evidence_id": "e1", "prediction_id": "p1", "source_round_id": "r1",
            "calculated_at": "2026-01-01T00:00:00Z", "confidence": "LOW"}
    assert repository.save_evidence_snapshot(base)["confidence"] == "LOW"
    changed = {**base, "evidence_id": "e2", "confidence": "VERY_HIGH"}
    assert repository.save_evidence_snapshot(changed)["confidence"] == "LOW"
    assert repository.latest_evidence()["evidence_id"] == "e1"


def test_evidence_api_views_return_persisted_calculations(repository):
    snapshot = {"evidence_id": "e1", "prediction_id": "p1", "source_round_id": "r1",
                "calculated_at": "2026-01-01T00:00:00Z", "pattern": {"sample_size": 40},
                "stability": "STABLE", "model_quality": {"brier_score": .2},
                "baselines": {"historical": {"rate": .5}}}
    repository.save_evidence_snapshot(snapshot)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
        wp=SimpleNamespace(repository=repository))))
    assert evidence_api.current(request)["evidence"]["prediction_id"] == "p1"
    assert evidence_api.recent(request, 10)["count"] == 1
    assert evidence_api.patterns(request)["pattern"]["sample_size"] == 40
    assert evidence_api.stability(request)["stability"] == "STABLE"
    assert evidence_api.model_quality(request)["model_quality"]["brier_score"] == .2
    assert evidence_api.baselines(request)["baselines"]["historical"]["rate"] == .5
