from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import numpy as np

from app.services.dataset_service import DatasetService
from scripts import null_check
from scripts.randomness_tests import battery


def _dataset(tmp_path):
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)
    rows = [{"round_id": str(i + 1), "round_index": i + 1,
             "timestamp": (start + timedelta(seconds=i)).isoformat(),
             "multiplier": 2.5 if (i * 17) % 31 < 15 else 1.2}
            for i in range(520)]
    source = tmp_path / "roundhistory.json"
    source.write_text(json.dumps(rows), encoding="utf-8")
    service = DatasetService(source, tmp_path / "processed", tmp_path / "features")
    return service.build_training_dataset(persist=False)


def _metadata(dataset):
    n = len(null_check._eligible_frame(dataset))
    train_end, validation_end = int(n * .70), int(n * .85)
    return {"model_version": "test-v1", "source_sha256": "test-source",
            "algorithm": "extra_trees", "calibration": {"applied": False},
            "dataset_size": n,
            "splits": {"train": train_end, "validation": validation_end - train_end,
                       "test": n - validation_end},
            "test_metrics": {metric: .5 for metric in null_check.METRICS},
            "baselines": {"test": {"rolling_250": {metric: .5 for metric in null_check.METRICS}}}}


def test_block_shuffle_keeps_whole_blocks_and_base_rate():
    unique = np.arange(103)
    shuffled = null_check.permute_time_blocks(unique, 25, 123)
    assert sorted(shuffled.tolist()) == unique.tolist()
    blocks = [unique[i:i + 25] for i in range(0, len(unique), 25)]
    for block in blocks:
        assert any(np.array_equal(shuffled[i:i + len(block)], block)
                   for i in range(len(shuffled) - len(block) + 1))
    binary = (unique % 3 == 0).astype(int)
    assert int(null_check.permute_time_blocks(binary, 25, 123).sum()) == int(binary.sum())
    assert np.array_equal(shuffled, null_check.permute_time_blocks(unique, 25, 123))


def test_null_training_pipeline_reproducible_with_fixed_seed(tmp_path):
    dataset = _dataset(tmp_path)
    metadata = _metadata(dataset)
    before = dataset.copy(deep=True)
    one = null_check._one_permutation(dataset, metadata, 50, 98765)
    two = null_check._one_permutation(dataset, metadata, 50, 98765)
    assert one == two
    assert set(one) == {"selected_algorithm", *null_check.ENTITIES}
    assert all(0 <= one[entity]["roc_auc"] <= 1 for entity in null_check.ENTITIES)
    assert dataset.equals(before)  # only the copied target is permuted


def test_null_report_checkpoint_resume_and_p_values(tmp_path, monkeypatch):
    dataset = _dataset(tmp_path)
    metadata = _metadata(dataset)

    def deterministic(_dataset, _metadata, _block_size, seed):
        value = .45 + (seed % 10) / 100
        return {"selected_algorithm": "extra_trees", **{
            entity: {metric: value for metric in null_check.METRICS}
            for entity in null_check.ENTITIES}}

    monkeypatch.setattr(null_check, "_one_permutation", deterministic)
    output = tmp_path / "null_check"
    first = null_check.run(dataset, metadata, output, iterations=3, seed=42, workers=1)
    second = null_check.run(dataset, metadata, output, iterations=3, seed=42, workers=1)
    assert first == second
    assert (output / "report.json").exists() and (output / "summary.txt").exists()
    assert len(first["samples"]) == 3
    assert 1 / 4 <= first["statistics"]["extra_trees"]["roc_auc"]["empirical_two_sided_p"] <= 1
    assert first["statistics"]["rolling_250"]["brier_score"]["p50"] is not None


def test_randomness_battery_seeded_prng_is_not_rejected():
    values = np.random.default_rng(12345).integers(0, 2, 5000)
    report = battery(values)
    assert report["overall_verdict"] == "NO_REJECTION_AT_ALPHA"
    assert report["failed_tests"] == []
    assert len(report["tests"]) == 55
    assert all(row["holm_adjusted_p"] >= row["p_value"] for row in report["tests"].values())


def test_randomness_battery_detects_autocorrelation_after_holm():
    rng = np.random.default_rng(12345)
    values = np.empty(5000, dtype=int)
    values[0] = 0
    flips = rng.random(4999) >= .9
    for i, flip in enumerate(flips, start=1):
        values[i] = 1 - values[i - 1] if flip else values[i - 1]
    report = battery(values)
    assert report["overall_verdict"] == "REJECT_SOME_NULLS"
    assert not report["tests"]["autocorrelation_lag_1"]["pass"]
    assert not report["tests"]["runs"]["pass"]
    assert not report["tests"]["ljung_box"]["pass"]
