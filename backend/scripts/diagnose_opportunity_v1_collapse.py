"""One-time, immutable diagnostics for the already inspected V1 test.

This script never fits/selects a model or threshold. It only measures feature
distribution and target-association shifts, then stores them as diagnostic-only
PostgreSQL application state. Existing frozen output is returned unchanged.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from scipy.stats import ks_2samp

from app.core.config import get_settings
from app.database.repository import Repository
from app.ml.opportunity import TARGET, build_opportunity_frame, validated_rounds
from app.ml.opportunity_v2 import V1_DIAGNOSTIC_START_INDEX, V1_SOURCE_END_INDEX

KEY = "selective_opportunity_v1_feature_diagnostic"
EXPECTED_CLEAN = 17305
EXPECTED_TARGETS = 10680


def _corr(values, outcomes):
    if len(values) < 3 or np.std(values) < 1e-10 or np.std(outcomes) < 1e-10:
        return None
    result = float(np.corrcoef(values, outcomes)[0, 1])
    return result if np.isfinite(result) else None


def _psi(reference, current, bins=10):
    edges = np.unique(np.quantile(reference, np.linspace(0, 1, bins + 1)))
    if len(edges) < 3:
        return None
    edges[0], edges[-1] = -np.inf, np.inf
    ref = np.histogram(reference, bins=edges)[0].astype(float)
    cur = np.histogram(current, bins=edges)[0].astype(float)
    ref = np.clip(ref / max(ref.sum(), 1), .005, 1)
    cur = np.clip(cur / max(cur.sum(), 1), .005, 1)
    return float(np.sum((cur - ref) * np.log(cur / ref)))


def main():
    settings = get_settings()
    repository = Repository(settings.database_path, settings.database_url, settings.require_postgres)
    repository.init()
    existing = repository.load_application_state(KEY)
    if existing:
        print(json.dumps({"status": "FROZEN_DIAGNOSTIC_ALREADY_EXISTS", "key": KEY,
                          "generated_at": existing.get("generated_at"),
                          "round_snapshot_sha256": existing.get("round_snapshot_sha256")}, indent=2))
        return
    rows = pd.DataFrame(repository.load_rounds())
    clean, quality = validated_rounds(rows)
    clean = clean.loc[clean["round_index"] <= V1_SOURCE_END_INDEX].reset_index(drop=True)
    if len(clean) != EXPECTED_CLEAN:
        raise RuntimeError(f"Frozen V1 source count mismatch: expected {EXPECTED_CLEAN}, received {len(clean)}")
    snapshot = "\n".join(clean["round_id"].astype(str).tolist())
    frame, frame_quality = build_opportunity_frame(clean, TARGET)
    if len(frame) != EXPECTED_TARGETS:
        raise RuntimeError(f"Frozen V1 target count mismatch: expected {EXPECTED_TARGETS}, received {len(frame)}")
    excluded = {"source_round_id", "source_round_index", "target_round_id", "target_round_index",
                "target_timestamp", "target_multiplier_value", "target_hit"}
    features = [name for name in frame.columns if name not in excluded]
    y = frame["target_hit"].to_numpy(int)
    n = len(frame)
    train_end, cal_end, validation_end = int(n * .60), int(n * .70), int(n * .85)
    validation = frame.iloc[cal_end:validation_end]
    test = frame.iloc[validation_end:]
    y_val, y_test = y[cal_end:validation_end], y[validation_end:]
    diagnostics = []
    for name in features:
        x_train = frame[name].to_numpy(float)[:train_end]
        x_cal = frame[name].to_numpy(float)[train_end:cal_end]
        x_val = validation[name].to_numpy(float)
        x_test = test[name].to_numpy(float)
        val_std, test_std = float(np.std(x_val)), float(np.std(x_test))
        denominator = float(np.sqrt((val_std ** 2 + test_std ** 2) / 2))
        r_train, r_cal = _corr(x_train, y[:train_end]), _corr(x_cal, y[train_end:cal_end])
        r_val, r_test = _corr(x_val, y_val), _corr(x_test, y_test)
        signs = [int(np.sign(value)) if value is not None and abs(value) >= .01 else 0
                 for value in (r_train, r_cal, r_val, r_test)]
        diagnostics.append({
            "feature": name, "family": ("pattern" if name.startswith("pattern_") else
                       "regime" if any(part in name for part in ("rate_", "std_", "entropy_", "q25_", "q75_", "shift")) else "distribution_or_sequence"),
            "training_target_correlation": r_train, "calibration_target_correlation": r_cal,
            "validation_target_correlation": r_val, "old_test_target_correlation_diagnostic_only": r_test,
            "validation_test_direction_consistent": bool(r_val is not None and r_test is not None and np.sign(r_val) == np.sign(r_test)),
            "direction_by_partition_train_cal_validation_test": signs,
            "validation_mean": float(np.mean(x_val)), "old_test_mean_diagnostic_only": float(np.mean(x_test)),
            "validation_test_standardized_mean_difference": (float((np.mean(x_test) - np.mean(x_val)) / denominator)
                                                              if denominator > 1e-12 else 0.0),
            "validation_test_ks_statistic": float(ks_2samp(x_val, x_test).statistic),
            "validation_test_ks_pvalue_diagnostic_only": float(ks_2samp(x_val, x_test).pvalue),
            "validation_test_psi": _psi(x_val, x_test),
        })
    report = {
        "status": "FROZEN_DIAGNOSTIC_ONLY", "used_for_selection": False,
        "used_for_threshold_or_model_changes": False, "target": TARGET,
        "source": "PostgreSQL:aviator_rounds", "clean_rounds": len(clean),
        "eligible_targets": len(frame), "quality": quality,
        "feature_frame_quality": frame_quality,
        "frozen_round_indices": {"v1_test_start": V1_DIAGNOSTIC_START_INDEX,
                                  "v1_source_end": V1_SOURCE_END_INDEX},
        "round_snapshot_sha256": hashlib.sha256(snapshot.encode()).hexdigest(),
        "partitions": {"train": [0, train_end], "calibration": [train_end, cal_end],
                       "validation": [cal_end, validation_end], "old_test_diagnostic_only": [validation_end, n]},
        "validation_rate": float(y_val.mean()), "old_test_rate_diagnostic_only": float(y_test.mean()),
        "feature_diagnostics": diagnostics,
        "summary": {
            "features": len(features),
            "features_psi_gt_0_2": sum((row["validation_test_psi"] or 0) > .2 for row in diagnostics),
            "features_ks_p_lt_0_05_diagnostic_only": sum(row["validation_test_ks_pvalue_diagnostic_only"] < .05 for row in diagnostics),
            "features_target_direction_changed_validation_to_old_test": sum(not row["validation_test_direction_consistent"] for row in diagnostics),
            "strongest_distribution_shifts": sorted(diagnostics,
                key=lambda row: abs(row["validation_test_standardized_mean_difference"]), reverse=True)[:10],
            "largest_psi_shifts": sorted((row for row in diagnostics if row["validation_test_psi"] is not None),
                key=lambda row: row["validation_test_psi"], reverse=True)[:10],
        },
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
    repository.save_application_state(KEY, report, report["generated_at"])
    print(json.dumps({"status": report["status"], "key": KEY,
                      "round_snapshot_sha256": report["round_snapshot_sha256"],
                      "summary": report["summary"],
                      "partitions": report["partitions"]}, indent=2))


if __name__ == "__main__":
    main()
