from __future__ import annotations

import pandas as pd

from app.services.feature_engineering import current_streak, volatility_label


def _pattern_row(pattern_id: str, label: str, occurrences: int, success: int, target: float, min_sample_size: int) -> dict:
    failure = occurrences - success
    probability = success / occurrences if occurrences else None
    return {
        "pattern_id": pattern_id,
        "pattern": label,
        "target": f">= {target:.2f}x",
        "occurrences": occurrences,
        "success_count": success,
        "failure_count": failure,
        "probability": probability,
        "rate_percent": round(probability * 100, 2) if probability is not None else None,
        "sufficient_data": occurrences >= min_sample_size,
    }


class PatternEngine:
    def __init__(self, target: float = 2.0, min_sample_size: int = 30):
        self.target = target
        self.min_sample_size = min_sample_size

    def discover(self, rounds: pd.DataFrame) -> list[dict]:
        values = rounds["multiplier"].astype(float).tolist()
        patterns: list[dict] = []

        for streak in range(1, 7):
            occurrences = success = 0
            for idx in range(streak - 1, len(values) - 1):
                recent = values[idx - streak + 1 : idx + 1]
                if all(v < self.target for v in recent):
                    if streak < 6 or (idx - streak < 0 or values[idx - streak] >= self.target):
                        occurrences += 1
                        success += int(values[idx + 1] >= self.target)
            label = f"{streak if streak < 6 else '6+'} consecutive rounds below {self.target:.2f}x"
            patterns.append(_pattern_row(f"low_streak_{streak if streak < 6 else '6_plus'}", label, occurrences, success, self.target, self.min_sample_size))

        for streak in range(2, 6):
            occurrences = success = 0
            for idx in range(streak - 1, len(values) - 1):
                if all(v >= self.target for v in values[idx - streak + 1 : idx + 1]):
                    occurrences += 1
                    success += int(values[idx + 1] >= self.target)
            patterns.append(_pattern_row(f"high_streak_{streak}", f"{streak} consecutive rounds >= {self.target:.2f}x", occurrences, success, self.target, self.min_sample_size))

        for window in (10, 25):
            for label in ("LOW", "MEDIUM", "HIGH"):
                occurrences = success = 0
                for idx in range(window - 1, len(values) - 1):
                    recent = values[idx - window + 1 : idx + 1]
                    if volatility_label(float(pd.Series(recent).std(ddof=0))) == label:
                        occurrences += 1
                        success += int(values[idx + 1] >= self.target)
                patterns.append(_pattern_row(f"volatility_{window}_{label.lower()}", f"{label.lower()} volatility over last {window} rounds", occurrences, success, self.target, self.min_sample_size))

        return patterns

    def current_pattern(self, rounds: pd.DataFrame) -> dict:
        values = rounds["multiplier"].astype(float).tolist()
        low = current_streak(values, self.target, True)
        high = current_streak(values, self.target, False)
        if low > 0:
            pattern_id = f"low_streak_{low if low < 6 else '6_plus'}"
            label = f"{low if low < 6 else '6+'} consecutive rounds below {self.target:.2f}x"
        elif high > 0:
            pattern_id = f"high_streak_{min(high, 5)}"
            label = f"{high} consecutive rounds >= {self.target:.2f}x"
        else:
            pattern_id = "none"
            label = "No active streak pattern"
        return {"pattern_id": pattern_id, "label": label, "low_streak": low, "high_streak": high}

    def conditional_for_current(self, rounds: pd.DataFrame) -> dict:
        patterns = {p["pattern_id"]: p for p in self.discover(rounds)}
        current = self.current_pattern(rounds)
        evidence = patterns.get(current["pattern_id"])
        if not evidence:
            evidence = _pattern_row(current["pattern_id"], current["label"], 0, 0, self.target, self.min_sample_size)
        return {**current, **evidence}

