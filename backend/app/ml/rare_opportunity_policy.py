"""Development-only selection research for Frozen V3 rare opportunities.

This module never fits or changes V3. It evaluates score cutoffs against
strictly pre-cutoff, pre-outcome assessments and refuses to freeze a policy
when rare-tail evidence is too small or unstable.
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from datetime import datetime, timezone
from typing import Any

import numpy as np

POLICY_ID = "RARE_OPPORTUNITY_SELECTION_POLICY_V1"
POLICY_VERSION = "RareOpportunitySelectionPolicyV1"
DEVELOPMENT_CUTOFF_ROUND = 19929
TARGET_MULTIPLIER = 2.0
CANDIDATE_FREQUENCIES = (0.002, 0.004, 0.006, 0.008, 0.01, 0.02)
CHRONOLOGICAL_BLOCKS = 5
BOOTSTRAP_REPLICATES = 2000
BOOTSTRAP_BLOCK_SIZE = 50
MIN_SELECTED_DEVELOPMENT = 30
MAX_FREEZE_FREQUENCY = 0.01


def _wilson(successes: int, total: int, z: float = 1.959963984540054) -> dict[str, Any]:
    if total <= 0:
        return {"lower": None, "upper": None, "n": 0, "confidence": 0.95}
    p = successes / total
    denom = 1 + z * z / total
    center = (p + z * z / (2 * total)) / denom
    margin = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denom
    return {"lower": max(0.0, center - margin), "upper": min(1.0, center + margin),
            "n": total, "confidence": 0.95}


def _bootstrap_lift(rows: list[dict[str, Any]], selected: np.ndarray,
                    baseline: float, seed: int = 742021) -> dict[str, Any]:
    n = len(rows)
    if n == 0 or not selected.any():
        return {"lower": None, "upper": None, "replicates": 0, "method": "moving chronological block bootstrap"}
    labels = np.asarray([bool(row["is_true"]) for row in rows], dtype=bool)
    blocks = [np.arange(start, min(start + BOOTSTRAP_BLOCK_SIZE, n))
              for start in range(0, n, BOOTSTRAP_BLOCK_SIZE)]
    rng = np.random.default_rng(seed)
    estimates: list[float] = []
    for _ in range(BOOTSTRAP_REPLICATES):
        chosen: list[int] = []
        while len(chosen) < n:
            block = blocks[int(rng.integers(0, len(blocks)))]
            chosen.extend(block.tolist())
        sample = np.asarray(chosen[:n], dtype=int)
        sample_selected = selected[sample]
        if sample_selected.any():
            estimates.append(float(labels[sample][sample_selected].mean() - labels[sample].mean()))
    if not estimates:
        return {"lower": None, "upper": None, "replicates": 0,
                "method": "moving chronological block bootstrap"}
    lower, upper = np.quantile(np.asarray(estimates), [0.025, 0.975])
    return {"lower": float(lower), "upper": float(upper),
            "replicates": len(estimates), "method": "moving chronological block bootstrap",
            "baseline_reference": float(baseline)}


def _candidate_metrics(rows: list[dict[str, Any]], requested_frequency: float) -> dict[str, Any]:
    scores = np.asarray([float(row["score"]) for row in rows], dtype=float)
    labels = np.asarray([bool(row["is_true"]) for row in rows], dtype=bool)
    baseline = float(labels.mean()) if len(labels) else 0.0
    threshold = float(np.quantile(scores, 1.0 - requested_frequency, method="higher"))
    selected = scores >= threshold
    count = int(selected.sum())
    successes = int(labels[selected].sum()) if count else 0
    precision = successes / count if count else None
    lift = precision - baseline if precision is not None else None
    blocks = []
    positions = np.array_split(np.arange(len(rows)), CHRONOLOGICAL_BLOCKS)
    for number, indexes in enumerate(positions, 1):
        if not len(indexes):
            continue
        block_selected = selected[indexes]
        block_labels = labels[indexes]
        selected_count = int(block_selected.sum())
        selected_true = int(block_labels[block_selected].sum()) if selected_count else 0
        block_baseline = float(block_labels.mean())
        block_precision = selected_true / selected_count if selected_count else None
        blocks.append({"block": number, "rounds": int(len(indexes)),
                       "selected": selected_count, "true": selected_true,
                       "baseline": block_baseline, "precision": block_precision,
                       "lift": block_precision - block_baseline if block_precision is not None else None})
    selected_indices = np.flatnonzero(selected)
    gaps = np.diff(selected_indices)
    selected_scores = scores[selected]
    positive_blocks = sum(item["lift"] is not None and item["lift"] > 0 for item in blocks)
    bootstrap = _bootstrap_lift(rows, selected, baseline,
                               seed=742021 + int(requested_frequency * 1_000_000))
    return {
        "requested_frequency": requested_frequency,
        "threshold": threshold,
        "selected": count,
        "effective_frequency": count / len(rows) if rows else 0.0,
        "true": successes,
        "false": count - successes,
        "precision": precision,
        "wilson_95": _wilson(successes, count),
        "baseline_ge_2x": baseline,
        "absolute_lift": lift,
        "relative_lift": (lift / baseline if lift is not None and baseline else None),
        "bootstrap_lift_95": bootstrap,
        "chronological_blocks": blocks,
        "positive_lift_blocks": positive_blocks,
        "average_selection_gap_rounds": float(gaps.mean()) if len(gaps) else None,
        "maximum_selection_gap_rounds": int(gaps.max()) if len(gaps) else None,
        "selected_score_mean": float(selected_scores.mean()) if count else None,
        "selected_score_min": float(selected_scores.min()) if count else None,
        "selected_score_max": float(selected_scores.max()) if count else None,
        "selection_score_std": float(selected_scores.std(ddof=1)) if count > 1 else None,
    }


class RareOpportunitySelectionPolicyV1:
    """Research and apply a separate immutable V3 score/evidence gate."""

    @staticmethod
    def develop(rows: list[dict[str, Any]], *, model_version: str,
                model_hash: str, development_cutoff_round: int = DEVELOPMENT_CUTOFF_ROUND,
                created_at: str | None = None) -> dict[str, Any]:
        # Fail closed on any row at or beyond the development boundary or
        # lacking source→target pre-outcome order evidence supplied by caller.
        rows = [row for row in rows if int(row.get("target_round_index", 0)) <= development_cutoff_round
                and int(row.get("source_round_index", development_cutoff_round)) < development_cutoff_round
                and row.get("pre_outcome_verified") is True]
        rows.sort(key=lambda row: int(row["target_round_index"]))
        if rows and any(int(rows[i]["target_round_index"]) <= int(rows[i - 1]["target_round_index"])
                        for i in range(1, len(rows))):
            raise ValueError("development rows must have unique chronological target indices")
        candidate_results = ([_candidate_metrics(rows, frequency) for frequency in CANDIDATE_FREQUENCIES]
                             if rows else [])
        eligible = []
        for item in candidate_results:
            ci = item["bootstrap_lift_95"]
            if (item["effective_frequency"] <= MAX_FREEZE_FREQUENCY
                    and item["selected"] >= MIN_SELECTED_DEVELOPMENT
                    and (item["absolute_lift"] or 0) > 0
                    and ci.get("lower") is not None and ci["lower"] > 0
                    and item["positive_lift_blocks"] >= 4):
                eligible.append(item)
        selected = max(eligible, key=lambda item: (item["bootstrap_lift_95"]["lower"],
                                                   -item["effective_frequency"])) if eligible else None
        status = "FROZEN" if selected else "NO_RELIABLE_POLICY"
        now = created_at or datetime.now(timezone.utc).isoformat()
        if selected:
            scores = np.asarray([float(row["score"]) for row in rows], dtype=float)
            watch_threshold = float(np.quantile(scores, 0.90, method="higher"))
            rules = {
                "selection_threshold": selected["threshold"],
                "watch_threshold": min(watch_threshold, selected["threshold"]),
                "score_comparison": "score >= frozen selection_threshold",
                "evidence_requirements": ["scorable frozen V3 assessment", "configuration hash match",
                                          "source round is current PostgreSQL latest under advisory lock",
                                          "target round absent at commit", "source index + 1 = target index",
                                          "target index >= policy activation target"],
                "max_selections_per_experiment": 4,
                "target_threshold": TARGET_MULTIPLIER,
                "decision_states": ["NO_SIGNAL", "WATCH", "SELECTED"],
            }
        else:
            rules = None
        body = {
            "policy_id": uuid.uuid4().hex if selected else f"{POLICY_ID}_RESEARCH_ONLY",
            "policy_version": POLICY_VERSION,
            "status": status,
            "created_at": now,
            "development_cutoff_round": int(development_cutoff_round),
            "target": TARGET_MULTIPLIER,
            "model_version": model_version,
            "model_hash": model_hash,
            "development_sample_count": len(rows),
            "development_target_start": int(rows[0]["target_round_index"]) if rows else None,
            "development_target_end": int(rows[-1]["target_round_index"]) if rows else None,
            "development_baseline_ge_2x": float(np.mean([bool(row["is_true"]) for row in rows])) if rows else None,
            "candidate_frequencies": candidate_results,
            "selection_rules": rules,
            "expected_development_coverage": selected["effective_frequency"] if selected else None,
            "selected_candidate_frequency": selected["requested_frequency"] if selected else None,
            "selected_candidate_metrics": selected,
            "reliability_requirements": {
                "minimum_selected_development": MIN_SELECTED_DEVELOPMENT,
                "maximum_effective_frequency": MAX_FREEZE_FREQUENCY,
                "bootstrap_lift_lower_bound_above_zero": True,
                "minimum_positive_chronological_blocks": 4,
                "block_bootstrap_size": BOOTSTRAP_BLOCK_SIZE,
                "bootstrap_replicates": BOOTSTRAP_REPLICATES,
            },
            "active_experiment_rounds_used": False,
        }
        material = {key: value for key, value in body.items() if key not in {"created_at", "policy_id"}}
        body["policy_hash"] = hashlib.sha256(json.dumps(
            material, sort_keys=True, separators=(",", ":"), default=str).encode()).hexdigest()
        if not selected:
            body["reason"] = "No candidate satisfies rare-tail sample support, positive block-bootstrap lift, and chronological stability requirements."
        return body

    @staticmethod
    def decide(score: float, policy: dict[str, Any]) -> tuple[str, str]:
        if policy.get("status") != "FROZEN" or not policy.get("selection_rules"):
            return "NO_SIGNAL", "NO_RELIABLE_POLICY"
        rules = policy["selection_rules"]
        value = float(score)
        if value >= float(rules["selection_threshold"]):
            return "SELECTED", "FROZEN_DEVELOPMENT_RARE_TAIL_CUTOFF"
        if value >= float(rules["watch_threshold"]):
            return "WATCH", "FROZEN_DEVELOPMENT_WATCH_BAND"
        return "NO_SIGNAL", "BELOW_FROZEN_DEVELOPMENT_WATCH_BAND"
