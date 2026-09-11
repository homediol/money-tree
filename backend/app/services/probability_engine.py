from __future__ import annotations

import pandas as pd


class ProbabilityEngine:
    def __init__(self, target: float = 2.0, min_sample_size: int = 30):
        self.target = target
        self.min_sample_size = min_sample_size

    def base_rate(self, rounds: pd.DataFrame) -> dict:
        if rounds.empty:
            return {"sample_size": 0, "success_count": 0, "failure_count": 0, "probability": None}
        target_series = rounds["multiplier"].astype(float) >= self.target
        success = int(target_series.sum())
        total = int(len(target_series))
        return {
            "sample_size": total,
            "success_count": success,
            "failure_count": total - success,
            "probability": success / total if total else None,
            "sufficient_data": total >= self.min_sample_size,
        }

    def conditional_summary(self, occurrences: int, success: int) -> dict:
        failure = max(occurrences - success, 0)
        probability = success / occurrences if occurrences else None
        return {
            "sample_size": occurrences,
            "success_count": success,
            "failure_count": failure,
            "probability": probability,
            "sufficient_data": occurrences >= self.min_sample_size,
            "warning": None if occurrences >= self.min_sample_size else "INSUFFICIENT DATA",
        }

