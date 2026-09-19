"""Descriptive randomness battery for observed >=2x outcomes.

Collector gaps break serial pairs and runs. Failure to reject is not proof of
independence, nor is rejection necessarily evidence of forecastable returns.

From the project root:
  PYTHONPATH=backend backend/.venv/bin/python backend/scripts/randomness_tests.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path

import numpy as np
from scipy.stats import binomtest, chi2, chi2_contingency, norm, pearsonr

from app.core.config import get_settings
from app.services.dataset_service import DatasetService


def _segments(values: np.ndarray, starts: np.ndarray) -> list[np.ndarray]:
    cuts = np.flatnonzero(starts)
    return [values[a:b] for a, b in zip(cuts, [*cuts[1:], len(values)])]


def _runs_test(segments: list[np.ndarray]) -> dict:
    observed, expected, variance, transitions = 0, 0.0, 0.0, 0
    for segment in segments:
        n = len(segment)
        if not n:
            continue
        hits = int(segment.sum())
        misses = n - hits
        observed += 1 + int(np.count_nonzero(segment[1:] != segment[:-1]))
        expected += 1 + 2 * hits * misses / n
        transitions += max(0, n - 1)
        if n > 1 and hits and misses:
            variance += 2 * hits * misses * (2 * hits * misses - n) / (n * n * (n - 1))
    z = (observed - expected) / math.sqrt(variance) if variance > 0 else 0.0
    return {"statistic": float(z), "p_value": float(2 * norm.sf(abs(z))),
            "observed_runs": observed, "expected_runs": expected,
            "variance": variance, "within_segment_pairs": transitions}


def _serial(segments: list[np.ndarray], max_lag: int) -> tuple[list[dict], dict]:
    lag_rows = []
    total = sum(map(len, segments))
    q = 0.0
    for lag in range(1, max_lag + 1):
        pairs = [(segment[:-lag], segment[lag:]) for segment in segments if len(segment) > lag]
        if pairs:
            before = np.concatenate([pair[0] for pair in pairs])
            after = np.concatenate([pair[1] for pair in pairs])
        else:
            before = after = np.empty(0, dtype=int)
        n = len(before)
        if n > 3 and len(np.unique(before)) == len(np.unique(after)) == 2:
            correlation, p_value = pearsonr(before, after)
            correlation, p_value = float(correlation), float(p_value)
        else:
            correlation, p_value = 0.0, 1.0
        lag_rows.append({"lag": lag, "statistic": correlation, "p_value": p_value, "pairs": n})
        if n:
            q += total * (total + 2) * correlation * correlation / max(total - lag, 1)
    # Approximate portmanteau statistic for independent Bernoulli outcomes;
    # each lag's correlations exclude pairs crossing a collector gap.
    ljung_box = {"statistic": float(q), "p_value": float(chi2.sf(q, max_lag)),
                 "degrees_of_freedom": max_lag, "approximation": "gap-aware pair correlations"}
    return lag_rows, ljung_box


def _transitions(segments: list[np.ndarray]) -> tuple[dict, dict]:
    first = np.zeros((2, 2), dtype=int)
    second = np.zeros((2, 2, 2), dtype=int)
    for segment in segments:
        for a, b in zip(segment[:-1], segment[1:]):
            first[a, b] += 1
        for a, b, c in zip(segment[:-2], segment[1:-1], segment[2:]):
            second[a, b, c] += 1
    if np.all(first.sum(axis=0) > 0) and np.all(first.sum(axis=1) > 0):
        first_stat, first_p, first_df, _ = chi2_contingency(first, correction=False)
    else:
        first_stat, first_p, first_df = 0.0, 1.0, 0
    second_stat, second_df = 0.0, 0
    for middle in (0, 1):
        table = second[:, middle, :]
        if np.all(table.sum(axis=0) > 0) and np.all(table.sum(axis=1) > 0):
            statistic, _, degrees, _ = chi2_contingency(table, correction=False)
            second_stat += float(statistic)
            second_df += int(degrees)
    second_p = float(chi2.sf(second_stat, second_df)) if second_df else 1.0
    return ({"statistic": float(first_stat), "p_value": float(first_p),
             "degrees_of_freedom": int(first_df), "matrix": first.tolist()},
            {"statistic": second_stat, "p_value": second_p,
             "degrees_of_freedom": second_df, "matrix": second.tolist(),
             "null": "Y[t] independent of Y[t-2] conditional on Y[t-1]"})


def _holm(tests: dict[str, dict], alpha: float) -> None:
    ordered = sorted(tests, key=lambda name: tests[name]["p_value"])
    adjusted = 0.0
    m = len(ordered)
    for rank, name in enumerate(ordered):
        adjusted = max(adjusted, min(1.0, (m - rank) * tests[name]["p_value"]))
        tests[name]["holm_adjusted_p"] = adjusted
        tests[name]["pass"] = adjusted >= alpha
        tests[name]["verdict"] = "FAIL_REJECT_IID_OR_BALANCE" if adjusted < alpha else "PASS_NOT_REJECTED"


def battery(values: np.ndarray, *, starts: np.ndarray | None = None,
            alpha: float = .05, max_lag: int = 50) -> dict:
    values = np.asarray(values, dtype=int)
    if len(values) < max(100, max_lag + 4) or not np.isin(values, [0, 1]).all():
        raise ValueError("battery needs at least 100 binary outcomes and enough rows for the requested lags")
    if not 0 < alpha < 1 or max_lag < 1:
        raise ValueError("alpha and max_lag must be positive and valid")
    if starts is None:
        starts = np.zeros(len(values), dtype=bool)
    else:
        starts = np.asarray(starts, dtype=bool).copy()
        if len(starts) != len(values):
            raise ValueError("starts must have one entry per outcome")
    starts[0] = True
    segments = _segments(values, starts)
    runs = _runs_test(segments)
    acf, ljung = _serial(segments, max_lag)
    first, second = _transitions(segments)
    rate = float(values.mean())
    entropy = float(-sum(p * math.log2(p) for p in (rate, 1 - rate) if p > 0))
    balance = {"statistic": entropy, "p_value": float(binomtest(int(values.sum()), len(values), .5).pvalue),
               "theoretical_max_bits": 1.0, "ratio_to_max": entropy,
               "null": "Bernoulli probability equals 0.5; this is not an independence test"}
    tests = {"runs": runs, "transition_first_order": first,
             "transition_second_order": second, "entropy_vs_max": balance,
             "ljung_box": ljung}
    tests.update({f"autocorrelation_lag_{row['lag']}": row for row in acf})
    _holm(tests, alpha)
    failed = [name for name, row in tests.items() if not row["pass"]]
    return {"sample_size": len(values), "positive_rate": rate, "segments": len(segments),
            "gap_boundaries": len(segments) - 1, "max_lag": max_lag, "alpha": alpha,
            "multiple_testing": "Holm-Bonferroni across all reported p-values",
            "overall_verdict": "REJECT_SOME_NULLS" if failed else "NO_REJECTION_AT_ALPHA",
            "failed_tests": failed, "tests": tests,
            "caveat": "Non-rejection is not proof of randomness; balance versus 0.5 is a separate assumption."}


def exact_repeat_diagnostic(multipliers: np.ndarray, starts: np.ndarray, max_lag: int = 50) -> dict:
    """Descriptive capture-artifact check; no post-hoc significance claims."""
    multipliers = np.asarray(multipliers, dtype=float)
    segment_id = np.cumsum(np.asarray(starts, dtype=bool))
    rows = []
    for lag in range(1, min(max_lag, len(multipliers) - 1) + 1):
        valid = segment_id[lag:] == segment_id[:-lag]
        same = (multipliers[lag:] == multipliers[:-lag]) & valid
        length = longest = 0
        for hit in same:
            length = length + 1 if hit else 0
            longest = max(longest, length)
        pairs = int(valid.sum())
        rows.append({"lag": lag, "exact_matches": int(same.sum()), "pairs": pairs,
                     "fraction": float(same.sum() / pairs) if pairs else None,
                     "longest_consecutive_matches": longest})
    return {"by_lag": rows,
            "highest_fraction": max((row for row in rows if row["fraction"] is not None),
                                    key=lambda row: row["fraction"], default=None),
            "note": "Exact multiplier repeats may indicate collector replay; this is a post-hoc diagnostic."}


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def summary(report: dict) -> str:
    lines = [f"Raw-outcome randomness battery: {report['model_version']}",
             f"N={report['sample_size']}; >=2x rate={report['positive_rate']:.6f}; "
             f"segments={report['segments']}; gap boundaries={report['gap_boundaries']}",
             f"Holm-Bonferroni family alpha={report['alpha']}; verdict={report['overall_verdict']}"]
    for name in ("runs", "transition_first_order", "transition_second_order", "entropy_vs_max", "ljung_box"):
        row = report["tests"][name]
        lines.append(f"{name}: statistic={row['statistic']:.6f}, p={row['p_value']:.6g}, "
                     f"Holm p={row['holm_adjusted_p']:.6g}, pass={row['pass']}")
    significant_lags = [row for name, row in report["tests"].items()
                        if name.startswith("autocorrelation_lag_") and not row["pass"]]
    lines.append("Significant serial lags after correction: " +
                 (", ".join(str(row["lag"]) for row in significant_lags) or "none"))
    repeat = (report.get("capture_diagnostic") or {}).get("highest_fraction")
    if repeat:
        lines.append(f"Largest exact multiplier-repeat fraction: lag {repeat['lag']}, "
                     f"{repeat['exact_matches']}/{repeat['pairs']} ({repeat['fraction']:.4%}); "
                     f"longest run {repeat['longest_consecutive_matches']} (post-hoc diagnostic).")
    lines.append("Non-rejection does not prove independence; rejection may reflect collection artifacts.")
    return "\n".join(lines) + "\n"


def main() -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Gap-aware binary-outcome randomness battery")
    parser.add_argument("--model-dir", type=Path, default=settings.model_dir)
    parser.add_argument("--version")
    parser.add_argument("--data", type=Path, help="history snapshot; defaults to archived training data")
    parser.add_argument("--alpha", type=float, default=.05)
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
    rounds, quality, _ = service.load_validate()
    if rounds.empty:
        raise ValueError("no valid rounds")
    seconds = rounds["timestamp_dt"].diff().dt.total_seconds()
    starts = ((seconds < 0) | (seconds > 120) | (rounds["round_index"].diff() != 1)).fillna(False).to_numpy(bool).copy()
    starts[0] = True
    report = battery((rounds["multiplier"].to_numpy(float) >= 2).astype(int), starts=starts, alpha=args.alpha)
    report.update({"model_version": metadata["model_version"], "source_sha256": metadata["source_sha256"],
                   "raw_data_quality": quality,
                   "capture_diagnostic": exact_repeat_diagnostic(rounds["multiplier"].to_numpy(float), starts)})
    output = version_dir / "randomness"
    _atomic_text(output / "report.json", json.dumps(report, indent=2) + "\n")
    _atomic_text(output / "summary.txt", summary(report))
    print(summary(report), end="")


if __name__ == "__main__":
    main()
