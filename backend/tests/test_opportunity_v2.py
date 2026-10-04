from types import SimpleNamespace

import numpy as np
import pandas as pd

from app.ml.opportunity_v2 import (
    V1_DIAGNOSTIC_START_INDEX, V1_SOURCE_END_INDEX, _collapse_diagnosis,
    _dynamic_baselines, _eligible_mask, _feature_stability, run_v2,
)


def test_dynamic_baselines_are_trailing_feature_values():
    names = ["past_target_rate", "rate_ge_2_1_100", "rate_ge_2_1_250",
             "rate_ge_2_1_500", "rate_ge_2_1_1000"]
    x = np.array([[.5, .6, .55, .52, .48], [.4, .5, .45, .43, .42]])
    baselines = _dynamic_baselines(x, names)
    assert baselines["last_1000"].tolist() == [.48, .42]
    assert baselines["relevant"].tolist() == [.48, .42]
    assert baselines["long_term"].tolist() == [.5, .4]


def test_feature_stability_uses_training_blocks_and_rejects_weak_features():
    rng = np.random.default_rng(4)
    y = np.tile(np.array([0, 1]), 200)
    stable = y + rng.normal(0, .1, len(y))
    unstable = np.r_[y[:200], 1 - y[200:400], y[400:600], 1 - y[600:]] + rng.normal(0, .1, len(y))
    noise = rng.normal(size=len(y))
    report = _feature_stability(np.column_stack([stable, unstable, noise]), y,
                                ["stable", "unstable", "noise"])
    assert "stable" in report["retained"]
    assert "unstable" not in report["retained"]
    assert "noise" not in report["retained"]
    assert len(report["rows"]) == 3


def test_evidence_gate_requires_independent_evidence_and_low_uncertainty():
    n = 2
    components = np.zeros((n, 7))
    components[:, 2] = [.1, .1]  # positive pattern lift
    components[:, 3] = [.01, .01]  # regime shift below warning limit
    components[:, 6] = [-.1, -.1]  # uncertainty
    evidence = {
        "baseline": {"relevant": np.array([.45, .45])},
        "model_probabilities": np.array([[.6, .58, .61, .56, .62], [.46, .4, .44, .56, .42]]),
        "analogs": {"count": np.array([100, 100]), "lift": np.array([.2, .2]),
                    "ci_lower": np.array([.5, .5]), "stable_blocks": np.array([1., 1.])},
    }
    allowed = _eligible_mask(components, evidence, .2, .1)
    assert allowed.tolist() == [True, False]


def test_analog_baseline_comes_from_all_evidence_features_when_predictors_are_filtered():
    from app.ml.opportunity_v2 import _evidence_matrix

    rng = np.random.default_rng(7)
    train = rng.normal(size=(150, 1))
    y = np.tile([0, 1], 75)
    query = np.array([[.3]])
    full = np.array([[.1, .5, .4, .45, .48, .02, .03, .01]])
    names = ["pattern_sequence_5_rate", "past_target_rate", "rate_ge_2_1_100",
             "rate_ge_2_1_250", "rate_ge_2_1_500", "rate_ge_2_1_1000",
             "recent_long_rate_shift_2_1", "recent_long_log_mean_shift"]
    p = {"a": np.array([.6]), "b": np.array([.61]), "c": np.array([.59])}
    components, evidence = _evidence_matrix(train, y, full, p, names, .1,
        X_query_stable=query, analog_feature_names=["pattern_sequence_5_rate"])
    assert np.isfinite(components).all()
    assert evidence["analogs"]["count"][0] == 100
    assert evidence["baseline"]["relevant"][0] == .02


def test_v1_collapse_diagnosis_calls_out_selection_and_small_sample():
    report = _collapse_diagnosis({
        "algorithms_tested": [{"model": "a"}, {"model": "b"}],
        "validation": {"signals": 17, "true_signals": 14, "false_signals": 3,
                       "precision_ci95": {"lower": .59, "upper": .94},
                       "baseline_rate": .471, "calibration_diagnostics": {"expected_calibration_error": .028}},
        "untouched_test": {"signals": 16, "true_signals": 8, "false_signals": 8,
                           "precision_ci95": {"lower": .28, "upper": .72},
                           "base_rate_observed": .478,
                           "calibration": {"expected_calibration_error": .037}},
    })
    assert report["threshold_overfit"]["searches"] == 10
    assert report["small_signal_sample"]["assessment"] == "CONFIRMED"
    assert "NOT_ESTABLISHED" in report["regime_or_distribution_change"]


def test_nested_research_never_passes_the_frozen_v1_test_to_feature_builder(monkeypatch):
    from app.ml import opportunity_v2

    rows = []
    for index in list(range(10000, 10030)) + list(range(V1_DIAGNOSTIC_START_INDEX, V1_DIAGNOSTIC_START_INDEX + 10)) + list(range(V1_SOURCE_END_INDEX + 1, V1_SOURCE_END_INDEX + 11)):
        rows.append({"round_id": str(index), "round_index": index,
                     "timestamp": pd.Timestamp("2026-10-01", tz="UTC") + pd.Timedelta(seconds=index),
                     "multiplier": 1.5})
    source = pd.DataFrame(rows)

    def fake_builder(development, target):
        assert int(development["round_index"].max()) < V1_DIAGNOSTIC_START_INDEX
        assert not (development["round_index"] > V1_SOURCE_END_INDEX).any()
        fake = pd.DataFrame({"source_round_id": [str(i) for i in range(200)],
                             "source_round_index": np.arange(200),
                             "target_round_id": [str(i + 1) for i in range(200)],
                             "target_round_index": np.arange(1, 201),
                             "target_timestamp": ["2026-10-01T00:00:00Z"] * 200,
                             "target_multiplier_value": np.ones(200),
                             "target_hit": np.tile([0, 1], 100)})
        return fake, {"eligible_targets": len(fake)}

    monkeypatch.setattr(opportunity_v2, "build_opportunity_frame", fake_builder)
    result = run_v2(SimpleNamespace(clean_rounds=source), {"untouched_test": {"signals": 16}})
    assert result["old_untouched_test"]["label"] == "DIAGNOSTIC ONLY"
    assert result["policy_search"]["old_test_accessed"] is False
    assert result["frozen_boundaries"]["v1_diagnostic_start_round_index"] == V1_DIAGNOSTIC_START_INDEX
