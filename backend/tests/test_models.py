from __future__ import annotations

import json
import hashlib
from threading import Event, Thread
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from app.api.ml import estimate, latest_prediction, metrics, model, recent_predictions, status
from app.database.repository import Repository
from app.ml.model_registry import ModelRegistry
from app.ml.trainer import TARGET_COLUMN, ModelTrainer, block_bootstrap_brier_advantage, causal_frequency, eligible_round_ids, evaluate_probabilities, feature_schema
from app.services.dataset_service import DatasetService


def raw_rows(count=520, *, stale=False):
    start = datetime(2020, 1, 1, tzinfo=timezone.utc) if stale else datetime.now(timezone.utc) - timedelta(seconds=count)
    values = []
    for index in range(count):
        previous = values[-1] if values else 1.1
        high = (index * 17) % 11 < (7 if previous < 1.5 else 4)
        values.append(2.5 + (index % 3) if high else 1.05 + (index % 4) / 10)
    return [{"round_id": str(i + 1), "round_index": i + 1,
             "timestamp": (start + timedelta(seconds=i)).isoformat(), "multiplier": value,
             "round_identity_type": "TEST_FIXTURE_SEQUENCE",
             "round_index_source": "TEST_FIXTURE_SEQUENCE",
             "continuity_verified": True, "gap_before": False,
             "continuity_proof": "TEST_FIXTURE_ADJACENT_INDEX"}
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
    assert {"logistic_regression", "random_forest", "extra_trees", "gradient_boosting"} <= set(result.models)
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
    assert info["source_sha256"] == hashlib.sha256((root / "roundhistory.json").read_bytes()).hexdigest()


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


def test_recent_collection_gap_blocks_validated_model_inference(trained):
    _, dataset, _, registry, _ = trained
    previous = dataset.quality["latest_contiguous_rounds"]
    try:
        dataset.quality["latest_contiguous_rounds"] = 20
        assert registry.status(dataset)["status"] == "INSUFFICIENT_RECENT_HISTORY"
        assert registry.status(dataset)["deployable"] is False
        assert registry.predict_latest(dataset) is None
    finally:
        dataset.quality["latest_contiguous_rounds"] = previous


def test_below_baseline_model_is_not_used_for_live_inference(trained):
    _, dataset, _, registry, _ = trained
    original = registry._metadata["overfitting_checks"]["deployable"]
    registry._metadata["overfitting_checks"]["deployable"] = False
    try:
        assert registry.status(dataset)["quality_state"] == "BELOW_BASELINE"
        assert registry.predict_latest(dataset) is None
        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(wp=SimpleNamespace(model_registry=registry, dataset_service=dataset))))
        fallback = estimate(request)
        assert fallback["status"] == "INFORMATIONAL_FALLBACK"
        assert fallback["estimate"]["usable"] is False
    finally:
        registry._metadata["overfitting_checks"]["deployable"] = original


def test_inference_failure_is_safe_and_does_not_create_prediction(trained):
    _, dataset, _, registry, _ = trained
    original, error = registry._model, registry._error

    class FailedEstimator:
        def predict_proba(self, _features):
            raise RuntimeError("unavailable")

    try:
        registry._model = FailedEstimator()
        assert registry.predict_latest(dataset) is None
        assert registry.status(dataset)["status"] == "ERROR"
        assert registry.fallback_estimate(dataset)["usable"] is False
    finally:
        registry._model, registry._error = original, error


def test_ml_api_responses(trained):
    _, dataset, _, registry, _ = trained
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(wp=SimpleNamespace(model_registry=registry, dataset_service=dataset))))
    assert status(request)["status"] == "READY"
    assert model(request)["algorithm"] in ModelTrainer.factories()
    assert metrics(request)["test_metrics"]["brier_score"] >= 0
    assert latest_prediction(request)["prediction"] is not None
    assert recent_predictions(request, 5)["count"] >= 1


def test_restart_loads_artifact_independently_of_working_directory(trained, tmp_path, monkeypatch):
    root, dataset, _, registry, _ = trained
    monkeypatch.chdir(tmp_path)
    loaded = ModelRegistry(model_dir=root / "models", max_feature_age_s=3600)
    assert loaded.status(dataset)["last_error"] is None
    assert loaded.predict_latest(dataset)["probability_2x"] == pytest.approx(
        registry.predict_latest(dataset)["probability_2x"])


def test_signal_uses_persisted_registry_and_training_features(trained):
    from app.services.signal_engine import SignalEngine
    root, dataset, _, registry, _ = trained
    loaded = ModelRegistry(model_dir=root / "models", max_feature_age_s=3600)
    analysis = SignalEngine(2, 10, .6, .68, loaded).current_analysis(dataset.clean_rounds, dataset)
    assert analysis["ml_estimate"]["validated"] is True
    assert analysis["ml_estimate"]["probability"] == pytest.approx(
        registry.predict_latest(dataset)["probability_2x"])


def test_legacy_predictor_rejects_training_success_without_quality_validation():
    from app.ml.predictor import MLPredictor
    result = SimpleNamespace(validated=True, ensemble={"validated": False})
    assert MLPredictor().predict(result, None)["probability"] is None


def test_rejected_candidate_is_persisted_for_diagnostics(trained, tmp_path, monkeypatch):
    from dataclasses import replace
    _, dataset, _, _, result = trained
    rejected = replace(result, validated=False, status="NOT_VALIDATED",
                       ensemble={**result.ensemble, "validated": False},
                       overfitting_checks={**result.overfitting_checks, "deployable": False})
    rejected._model = result._model
    registry = ModelRegistry(model_dir=tmp_path / "models")
    monkeypatch.setattr(registry.trainer, "train_validate", lambda _: rejected)
    registry.train(dataset)
    loaded = ModelRegistry(model_dir=tmp_path / "models", max_feature_age_s=3600)
    assert loaded.performance()["validated"] is False
    assert loaded.status(dataset)["deployable"] is False
    assert loaded.predict_latest(dataset) is None


def test_validation_flag_requires_both_held_out_baselines(trained):
    result = trained[-1]
    selection = result.models[result.algorithm]["selection"]["brier_score"]
    selection_baseline = min(x["brier_score"] for x in result.baselines["selection"].values() if isinstance(x, dict) and "brier_score" in x)
    test_baseline = min(x["brier_score"] for x in result.baselines["test"].values() if isinstance(x, dict) and "brier_score" in x)
    expected = (selection_baseline - selection >= .001
                and result.overfitting_checks["folds_beating_baseline"] >= 2
                and test_baseline - result.test_metrics["brier_score"] >= .001
                and result.overfitting_checks["test_brier_advantage_uncertainty"]["ci95"][0] > 0
                and result.overfitting_checks["best_baseline_test_uncertainty"]["ci95"][0] > 0)
    assert result.validated == result.ensemble["validated"] == result.overfitting_checks["deployable"] == expected


def test_selection_precedes_test_and_stability_spans_disjoint_periods(trained):
    result = trained[-1]
    qualified = result.overfitting_checks["qualified_candidates_before_test"]
    assert result.algorithm in qualified
    assert all(result.models[name]["passes_selection_gate"] for name in qualified)
    assert all(fold["evaluation_end"] <= result.splits["train"] for fold in result.walk_forward)
    periods = result.overfitting_checks["test_periods"]
    assert len(periods) == 3
    assert sum(period["n"] for period in periods) == result.splits["test"]
    assert all(periods[i]["end"] < periods[i + 1]["start"] for i in range(2))


def test_calibration_and_explanations_never_fit_on_final_test(trained):
    result = trained[-1]
    calibration = result.calibration
    assert "80-85% calibration holdout; 85-100% untouched test" in calibration["selection_data"]
    if calibration["applied"]:
        assert calibration["method"] in {"sigmoid", "isotonic"}
        assert calibration["validation_holdout_improvement"] >= calibration["material_improvement_threshold"]
    assert result.feature_importance
    assert all(entry["feature"] in result.feature_names for entry in result.feature_importance)
    assert all("test" not in entry["method"] for entry in result.feature_importance)


def test_future_outcome_cannot_change_earlier_features(tmp_path):
    rows = raw_rows(180)
    raw = tmp_path / "history.json"; raw.write_text(json.dumps(rows), encoding="utf-8")
    dataset = DatasetService(raw, tmp_path / "processed", tmp_path / "features")
    before = dataset.build_training_dataset(persist=False)
    earlier = dataset.features_after_round("130")
    rows[160]["multiplier"] = 500.0
    raw.write_text(json.dumps(rows), encoding="utf-8")
    after = dataset.build_training_dataset(persist=False)
    assert before.loc[before.round_id == "130"].drop(columns="target_2x").to_dict("records") == after.loc[after.round_id == "130"].drop(columns="target_2x").to_dict("records")
    assert earlier == dataset.features_after_round("130")


def test_causal_pattern_counts_exclude_current_label_and_collection_gaps(tmp_path):
    rows = raw_rows(180)
    raw = tmp_path / "history.json"
    raw.write_text(json.dumps(rows), encoding="utf-8")
    service = DatasetService(raw, tmp_path / "processed", tmp_path / "features")
    original = service.build_training_dataset(persist=False)
    target = original.loc[original.round_id == "135"].iloc[0]
    key = min(int(target.streak_below_2), 6)
    # Include the first 100 rounds, which are eligible evidence even though
    # they are not themselves training rows.
    prior_values = [row["multiplier"] for row in rows[:134]]
    matches = []
    for j in range(1, 134):
        length = 0
        for previous in reversed(prior_values[:j]):
            if previous >= 2:
                break
            length += 1
        if min(length, 6) == key:
            matches.append(j)
    assert target.pattern_streak_count == len(matches)
    hits = sum(prior_values[j] >= 2 for j in matches)
    raw_prior = service.clean_rounds.iloc[:134]
    prior_frequency = (int((raw_prior.multiplier >= 2).sum()) + 10) / (134 + 20)
    assert target.pattern_streak_rate == pytest.approx((hits + 30 * prior_frequency) / (len(matches) + 30))
    rows[134]["multiplier"] = 1.01 if target.target_2x else 999.0  # alter only the target
    raw.write_text(json.dumps(rows), encoding="utf-8")
    changed = service.build_training_dataset(persist=False).loc[lambda frame: frame.round_id == "135"].iloc[0]
    assert changed.drop(labels="target_2x").to_dict() == target.drop(labels="target_2x").to_dict()
    assert changed.target_2x != target.target_2x


def test_incremental_pattern_evidence_equals_full_rebuild(tmp_path):
    rows = raw_rows(130)
    raw = tmp_path / "history.json"
    raw.write_text(json.dumps(rows), encoding="utf-8")
    service = DatasetService(raw, tmp_path / "processed", tmp_path / "features")
    service.build_training_dataset(persist=False)
    raw.write_text(json.dumps(raw_rows(131)), encoding="utf-8")
    incremented = service.process_incremental()
    rebuilt = service.build_training_dataset(persist=False)
    pd.testing.assert_frame_equal(incremented, rebuilt, rtol=1e-10, atol=1e-10)


def test_fast_next_features_match_full_prefix_even_after_long_streak_or_gap(tmp_path):
    rows = raw_rows(155)
    for row in rows:
        row["multiplier"] = 1.01
    rows[121]["timestamp"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    raw = tmp_path / "history.json"
    raw.write_text(json.dumps(rows), encoding="utf-8")
    dataset = DatasetService(raw, tmp_path / "processed", tmp_path / "features")
    dataset.build_training_dataset(persist=False)
    actual = dataset.latest_features()
    full = dataset.clean_rounds
    placeholder = pd.DataFrame([{"round_id": "NEXT", "round_index": 156,
                                 "timestamp": None, "timestamp_dt": pd.NaT, "multiplier": 0.0}])
    expected = dataset._engineer(pd.concat([full, placeholder], ignore_index=True)).iloc[-1]
    assert actual["streak_below_2"] == 155
    for name in dataset.metadata:
        if name.name == "sequence_last_10":
            assert actual[name.name] == expected[name.name]
        else:
            assert actual[name.name] == pytest.approx(expected[name.name], abs=1e-9)


def test_frequency_baseline_is_causal_and_walk_forward_precedes_test(trained):
    y = np.array([0, 1, 1, 0, 1])
    assert causal_frequency(y, 2, 3)[0] == .5
    changed = y.copy(); changed[4] = 0
    assert causal_frequency(changed, 2, 3)[0] == .5
    result = trained[-1]
    assert all(f["evaluation_end"] <= result.splits["train"] for f in result.walk_forward)
    assert "85-100% untouched test" in result.calibration["selection_data"]


def test_fixed_model_prediction_for_past_round_ignores_future_change(trained):
    _, dataset, _, registry, result = trained
    source = str(dataset.clean_rounds.iloc[250]["round_id"])
    earlier = dataset.features_after_round(source)
    names = result.feature_names
    before = float(registry._model.predict_proba(np.array([[earlier[name] for name in names]], dtype=float))[0, 1])
    original = dataset.clean_rounds
    try:
        modified = original.copy()
        modified.loc[300, "multiplier"] = 1000.0
        dataset.clean_rounds = modified
        after_features = dataset.features_after_round(source)
        after = float(registry._model.predict_proba(np.array([[after_features[name] for name in names]], dtype=float))[0, 1])
    finally:
        dataset.clean_rounds = original
    assert before == pytest.approx(after)


def test_future_outcome_feature_is_rejected(trained):
    _, dataset, _, _, _ = trained
    poisoned = dataset.dataset.assign(next_multiplier=dataset.dataset["target_2x"])
    result = ModelTrainer().train_validate(poisoned)
    assert result.status == "INCOMPATIBLE"
    assert result.validated is False


def test_block_bootstrap_advantage_is_reproducible():
    y = np.tile([0, 1], 100)
    strong = np.where(y == 1, .9, .1)
    baseline = np.full(len(y), .5)
    first = block_bootstrap_brier_advantage(y, strong, baseline)
    assert first == block_bootstrap_brier_advantage(y, strong, baseline)
    assert first["ci95"][0] > 0


def test_training_target_requires_complete_observed_prior_window():
    rows = raw_rows(210)
    for row in rows[105:]:
        row["timestamp"] = (datetime.fromisoformat(row["timestamp"]) + timedelta(minutes=10)).isoformat()
    admitted = eligible_round_ids(pd.DataFrame(rows))
    assert "100" not in admitted
    assert "101" in admitted
    assert "105" in admitted
    # Round order and identity prove adjacency; the timestamp jump remains a
    # diagnostic anomaly and cannot break the observation sequence.
    assert "106" in admitted
    assert "205" in admitted
    assert "206" in admitted


def test_fold_reports_base_rate_advantage_and_calibration(trained):
    result = trained[-1]
    for candidate in result.models.values():
        if candidate.get("status") in {"UNAVAILABLE", "FAILED"}:
            assert candidate["rejection_reasons"]
            continue
        assert len(candidate["walk_forward"]) == 3
        for fold in candidate["walk_forward"]:
            assert fold["baseline_name"] == "base_rate_probability"
            assert fold["brier_advantage"] == pytest.approx(
                fold["baseline"]["brier_score"] - fold["metrics"]["brier_score"])
            assert fold["brier_advantage_uncertainty"]["n"] == fold["evaluation_end"] - fold["evaluation_start"]
            assert 0 <= fold["metrics"]["calibration_error"] <= 1
    if result.class_distribution["splits"]["selection"]["n"] >= 100:
        assert set(result.calibration["methods"]) == {"none", "sigmoid", "isotonic"}
    else:
        assert result.calibration["method"] == "none"
        assert result.calibration["applied"] is False


def test_registry_rejects_concurrent_training_attempt(tmp_path, monkeypatch):
    registry = ModelRegistry(model_dir=tmp_path / "models")
    entered, release = Event(), Event()
    dataset = SimpleNamespace(dataset=pd.DataFrame(), raw_path=None)
    first_result = []

    def slow_training(_snapshot):
        entered.set()
        assert release.wait(timeout=5)
        return registry.trainer._empty("INSUFFICIENT_DATA", "fixture", 0)

    monkeypatch.setattr(registry.trainer, "train_validate", slow_training)
    worker = Thread(target=lambda: first_result.append(registry.train(dataset)))
    worker.start()
    try:
        assert entered.wait(timeout=5)
        with pytest.raises(RuntimeError, match="training_already_in_progress"):
            registry.train(dataset)
    finally:
        release.set()
        worker.join(timeout=5)
    assert not worker.is_alive()
    assert len(first_result) == 1


def test_requested_windows_and_conditional_features_are_past_only(tmp_path):
    rows = raw_rows(180)
    raw = tmp_path / "history.json"
    raw.write_text(json.dumps(rows), encoding="utf-8")
    service = DatasetService(raw, tmp_path / "processed", tmp_path / "features")
    before = service.build_training_dataset(persist=False)
    target = before.loc[before.round_id == "150"].iloc[0]
    assert all(f"rate_2x_last_{window}" in before for window in (5, 10, 20, 50, 100))
    assert "rate_2x_last_25" not in before
    assert "variance_last_20" not in before
    assert target.transition_after_0_count_100 + target.transition_after_1_count_100 == 100
    rows[149]["multiplier"] = 999 if target.target_2x == 0 else 1.01
    raw.write_text(json.dumps(rows), encoding="utf-8")
    after = service.build_training_dataset(persist=False).loc[lambda frame: frame.round_id == "150"].iloc[0]
    assert after.drop(labels="target_2x").to_dict() == target.drop(labels="target_2x").to_dict()


def test_insufficient_history_fallback_has_no_probability(tmp_path):
    raw = tmp_path / "history.json"; raw.write_text(json.dumps(raw_rows(120)), encoding="utf-8")
    dataset = DatasetService(raw, tmp_path / "processed", tmp_path / "features")
    dataset.build_training_dataset(persist=False)
    registry = ModelRegistry(model_dir=tmp_path / "models")
    fallback = registry.fallback_estimate(dataset)
    assert fallback["probability_2x"] is None
    assert fallback["usable"] is False


def test_independent_rounds_do_not_promote_a_spurious_model(tmp_path):
    rng = np.random.default_rng(2026)
    rows = raw_rows(520)
    for row, hit in zip(rows, rng.integers(0, 2, len(rows))):
        row["multiplier"] = 2.5 if hit else 1.2
    raw = tmp_path / "history.json"; raw.write_text(json.dumps(rows), encoding="utf-8")
    dataset = DatasetService(raw, tmp_path / "processed", tmp_path / "features")
    frame = dataset.build_training_dataset(persist=False)
    result = ModelTrainer().train_validate(frame)
    assert result.validated is False
    assert result.overfitting_checks["deployable"] is False
