"""Block-permutation null for the frozen next-round 2x evaluation.

The existing ModelTrainer is called unchanged for every permuted target. This
script never writes an active model or changes the chronological split. Runs
are checkpointed so a 300-iteration calculation can be resumed safely.

From the project root:
  PYTHONPATH=backend backend/.venv/bin/python backend/scripts/null_check.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

from app.core.config import get_settings
from app.ml.trainer import (MAX_HISTORY_GAP_S, TARGET_COLUMN, ModelTrainer,
                            ProbabilityCalibratedModel, causal_frequency,
                            evaluate_probabilities, feature_schema)
from app.services.dataset_service import DatasetService

METRICS = ("roc_auc", "pr_auc", "brier_score", "log_loss")
ENTITIES = ("extra_trees", "rolling_250", "pipeline_selected")
_WORKER_DATASET = None
_WORKER_METADATA = None
_WORKER_BLOCK_SIZE = None


def permute_time_blocks(values: np.ndarray, block_size: int, seed: int) -> np.ndarray:
    """Reorder whole consecutive blocks; never shuffle values within a block."""
    values = np.asarray(values)
    if block_size < 2 or block_size > len(values):
        raise ValueError("block_size must be between 2 and the number of target rows")
    blocks = [values[start:start + block_size] for start in range(0, len(values), block_size)]
    order = np.random.default_rng(seed).permutation(len(blocks))
    return np.concatenate([blocks[index] for index in order])


def _eligible_frame(dataset: pd.DataFrame) -> pd.DataFrame:
    """Mirror the trainer's fixed eligibility rule; do not alter its folds."""
    names, _, _ = feature_schema(dataset)
    ordered = dataset.dropna(subset=names + [TARGET_COLUMN]).reset_index(drop=True)
    times = pd.to_datetime(ordered["timestamp"], utc=True, errors="coerce")
    seconds = times.diff().dt.total_seconds()
    delta = ordered["round_index"].diff()
    bad = ((seconds > MAX_HISTORY_GAP_S) | (seconds < 0) | (delta != 1)).fillna(False)
    bad.iloc[0] = False
    return ordered.loc[~bad].reset_index(drop=True)


def _compact_metrics(y: np.ndarray, probabilities: np.ndarray) -> dict:
    evaluated = evaluate_probabilities(y, probabilities)
    # Parallel tree reductions may differ at the last binary digit; retain
    # far more precision than the report needs while making reruns byte-stable.
    return {key: round(float(evaluated[key]), 12) for key in METRICS}


def _one_permutation(dataset: pd.DataFrame, metadata: dict, block_size: int, seed: int) -> dict:
    changed = dataset.copy()
    changed[TARGET_COLUMN] = permute_time_blocks(dataset[TARGET_COLUMN].to_numpy(int), block_size, seed)
    trainer = ModelTrainer()
    result = trainer.train_validate(changed)
    if result.test_metrics is None:
        raise RuntimeError(f"null training failed: {result.status}: {result.message}")
    clean = _eligible_frame(changed)
    if len(clean) != metadata["dataset_size"] or result.splits != metadata["splits"]:
        raise RuntimeError("permutation changed eligible rows or chronological split")
    names = result.feature_names
    train_end = result.splits["train"]
    test_start = train_end + result.splits["validation"]
    y = clean[TARGET_COLUMN].to_numpy(int)
    X_train = clean.iloc[:train_end][names].to_numpy(float)
    X_test = clean.iloc[test_start:][names].to_numpy(float)
    y_train, y_test = y[:train_end], y[test_start:]
    if result.algorithm == "extra_trees":
        fitted = result._model
        extra = fitted.model if isinstance(fitted, ProbabilityCalibratedModel) else fitted
    else:
        weighted = "balanced" if min(float(y_train.mean()), 1 - float(y_train.mean())) < .20 else None
        extra = ModelTrainer.factories(weighted)["extra_trees"]()
        extra.fit(X_train, y_train)
    extra_probability = extra.predict_proba(X_test)[:, 1]
    rolling_probability = causal_frequency(y, test_start, len(y), 250)
    return {
        "selected_algorithm": result.algorithm,
        "extra_trees": _compact_metrics(y_test, extra_probability),
        "rolling_250": _compact_metrics(y_test, rolling_probability),
        "pipeline_selected": {key: round(float(result.test_metrics[key]), 12) for key in METRICS},
    }


def _distribution(values: list[float]) -> dict:
    array = np.asarray(values, dtype=float)
    if not np.isfinite(array).all():
        raise ValueError("non-finite metric in null samples")
    q = np.quantile(array, [.025, .5, .975])
    return {"mean": float(array.mean()), "sd": float(array.std(ddof=1)) if len(array) > 1 else 0.0,
            "p2_5": float(q[0]), "p50": float(q[1]), "p97_5": float(q[2])}


def _two_sided_p(real: float, null_values: list[float]) -> float:
    """Finite-permutation p-value about the null center, with +1 correction."""
    center = float(np.mean(null_values))
    observed = abs(real - center)
    extreme = sum(abs(value - center) >= observed - 1e-14 for value in null_values)
    return (extreme + 1) / (len(null_values) + 1)


def summarize(samples: list[dict], metadata: dict, *, block_size: int, seed: int) -> dict:
    baseline = metadata["baselines"]["test"]["rolling_250"]
    real = {"extra_trees": metadata["test_metrics"], "rolling_250": baseline}
    stats = {}
    for entity in ENTITIES:
        stats[entity] = {}
        for metric in METRICS:
            values = [record[entity][metric] for record in samples]
            entry = _distribution(values)
            if entity in real:
                entry["real"] = float(real[entity][metric])
                entry["empirical_two_sided_p"] = _two_sided_p(entry["real"], values)
            stats[entity][metric] = entry
    return {"model_version": metadata["model_version"], "source_sha256": metadata["source_sha256"],
            "method": "whole contiguous target-block permutation; trainer and features unchanged",
            "p_value_definition": "(1 + count(|null - null_mean| >= |real - null_mean|)) / (iterations + 1)",
            "iterations": len(samples), "block_size": block_size, "seed": seed,
            "test_rows": metadata["splits"]["test"], "statistics": stats,
            "samples": samples,
            "interpretation": "A null comparison is not proof of randomness or of absent predictive signal."}


def _atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def _text_summary(report: dict) -> str:
    lines = [f"Block-permutation null: {report['model_version']}",
             f"Iterations: {report['iterations']}; block size: {report['block_size']}; seed: {report['seed']}",
             "Two-sided p-values use the empirical null mean as their center and a +1 correction."]
    for entity in ENTITIES:
        lines.append(f"\n{entity}:")
        for metric in METRICS:
            row = report["statistics"][entity][metric]
            suffix = (f"; real={row['real']:.6f}; p={row['empirical_two_sided_p']:.6f}"
                      if "real" in row else "")
            lines.append(f"  {metric}: mean={row['mean']:.6f}, sd={row['sd']:.6f}, "
                         f"2.5/50/97.5%={row['p2_5']:.6f}/{row['p50']:.6f}/{row['p97_5']:.6f}{suffix}")
    lines.append("\nNo significance test can establish that no signal exists.")
    return "\n".join(lines) + "\n"


def _init_worker(dataset: pd.DataFrame, metadata: dict, block_size: int) -> None:
    global _WORKER_DATASET, _WORKER_METADATA, _WORKER_BLOCK_SIZE
    _WORKER_DATASET, _WORKER_METADATA, _WORKER_BLOCK_SIZE = dataset, metadata, block_size


def _worker(task: tuple[int, int]) -> tuple[int, dict]:
    iteration, seed = task
    return iteration, _one_permutation(_WORKER_DATASET, _WORKER_METADATA, _WORKER_BLOCK_SIZE, seed)


def run(dataset: pd.DataFrame, metadata: dict, output_dir: Path, *,
        iterations: int = 300, block_size: int = 50, seed: int = 20260919,
        workers: int = 2) -> dict:
    if iterations < 1 or workers < 1:
        raise ValueError("iterations and workers must be positive")
    if metadata.get("algorithm") != "extra_trees" or metadata.get("calibration", {}).get("applied"):
        raise ValueError("this frozen null comparison requires the recorded uncalibrated Extra Trees result")
    if len(_eligible_frame(dataset)) != metadata["dataset_size"]:
        raise ValueError("dataset does not match the frozen model's eligible row count")
    output_dir = Path(output_dir)
    progress_path = output_dir / "progress.json"
    fingerprint = hashlib.sha256(pd.util.hash_pandas_object(dataset, index=False).values.tobytes()).hexdigest()
    config = {"model_version": metadata["model_version"], "dataset_fingerprint": fingerprint,
              "iterations": iterations, "block_size": block_size, "seed": seed}
    if progress_path.exists():
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        if progress["config"] != config:
            raise ValueError("existing null-check checkpoint has different inputs or parameters")
        samples = progress["samples"]
    else:
        samples = []
    tasks = ((iteration, int(np.random.SeedSequence([seed, iteration]).generate_state(1)[0]))
             for iteration in range(len(samples), iterations))
    if workers == 1:
        results = ((iteration, _one_permutation(dataset, metadata, block_size, permutation_seed))
                   for iteration, permutation_seed in tasks)
        for iteration, record in results:
            samples.append({"iteration": iteration, **record})
            _atomic_json(progress_path, {"config": config, "samples": samples})
            print(f"null iteration {iteration + 1}/{iterations}: {record['selected_algorithm']}", flush=True)
    else:
        with ProcessPoolExecutor(max_workers=workers, initializer=_init_worker,
                                 initargs=(dataset, metadata, block_size)) as pool:
            for iteration, record in pool.map(_worker, tasks, chunksize=1):
                samples.append({"iteration": iteration, **record})
                _atomic_json(progress_path, {"config": config, "samples": samples})
                print(f"null iteration {iteration + 1}/{iterations}: {record['selected_algorithm']}", flush=True)
    report = summarize(samples, metadata, block_size=block_size, seed=seed)
    _atomic_json(output_dir / "report.json", report)
    (output_dir / "summary.txt").write_text(_text_summary(report), encoding="utf-8")
    return report


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="300-block-shuffle null for the frozen model")
    parser.add_argument("--model-dir", type=Path, default=settings.model_dir)
    parser.add_argument("--version")
    parser.add_argument("--data", type=Path, help="history snapshot; defaults to the archived training SHA-256")
    parser.add_argument("--iterations", type=int, default=300)
    parser.add_argument("--block-size", type=int, default=50)
    parser.add_argument("--seed", type=int, default=20260919)
    parser.add_argument("--workers", type=int, default=2)
    args = parser.parse_args()
    active = json.loads((args.model_dir / "active.json").read_text(encoding="utf-8"))
    version = args.version or active["model_version"]
    version_dir = args.model_dir / version
    metadata = json.loads((version_dir / "metadata.json").read_text(encoding="utf-8"))
    snapshot = args.data or args.model_dir.parent / "artifacts" / "training_datasets" / f"{metadata['source_sha256']}.json"
    raw = snapshot.read_bytes()
    if hashlib.sha256(raw).hexdigest() != metadata["source_sha256"]:
        raise ValueError("history snapshot SHA-256 does not match the frozen model")
    service = DatasetService(snapshot)
    frame = service.build_training_dataset(persist=False)
    report = run(frame, metadata, version_dir / "null_check", iterations=args.iterations,
                 block_size=args.block_size, seed=args.seed, workers=args.workers)
    print(_text_summary(report), end="")


if __name__ == "__main__":
    main()
