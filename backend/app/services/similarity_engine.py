from __future__ import annotations

import math
from statistics import mean, pstdev

import numpy as np
import pandas as pd


def _normalize(seq: list[float]) -> np.ndarray:
    arr = np.array(seq, dtype=float)
    std = float(arr.std())
    if std == 0:
        return arr - arr.mean()
    return (arr - arr.mean()) / std


def _cosine(a: np.ndarray, b: np.ndarray) -> float:
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return 0.0 if denom == 0 else float(np.dot(a, b) / denom)


def _dtw(a: list[float], b: list[float]) -> float:
    n, m = len(a), len(b)
    dp = np.full((n + 1, m + 1), np.inf)
    dp[0, 0] = 0.0
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            cost = abs(a[i - 1] - b[j - 1])
            dp[i, j] = cost + min(dp[i - 1, j], dp[i, j - 1], dp[i - 1, j - 1])
    return float(dp[n, m])


class SimilarityEngine:
    def __init__(self, target: float = 2.0, min_sample_size: int = 30):
        self.target = target
        self.min_sample_size = min_sample_size

    def find_similar(self, rounds: pd.DataFrame, sequence_length: int = 3, limit: int = 25) -> dict:
        values = rounds["multiplier"].astype(float).tolist()
        if len(values) <= sequence_length + 1:
            return self._empty(sequence_length)

        current = values[-sequence_length:]
        current_norm = _normalize(current)
        cases = []
        exact_success = exact_total = 0
        for idx in range(0, len(values) - sequence_length):
            seq = values[idx : idx + sequence_length]
            if idx + sequence_length >= len(values):
                continue
            next_value = values[idx + sequence_length]
            exact = all(round(a, 2) == round(b, 2) for a, b in zip(seq, current))
            if exact:
                exact_total += 1
                exact_success += int(next_value >= self.target)
            seq_norm = _normalize(seq)
            euclidean = float(np.linalg.norm(current_norm - seq_norm))
            cosine = _cosine(current_norm, seq_norm)
            dtw = _dtw(current_norm.tolist(), seq_norm.tolist())
            score = (1 / (1 + euclidean)) * 0.45 + ((cosine + 1) / 2) * 0.35 + (1 / (1 + dtw)) * 0.20
            cases.append(
                {
                    "round_index": int(rounds["round_index"].iloc[idx + sequence_length - 1]),
                    "sequence": [round(float(v), 2) for v in seq],
                    "next_multiplier": round(float(next_value), 2),
                    "next_target": int(next_value >= self.target),
                    "similarity": round(score, 4),
                    "euclidean": round(euclidean, 4),
                    "cosine": round(cosine, 4),
                    "dtw": round(dtw, 4),
                }
            )

        cases.sort(key=lambda row: row["similarity"], reverse=True)
        selected = cases[: max(limit, self.min_sample_size)]
        successes = sum(c["next_target"] for c in selected)
        total = len(selected)
        probability = successes / total if total else None
        return {
            "current_sequence": [round(float(v), 2) for v in current],
            "sequence_length": sequence_length,
            "similar_cases": total,
            "success_count": int(successes),
            "failure_count": int(total - successes),
            "probability": probability,
            "average_similarity": mean([c["similarity"] for c in selected]) if selected else None,
            "sufficient_data": total >= self.min_sample_size,
            "exact_matches": exact_total,
            "exact_success_count": exact_success,
            "exact_probability": exact_success / exact_total if exact_total else None,
            "cases": cases[:limit],
        }

    def _empty(self, sequence_length: int) -> dict:
        return {
            "current_sequence": [],
            "sequence_length": sequence_length,
            "similar_cases": 0,
            "success_count": 0,
            "failure_count": 0,
            "probability": None,
            "average_similarity": None,
            "sufficient_data": False,
            "exact_matches": 0,
            "cases": [],
        }

