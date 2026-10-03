from __future__ import annotations

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


def _dtw_batch(current: np.ndarray, candidates: np.ndarray) -> np.ndarray:
    """Compute all short DTW distances with nine array operations for 3 rounds."""
    count, size = candidates.shape
    cells = [[np.full(count, np.inf) for _ in range(size + 1)] for _ in range(size + 1)]
    cells[0][0] = np.zeros(count)
    for i in range(1, size + 1):
        for j in range(1, size + 1):
            cells[i][j] = (np.abs(current[i - 1] - candidates[:, j - 1])
                           + np.minimum(np.minimum(cells[i - 1][j], cells[i][j - 1]), cells[i - 1][j - 1]))
    return cells[size][size]


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
        values_array = np.asarray(values, dtype=float)
        windows = np.lib.stride_tricks.sliding_window_view(values_array, sequence_length)[:-1]
        next_values = values_array[sequence_length:]
        centered = windows - windows.mean(axis=1, keepdims=True)
        deviations = windows.std(axis=1)
        normalized = np.divide(centered, deviations[:, None],
                               out=np.zeros_like(centered), where=deviations[:, None] != 0)
        euclidean_distances = np.linalg.norm(normalized - current_norm, axis=1)
        current_length = np.linalg.norm(current_norm)
        candidate_lengths = np.linalg.norm(normalized, axis=1)
        denominators = candidate_lengths * current_length
        cosine_scores = np.divide(normalized @ current_norm, denominators,
                                  out=np.zeros(len(windows)), where=denominators != 0)
        dtw_distances = _dtw_batch(current_norm, normalized)
        scores = (0.45 / (1 + euclidean_distances)
                  + 0.35 * ((cosine_scores + 1) / 2)
                  + 0.20 / (1 + dtw_distances))
        round_indices = rounds["round_index"].to_numpy()[sequence_length - 1:-1]
        cases = []
        exact_success = exact_total = 0
        for idx, seq in enumerate(windows):
            next_value = next_values[idx]
            exact = all(round(a, 2) == round(b, 2) for a, b in zip(seq, current))
            if exact:
                exact_total += 1
                exact_success += int(next_value >= self.target)
            euclidean = float(euclidean_distances[idx])
            cosine = float(cosine_scores[idx])
            dtw = float(dtw_distances[idx])
            score = float(scores[idx])
            cases.append(
                {
                    "round_index": int(round_indices[idx]),
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
