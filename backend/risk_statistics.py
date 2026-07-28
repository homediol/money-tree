"""
statistics.py
Statistical analysis of round history with extension points for:

  • TensorFlow / Keras machine-learning models
  • Risk-management system (stop-loss, volatility estimation)
  • prediction.json  /  decisions.json  outputs
  • Playwright bot integration hooks
"""

from __future__ import annotations

import math
import json
from pathlib import Path
from typing import Dict, List, Optional, Any

from utils import load_round_history


# ── Risk / ML output paths ────────────────────────────────────────────────

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
PREDICTION_FILE = DATA_DIR / "prediction.json"
DECISIONS_FILE = DATA_DIR / "decisions.json"


# ── Basic statistics ──────────────────────────────────────────────────────

def compute_stats(rounds: Optional[List[dict]] = None) -> dict:
    """
    Return a dictionary of summary statistics from the provided rounds
    (or from the global store if ``None``).
    """
    if rounds is None:
        rounds = load_round_history()

    if not rounds:
        return {
            "total_rounds": 0,
            "mean_multiplier": 0.0,
            "median_multiplier": 0.0,
            "min_multiplier": 0.0,
            "max_multiplier": 0.0,
            "std_multiplier": 0.0,
            "p_above_2x": 0.0,
            "p_above_10x": 0.0,
            "p_above_50x": 0.0,
            "p_above_100x": 0.0,
            "histogram_1_2": 0.0,
            "histogram_2_10": 0.0,
            "histogram_10_50": 0.0,
            "histogram_50_plus": 0.0,
            "mean_duration": 0.0,
        }

    multipliers = [r["multiplier"] for r in rounds]
    durations = [r.get("duration", 0.0) for r in rounds]  # duration is optional
    n = len(multipliers)

    # ── Central tendency ────────────────────────────────────────────
    mean_m = sum(multipliers) / n
    sorted_m = sorted(multipliers)
    median_m = sorted_m[n // 2] if n % 2 else (sorted_m[n // 2 - 1] + sorted_m[n // 2]) / 2

    # ── Spread ──────────────────────────────────────────────────────
    variance = sum((x - mean_m) ** 2 for x in multipliers) / n
    std_m = math.sqrt(variance)

    # ── Tail probabilities (should approximate 1 % house edge) ──────
    p_above_2x = sum(1 for m in multipliers if m > 2.0) / n * 100
    p_above_10x = sum(1 for m in multipliers if m > 10.0) / n * 100
    p_above_50x = sum(1 for m in multipliers if m > 50.0) / n * 100
    p_above_100x = sum(1 for m in multipliers if m > 100.0) / n * 100

    # ── Histogram bins ──────────────────────────────────────────────
    h_1_2 = sum(1 for m in multipliers if 1.0 <= m <= 2.0) / n * 100
    h_2_10 = sum(1 for m in multipliers if 2.0 < m <= 10.0) / n * 100
    h_10_50 = sum(1 for m in multipliers if 10.0 < m <= 50.0) / n * 100
    h_50_plus = sum(1 for m in multipliers if m > 50.0) / n * 100

    mean_d = sum(durations) / n if durations else 0.0

    # ── Consecutive-round streak info ───────────────────────────────
    high_streak = 0
    current_high = 0
    low_streak = 0
    current_low = 0
    for m in multipliers:
        if m > 2.0:
            current_high += 1
            current_low = 0
            high_streak = max(high_streak, current_high)
        else:
            current_low += 1
            current_high = 0
            low_streak = max(low_streak, current_low)

    return {
        "total_rounds": n,
        "mean_multiplier": round(mean_m, 4),
        "median_multiplier": round(median_m, 4),
        "min_multiplier": round(min(multipliers), 2),
        "max_multiplier": round(max(multipliers), 2),
        "std_multiplier": round(std_m, 4),
        "p_above_2x": round(p_above_2x, 2),
        "p_above_10x": round(p_above_10x, 2),
        "p_above_50x": round(p_above_50x, 2),
        "p_above_100x": round(p_above_100x, 2),
        "histogram_1_2": round(h_1_2, 2),
        "histogram_2_10": round(h_2_10, 2),
        "histogram_10_50": round(h_10_50, 2),
        "histogram_50_plus": round(h_50_plus, 2),
        "mean_duration": round(mean_d, 2),
        "max_high_streak": high_streak,
        "max_low_streak": low_streak,
    }


# ── ML / Risk integration points ─────────────────────────────────────────

def prepare_ml_features(rounds: Optional[List[dict]] = None) -> list:
    """
    Build a feature matrix suitable for TensorFlow / Keras training.

    Each row contains features derived from the previous *N* rounds:
        [mean_m, std_m, max_m, min_m, streak_above_2, time_since_last, ...]

    Extend this method with your own engineered features.
    """
    if rounds is None:
        rounds = load_round_history()
    if not rounds:
        return []

    features = []
    for i in range(1, len(rounds)):
        prev = rounds[:i]
        mults = [r["multiplier"] for r in prev[-50:]]  # last 50 rounds
        if not mults:
            continue

        recent = prev[-10:]
        recent_mults = [r["multiplier"] for r in recent]

        f = {
            "mean_last_50": sum(mults) / len(mults),
            "max_last_50": max(mults),
            "std_last_50": (
                (sum((x - sum(mults) / len(mults)) ** 2 for x in mults) / len(mults)) ** 0.5
            ),
            "mean_last_10": sum(recent_mults) / len(recent_mults) if recent_mults else 0,
            "max_last_10": max(recent_mults) if recent_mults else 0,
            "streak_above_2": _streak_len(prev, "high"),
            "streak_below_2": _streak_len(prev, "low"),
            "target": rounds[i]["multiplier"],
        }
        features.append(f)

    return features


def _streak_len(rounds: list, kind: str = "high") -> int:
    """Length of the most recent streak of high (>2×) or low (≤2×) rounds."""
    count = 0
    for r in reversed(rounds):
        if kind == "high" and r["multiplier"] > 2.0:
            count += 1
        elif kind == "low" and r["multiplier"] <= 2.0:
            count += 1
        else:
            break
    return count


# ── Prediction / Decision output ─────────────────────────────────────────

def write_prediction(prediction: dict) -> None:
    """
    Write a prediction payload to ``data/prediction.json``.

    Expected shape::

        {
            "round_id": int,
            "predicted_multiplier": float,
            "confidence": float,
            "model": "lstm_v1" | ...
        }
    """
    PREDICTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    PREDICTION_FILE.write_text(
        json.dumps(prediction, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def write_decisions(decisions: dict) -> None:
    """
    Write risk-management decisions to ``data/decisions.json``.

    Expected shape::

        {
            "action": "bet" | "skip" | "cash_out",
            "round_id": int,
            "cash_out_at": float | None,
            "reason": str,
        }
    """
    DECISIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
    DECISIONS_FILE.write_text(
        json.dumps(decisions, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

