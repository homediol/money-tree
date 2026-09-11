from __future__ import annotations

import numpy as np
import pandas as pd


WINDOWS = (3, 5, 10, 25, 50)


def current_streak(values: list[float], target: float = 2.0, below: bool = True) -> int:
    count = 0
    for value in reversed(values):
        if (value < target) if below else (value >= target):
            count += 1
        else:
            break
    return count


def longest_streak(values: list[float], target: float = 2.0, below: bool = True) -> int:
    best = run = 0
    for value in values:
        ok = (value < target) if below else (value >= target)
        run = run + 1 if ok else 0
        best = max(best, run)
    return best


def volatility_label(std: float) -> str:
    if std < 0.75:
        return "LOW"
    if std < 2.0:
        return "MEDIUM"
    return "HIGH"


def build_current_features(values: list[float], target: float = 2.0) -> dict:
    arr = np.array(values, dtype=float)
    features: dict[str, float | int | str] = {
        "count": int(len(arr)),
        "current_low_streak": current_streak(values, target, True),
        "current_high_streak": current_streak(values, target, False),
        "longest_recent_low_streak": longest_streak(values[-50:], target, True),
        "longest_recent_high_streak": longest_streak(values[-50:], target, False),
    }
    for window in WINDOWS:
        win = arr[-window:] if len(arr) >= window else arr
        prefix = f"last_{window}"
        if len(win) == 0:
            continue
        features.update(
            {
                f"{prefix}_mean": float(np.mean(win)),
                f"{prefix}_median": float(np.median(win)),
                f"{prefix}_min": float(np.min(win)),
                f"{prefix}_max": float(np.max(win)),
                f"{prefix}_std": float(np.std(win)),
                f"{prefix}_var": float(np.var(win)),
                f"{prefix}_range": float(np.max(win) - np.min(win)),
                f"{prefix}_q25": float(np.quantile(win, 0.25)),
                f"{prefix}_q75": float(np.quantile(win, 0.75)),
                f"{prefix}_pct_above_target": float(np.mean(win >= target)),
                f"{prefix}_pct_below_target": float(np.mean(win < target)),
                f"{prefix}_pct_below_1_2": float(np.mean(win < 1.2)),
                f"{prefix}_pct_below_1_5": float(np.mean(win < 1.5)),
                f"{prefix}_pct_between_2_5": float(np.mean((win >= 2.0) & (win < 5.0))),
                f"{prefix}_pct_above_5": float(np.mean(win >= 5.0)),
            }
        )
    std25 = float(features.get("last_25_std", features.get("last_10_std", 0.0)))
    features["volatility"] = volatility_label(std25)
    return features


def build_supervised_frame(rounds: pd.DataFrame, target: float = 2.0) -> pd.DataFrame:
    values = rounds["multiplier"].astype(float).tolist()
    rows = []
    for idx in range(50, len(values) - 1):
        feats = build_current_features(values[: idx + 1], target)
        numeric = {k: v for k, v in feats.items() if isinstance(v, (int, float))}
        numeric["next_target"] = int(values[idx + 1] >= target)
        numeric["round_index"] = int(rounds["round_index"].iloc[idx])
        rows.append(numeric)
    return pd.DataFrame(rows).replace([np.inf, -np.inf], np.nan).dropna()

