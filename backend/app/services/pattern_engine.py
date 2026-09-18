"""Historical pattern evidence only; no next-round prediction or decisions."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

import pandas as pd

from app.services.dataset_service import BUCKETS, multiplier_bucket

Mask = Callable[[list[float], int], bool]
SUPPORTED_TARGETS = (1.5, 2.0, 3.0, 5.0, 10.0)
STREAK_THRESHOLDS = (1.2, 1.5, 2.0, 3.0)
RECENT_WINDOWS = (1000, 250)


@dataclass(frozen=True)
class EvidenceRules:
    min_sample_size: int = 30
    train_fraction: float = 0.70
    stable_rate_tolerance: float = 0.10


def wilson_interval(successes: int, n: int, z: float = 1.959963984540054) -> dict:
    if n <= 0:
        return {"lower": None, "upper": None, "confidence": 0.95, "n": 0}
    rate = successes / n
    denominator = 1 + z * z / n
    center = (rate + z * z / (2 * n)) / denominator
    margin = z * math.sqrt(rate * (1 - rate) / n + z * z / (4 * n * n)) / denominator
    return {"lower": max(0.0, center - margin), "upper": min(1.0, center + margin), "confidence": 0.95, "n": n}


def _rate(successes: int, n: int) -> float | None:
    return successes / n if n else None


def _comparison(rate: float | None, baseline: float | None) -> tuple[float | None, float | None]:
    if rate is None or baseline is None:
        return None, None
    difference = rate - baseline
    return difference, difference / baseline if baseline else None


def _streak_before(values: list[float], target_index: int, threshold: float) -> int:
    length = 0
    for index in range(target_index - 1, -1, -1):
        if values[index] >= threshold:
            break
        length += 1
    return length


def classify_stability(train: dict, test: dict, rules: EvidenceRules) -> str:
    if train["sample_size"] < rules.min_sample_size or test["sample_size"] < rules.min_sample_size:
        return "INSUFFICIENT_DATA"
    train_rate, test_rate = train["success_rate"], test["success_rate"]
    train_ci, test_ci = train["confidence_interval"], test["confidence_interval"]
    overlaps = train_ci["lower"] <= test_ci["upper"] and test_ci["lower"] <= train_ci["upper"]
    if abs(train_rate - test_rate) <= rules.stable_rate_tolerance and overlaps:
        return "STABLE"
    if abs(train["difference_from_baseline"] or 0) < 0.02 and abs(test["difference_from_baseline"] or 0) < 0.02:
        return "WEAK"
    return "VARIABLE"


class PatternEngine:
    def __init__(self, target: float = 2.0, min_sample_size: int = 30, *, train_fraction: float = 0.70):
        if target <= 0:
            raise ValueError("target must be positive")
        self.target = float(target)
        self.rules = EvidenceRules(min_sample_size=min_sample_size, train_fraction=train_fraction)
        self.min_sample_size = min_sample_size

    def baseline(self, rounds: pd.DataFrame, target: float | None = None) -> dict:
        target = float(target or self.target)
        values = rounds["multiplier"].astype(float).tolist() if not rounds.empty else []
        successes, n = sum(value >= target for value in values), len(values)
        return {"target": f">= {target:.2f}x", "sample_size": n, "successes": successes,
                "failures": n - successes, "rate": _rate(successes, n),
                "confidence_interval": wilson_interval(successes, n)}

    def _measure(self, values: list[float], mask: Mask, start: int = 0, end: int | None = None) -> dict:
        end = len(values) if end is None else min(end, len(values))
        indices = [index for index in range(max(0, start), end) if mask(values, index)]
        successes, n = sum(values[index] >= self.target for index in indices), len(indices)
        return {"sample_size": n, "next_outcomes_observed": n, "successes": successes,
                "failures": n - successes, "success_rate": _rate(successes, n),
                "failure_rate": _rate(n - successes, n), "confidence_interval": wilson_interval(successes, n)}

    def _evidence(self, *, pattern_id: str, pattern: str, kind: str, definition: dict,
                  values: list[float], mask: Mask, timestamps: list[str] | None = None) -> dict:
        baseline = _rate(sum(value >= self.target for value in values), len(values))
        full = self._measure(values, mask)
        difference, relative = _comparison(full["success_rate"], baseline)
        recent: dict[str, dict] = {}
        for window in RECENT_WINDOWS:
            start = max(0, len(values) - window)
            measured = self._measure(values, mask, start=start)
            segment = values[start:]
            measured["baseline_rate"] = _rate(sum(value >= self.target for value in segment), len(segment))
            measured["difference_from_baseline"], measured["relative_difference"] = _comparison(measured["success_rate"], measured["baseline_rate"])
            recent[str(window)] = measured
        split = max(1, min(len(values) - 1, int(len(values) * self.rules.train_fraction))) if len(values) > 1 else 0
        train, test = self._measure(values, mask, end=split), self._measure(values, mask, start=split)
        for measured, segment in ((train, values[:split]), (test, values[split:])):
            measured["baseline_rate"] = _rate(sum(value >= self.target for value in segment), len(segment))
            measured["difference_from_baseline"], measured["relative_difference"] = _comparison(measured["success_rate"], measured["baseline_rate"])
        stability = classify_stability(train, test, self.rules)
        state = "SUFFICIENT_DATA" if full["sample_size"] >= self.min_sample_size else "INSUFFICIENT_DATA"
        score, components = self._rank_score(full, difference, recent["250"], test, stability)
        ci = full["confidence_interval"]
        return {
            "pattern_id": pattern_id, "pattern": pattern, "kind": kind, "definition": definition,
            "target": f">= {self.target:.2f}x", **full,
            "occurrences": full["sample_size"], "success_count": full["successes"],
            "failure_count": full["failures"], "probability": full["success_rate"],
            "rate_percent": round(full["success_rate"] * 100, 2) if full["success_rate"] is not None else None,
            "sufficient_data": state == "SUFFICIENT_DATA", "evidence_state": state,
            "baseline_rate": baseline, "difference_from_baseline": difference,
            "percentage_point_difference": difference * 100 if difference is not None else None,
            "relative_difference": relative, "confidence_interval": ci,
            "recent_rate": recent["250"]["success_rate"], "recent": recent,
            "train": train, "test": test, "out_of_sample_rate": test["success_rate"],
            "stability": stability, "rank_score": score, "rank_components": components,
            "data_period": {"start": timestamps[0] if timestamps else None, "end": timestamps[-1] if timestamps else None,
                            "total_rounds": len(values), "train_end_index": split - 1, "test_start_index": split},
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "explanation": self._explain(pattern, full, baseline, difference, recent["250"], test, stability),
        }

    def _rank_score(self, full: dict, difference: float | None, recent: dict, test: dict, stability: str) -> tuple[float, dict]:
        sample = min(1.0, full["sample_size"] / max(1, self.min_sample_size * 3))
        effect = min(1.0, abs(difference or 0) / 0.20)
        ci = full["confidence_interval"]
        precision = 0.0 if ci["lower"] is None else max(0.0, 1.0 - (ci["upper"] - ci["lower"]))
        oos = 0.0 if full["success_rate"] is None or test["success_rate"] is None else max(0.0, 1.0 - abs(full["success_rate"] - test["success_rate"]))
        current = 0.0 if full["success_rate"] is None or recent["success_rate"] is None else max(0.0, 1.0 - abs(full["success_rate"] - recent["success_rate"]))
        stable = {"STABLE": 1.0, "WEAK": 0.55, "VARIABLE": 0.35, "INSUFFICIENT_DATA": 0.10}[stability]
        parts = {"sample_size": sample, "baseline_effect": effect, "precision": precision,
                 "out_of_sample_consistency": oos, "recent_consistency": current, "stability": stable}
        score = 100 * (0.25 * sample + 0.20 * effect + 0.15 * precision + 0.15 * oos + 0.10 * current + 0.15 * stable)
        return round(score, 4), parts

    @staticmethod
    def _explain(pattern: str, full: dict, baseline: float | None, difference: float | None,
                 recent: dict, test: dict, stability: str) -> str:
        pct = lambda value: "N/A" if value is None else f"{value * 100:.2f}%"
        ci = full["confidence_interval"]
        interval = "N/A" if ci["lower"] is None else f"{pct(ci['lower'])}–{pct(ci['upper'])}"
        diff = "N/A" if difference is None else f"{difference * 100:+.2f} percentage points"
        return (f"{pattern} occurred {full['sample_size']} times. The following outcome historically met the target "
                f"{full['successes']} times ({pct(full['success_rate'])}; Wilson 95% CI {interval}), versus a "
                f"{pct(baseline)} baseline ({diff}). Recent-250 rate: {pct(recent['success_rate'])}. "
                f"Out-of-sample: {pct(test['success_rate'])} (N={test['sample_size']}). Stability: {stability}. "
                "This is historical evidence, not a next-round prediction.")

    def streak_patterns(self, rounds: pd.DataFrame, thresholds: Iterable[float] = STREAK_THRESHOLDS) -> list[dict]:
        values = rounds["multiplier"].astype(float).tolist()
        timestamps = rounds.get("timestamp", pd.Series(dtype=str)).astype(str).tolist() or None
        evidence = []
        for threshold in thresholds:
            for length in range(1, 7):
                is_plus = length == 6
                def mask(vals: list[float], index: int, t=threshold, size=length, plus=is_plus) -> bool:
                    streak = _streak_before(vals, index, t) if index > 0 else 0
                    return streak >= size if plus else streak == size
                label_length = "6+" if is_plus else str(length)
                evidence.append(self._evidence(
                    pattern_id=f"streak_below_{str(threshold).replace('.', '_')}_{'6_plus' if is_plus else length}_target_{str(self.target).replace('.', '_')}",
                    pattern=f"{label_length} consecutive prior rounds below {threshold:.2f}x", kind="streak",
                    definition={"threshold": threshold, "streak_length": label_length}, values=values, mask=mask, timestamps=timestamps))
        return evidence

    def sequence_patterns(self, rounds: pd.DataFrame, lengths: Iterable[int] = (2, 3, 4, 5)) -> list[dict]:
        values = rounds["multiplier"].astype(float).tolist()
        buckets = [multiplier_bucket(value) for value in values]
        timestamps = rounds.get("timestamp", pd.Series(dtype=str)).astype(str).tolist() or None
        evidence = []
        # Candidate sequences are discovered in the chronological train
        # period only, then the identical definitions are measured later.
        discovery_end = max(0, int(len(values) * self.rules.train_fraction))
        for length in lengths:
            observed = sorted({tuple(buckets[index - length:index]) for index in range(length, discovery_end)})
            for sequence in observed:
                def mask(_vals: list[float], index: int, seq=sequence, size=length) -> bool:
                    return index >= size and tuple(buckets[index - size:index]) == seq
                slug = "_".join(value.lower() for value in sequence)
                evidence.append(self._evidence(
                    pattern_id=f"sequence_{length}_{slug}_target_{str(self.target).replace('.', '_')}",
                    pattern=" → ".join(sequence), kind="sequence",
                    definition={"length": length, "sequence": list(sequence), "buckets": BUCKETS},
                    values=values, mask=mask, timestamps=timestamps))
        return evidence

    def discover(self, rounds: pd.DataFrame) -> list[dict]:
        return sorted(self.streak_patterns(rounds) + self.sequence_patterns(rounds),
                      key=lambda item: (-item["rank_score"], -item["sample_size"], item["pattern_id"]))

    def report(self, rounds: pd.DataFrame) -> dict:
        return {"target": f">= {self.target:.2f}x", "baseline": self.baseline(rounds),
                "minimum_sample_size": self.min_sample_size, "bucket_definitions": BUCKETS,
                "ranking_method": {"sample_size": 0.25, "baseline_effect": 0.20, "precision": 0.15,
                                   "out_of_sample_consistency": 0.15, "recent_consistency": 0.10, "stability": 0.15},
                "patterns": self.discover(rounds), "generated_at": datetime.now(timezone.utc).isoformat()}

    def current_pattern(self, rounds: pd.DataFrame) -> dict:
        values = rounds["multiplier"].astype(float).tolist()
        streak = _streak_before(values, len(values), self.target)
        label = "6+" if streak >= 6 else str(streak)
        pattern_id = f"streak_below_{str(self.target).replace('.', '_')}_{'6_plus' if streak >= 6 else streak}_target_{str(self.target).replace('.', '_')}"
        return {"pattern_id": pattern_id, "label": f"{label} consecutive prior rounds below {self.target:.2f}x", "low_streak": streak, "high_streak": 0}

    def conditional_for_current(self, rounds: pd.DataFrame) -> dict:
        current = self.current_pattern(rounds)
        evidence = next((item for item in self.streak_patterns(rounds, (self.target,)) if item["pattern_id"] == current["pattern_id"]), None)
        return {**current, **(evidence or {})}
