from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pytest

from app.api.ml import latest_prediction, metrics, model, recent_predictions, status
from app.database.repository import Repository
from app.ml.model_registry import ModelRegistry
from app.ml.trainer import TARGET_COLUMN, ModelTrainer, evaluate_probabilities, feature_schema
from app.services.dataset_service import DatasetService


def raw_rows(count=520, *, stale=False):
    start = datetime(2020, 1, 1, tzinfo=timezone.utc) if stale else datetime.now(timezone.utc) - timedelta(seconds=count)
    values = []
    for index in range(count):
        previous = values[-1] if values else 1.1
        high = (index * 17) % 11 < (7 if previous < 1.5 else 4)
        values.append(2.5 + (index % 3) if high else 1.05 + (index % 4) / 10)
    return [{"round_id": str(i + 1), "round_index": i + 1,
             "timestamp": (start + timedelta(seconds=i)).isoformat(), "multiplier": value}
            for i, value in enumerate(values)]


@pytest.fixture(scope="module")
def trained(tmp_path_factory):
    root = tmp_path_factory.mktemp("ml")
    raw = root / "roundhistory.json"; raw.write_text(json.dumps(raw_rows()), encoding="utf-8")
    dataset = DatasetService(raw, root / "processed", root / "features"); dataset.build_training_dataset()
    repository = Repository(root / "ml.sqlite3"); repository.init()
    registry = ModelRegistry(2.0, root / "models", repository, max_feature_age_s=3600)
    result = registry.train(dataset)
    return root, dataset, repository, registry, result


def test_dataset_target_schema_order_and_no_leakage(trained):
    _, dataset, _, _, _ = trained
    assert TARGET_COLUMN in dataset.dataset
    assert dataset.dataset["round_index"].is_monotonic_increasing
    names, digest, version = feature_schema(dataset.dataset)
    assert names == [column for column in dataset.dataset.columns if column not in {"round_id", "round_index", "timestamp", "target_2x", "sequence_last_10"}]
    assert "multiplier" not in names and "target_2x" not in names
    assert len(digest) == 64 and version.startswith("part4-")
    target_row = dataset.dataset.iloc[0]
    actual = dataset.clean_rounds.set_index("round_id").loc[str(target_row.round_id), "multiplier"]
    assert target_row.target_2x == int(actual >= 2)


def test_chronological_split_and_class_distribution(trained):
    _, dataset, _, _, result = trained
    train, validation, test = dataset.chronological_split()
    assert train.iloc[-1].round_index < validation.iloc[0].round_index < test.iloc[0].round_index
    assert result.splits == {"train": int(len(dataset.dataset) * .70),
                             "validation": int(len(dataset.dataset) * .85) - int(len(dataset.dataset) * .70),
                             "test": len(dataset.dataset) - int(len(dataset.dataset) * .85)}
    distribution = result.class_distribution
    assert distribution["positive"] + distribution["negative"] == distribution["total"]
    assert distribution["positive_rate"] + distribution["negative_rate"] == pytest.approx(1)


def test_baseline_evaluation_and_probability_metrics():
    y = np.array([0, 1, 0, 1])
    metrics_value = evaluate_probabilities(y, np.array([.1, .8, .2, .7]))
    assert metrics_value["accuracy"] == 1
    assert 0 <= metrics_value["brier_score"] <= 1
    assert {"precision", "recall", "f1", "roc_auc", "pr_auc", "log_loss", "confusion_matrix"} <= metrics_value.keys()


def test_training_pipeline_candidates_metrics_and_determinism(trained):
    _, dataset, _, _, result = trained
    assert result.status == "READY" and result.validated
    assert set(result.models) == {"logistic_regression", "random_forest", "gradient_boosting"}
    assert result.algorithm in result.models
    assert result.validation_metrics == result.models[result.algorithm]["validation"]
    assert result.test_metrics["tp"] + result.test_metrics["fp"] + result.test_metrics["fn"] + result.test_metrics["tn"] == result.splits["test"]
    second = ModelTrainer().train_validate(dataset.dataset)
    assert second.algorithm == result.algorithm
    assert second.validation_metrics["brier_score"] == pytest.approx(result.validation_metrics["brier_score"])


def test_model_version_save_load_and_metadata(trained):
    root, dataset, repository, registry, result = trained
    assert (root / "models" / "active.json").exists()
    assert (root / "models" / result.model_version / "model.joblib").exists()
    loaded = ModelRegistry(2.0, root / "models", repository, max_feature_age_s=3600)
    assert loaded.status(dataset)["status"] == "READY"
    info = loaded.model_info()
    assert info["model_version"] == result.model_version
    assert info["feature_names"] == result.feature_names
    assert info["target_definition"] == "next_round_ge_2x"


def test_probability_inference_persistence_and_range(trained):
    _, dataset, repository, registry, result = trained
    # Synthetic fixture has a real learnable relationship and is deployable.
    assert registry.status(dataset)["deployable"] is True
    prediction = registry.predict_latest(dataset)
    assert prediction and 0 <= prediction["probability_2x"] <= 1
    assert prediction["model_version"] == result.model_version
    assert prediction["feature_version"] == result.feature_version
    assert prediction["source_round_id"] == str(dataset.clean_rounds.iloc[-1].round_id)
    assert repository.latest_ml_prediction()["prediction_id"] == prediction["prediction_id"]


def test_incompatible_feature_version_and_stale_rejection(trained):
    _, dataset, _, registry, _ = trained
    original = dataset.dataset
    dataset.dataset = original.assign(unexpected_numeric_feature=1.0)
    assert registry.status(dataset)["status"] == "INCOMPATIBLE"
    dataset.dataset = original
    registry.max_feature_age_s = -1
    assert registry.status(dataset)["status"] == "STALE"
    assert registry.predict_latest(dataset) is None
    registry.max_feature_age_s = 3600


def test_below_baseline_model_is_not_used_for_live_inference(trained):
    _, dataset, _, registry, _ = trained
    original = registry._metadata["overfitting_checks"]["deployable"]
    registry._metadata["overfitting_checks"]["deployable"] = False
    try:
        assert registry.status(dataset)["quality_state"] == "BELOW_BASELINE"
        assert registry.predict_latest(dataset) is None
    finally:
        registry._metadata["overfitting_checks"]["deployable"] = original


def test_ml_api_responses(trained):
    _, dataset, _, registry, _ = trained
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(wp=SimpleNamespace(model_registry=registry, dataset_service=dataset))))
    assert status(request)["status"] == "READY"
    assert model(request)["algorithm"] in ModelTrainer.factories()
    assert metrics(request)["test_metrics"]["brier_score"] >= 0
    assert latest_prediction(request)["prediction"] is not None
    assert recent_predictions(request, 5)["count"] >= 1
