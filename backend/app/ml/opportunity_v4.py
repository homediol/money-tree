"""V4 frozen-ranking prospective observer and historical stability gate.

The V3 ranker is frozen from its original development partition. V4 writes an
append-only assessment before its target arrives and stores outcomes separately.
It is research-only and has no Decision/Risk/Execution integration.
"""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from collections import Counter
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import spearmanr
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import brier_score_loss
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import StandardScaler

from app.ml.opportunity import TARGET, validated_rounds, wilson

MODEL_VERSION = "V3_FROZEN_2026-10-03"
CONFIG_KEY = "opportunity_v3_frozen_config"
STABILITY_KEY = "opportunity_v4_stability"
DIAGNOSTICS_KEY = "opportunity_v4_historical_diagnostics_v4"
OBSERVER_RUNTIME_KEY = "opportunity_v4_observer_runtime"
WARMUP_MONITOR_KEY = "opportunity_v3_frozen_warmup_monitor_v1"
OBSERVER_HEARTBEAT_STALE_SECONDS = 45.0
REAL_DATA_ALIGNMENT_AUDIT_VERSION = 1
REAL_DATA_ALIGNMENT_REQUIRED_ROUNDS = 10
PROSPECTIVE_RESULT_THRESHOLD = 2.00


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def real_data_alignment_snapshot(runtime: dict[str, Any] | None) -> dict[str, Any]:
    """Describe only the current persisted, consecutive live-chain audit run."""
    runtime = runtime or {}
    records = runtime.get("alignment_audit_rounds") or []
    if runtime.get("alignment_audit_version") != REAL_DATA_ALIGNMENT_AUDIT_VERSION:
        records = []
    verified = []
    for row in records:
        try:
            valid = (row.get("collector_round_id") == row.get("postgres_round_id")
                     == row.get("assessment_source_round_id")
                     and int(row.get("round_index")) == int(row.get("assessment_source_round_index"))
                     and row.get("pre_outcome_proof_verified") is True
                     and bool(row.get("configuration_hash")))
            if verified and int(row["round_index"]) != int(verified[-1]["round_index"]) + 1:
                verified = []
            if valid:
                verified.append(row)
            else:
                verified = []
        except (KeyError, TypeError, ValueError):
            verified = []
    verified = verified[-REAL_DATA_ALIGNMENT_REQUIRED_ROUNDS:]
    complete = len(verified) >= REAL_DATA_ALIGNMENT_REQUIRED_ROUNDS
    tracker_active = runtime.get("alignment_tracker_version") == 1
    batch_progress = runtime.get("alignment_batch_progress")
    if not isinstance(batch_progress, int):
        batch_progress = min(len(verified), REAL_DATA_ALIGNMENT_REQUIRED_ROUNDS)
    last_failure = runtime.get("alignment_last_failure")
    last_pass_at = runtime.get("alignment_last_pass_at")
    failure_at = (last_failure or {}).get("detected_at") if isinstance(last_failure, dict) else None
    recovered_failure = bool(last_pass_at and failure_at and last_pass_at >= failure_at)
    display_status = ("DEGRADED" if last_failure and not recovered_failure else
                      "VERIFIED" if (runtime.get("alignment_had_verified_batch") or complete
                                     or int(runtime.get("alignment_passed_batches") or 0) > 0) else
                      "VERIFYING")
    tracking_counts = tracker_active
    last_completed_batch = runtime.get("alignment_last_completed_batch")
    return {
        "status": "VERIFIED_10_ROUND" if complete else "UNVERIFIED",
        "display_status": display_status,
        "consecutive_rounds": len(verified),
        "current_batch_progress": batch_progress,
        "required_rounds": REAL_DATA_ALIGNMENT_REQUIRED_ROUNDS,
        "completed_batches": int(runtime.get("alignment_completed_batches") or 0) if tracking_counts else None,
        "passed_batches": int(runtime.get("alignment_passed_batches") or 0) if tracking_counts else None,
        "failed_batches": int(runtime.get("alignment_failed_batches") or 0) if tracking_counts else None,
        "batch_counters_tracking_since": runtime.get("alignment_tracking_started_at") if tracking_counts else None,
        "last_completed_batch": last_completed_batch,
        "total_rounds_verified": int(runtime.get("alignment_total_rounds_verified") or 0) if tracking_counts else None,
        "consecutive_aligned_rounds": int(runtime.get("alignment_consecutive_rounds") or len(verified)),
        "last_verified_target_round_index": (int(verified[-1]["round_index"]) + 1 if verified else None),
        "last_failure": last_failure if isinstance(last_failure, dict) else None,
        "reset": runtime.get("alignment_last_reset"),
        "latest_verified_round_index": int(verified[-1]["round_index"]) if verified else None,
        "latest_verified_round_id": verified[-1].get("postgres_round_id") if verified else None,
        "reason": (None if complete else runtime.get("alignment_audit_reason")
                   or f"Need {REAL_DATA_ALIGNMENT_REQUIRED_ROUNDS - len(verified)} more consecutive live collector-to-assessment matches."),
    }


def scoring_warmup_snapshot(assessments: list[dict[str, Any]] | None,
                            configuration_hash: str | None = None) -> dict[str, Any]:
    """Report the current verified-segment warm-up without weakening its gate."""
    rows = assessments or []
    latest = max(rows, key=lambda row: int(row.get("round_index", 0)), default=None)
    required = 102
    if latest is None:
        return {"status": "WAITING", "verified_rounds": 0, "required_rounds": required,
                "remaining_rounds": required, "source_round_index": None,
                "reason": "No frozen V3 assessment has been persisted for the current source yet."}
    snapshot = latest.get("feature_snapshot") or {}
    try:
        progress = max(0, min(required, int(snapshot.get("verified_segment_rounds", 0))))
    except (TypeError, ValueError):
        progress = 0
    proof = latest.get("round_order_proof") or {}
    try:
        source_index = int(latest["round_index"])
        target_index = int(latest.get("target_round_index", source_index + 1))
    except (KeyError, TypeError, ValueError):
        source_index, target_index = -1, -1
    proof_verified = bool(
        latest.get("assessment_immutable") is True
        and target_index == source_index + 1
        and proof.get("assessment_source_round_index") == source_index
        and proof.get("assessment_source_round_id") == str(latest.get("round_id"))
        and proof.get("latest_round_at_assessment_commit_index") == source_index
        and proof.get("latest_round_at_assessment_commit_id") == str(latest.get("round_id"))
        and proof.get("target_outcome_round_index") == target_index
        and proof.get("source_was_latest_at_assessment_commit") is True
        and proof.get("target_absent_at_assessment_commit") is True
        and proof.get("proof_method") == "postgres_advisory_transaction_lock"
        and (not configuration_hash or latest.get("model_configuration_hash") == configuration_hash)
    )
    if latest.get("scorable") is True and proof_verified:
        status = "READY"
        progress = required
        reason = None
    elif latest.get("failed_gate") == "CONTINUITY_WARMUP":
        status = "WARMING_UP"
        reason = f"{progress}/{required} verified contiguous rounds; {max(0, required - progress)} more are required before frozen scoring."
    else:
        status = "WAITING"
        reason = latest.get("failed_reason") or "The latest source assessment is not eligible for frozen scoring."
    return {"status": status, "verified_rounds": progress, "required_rounds": required,
            "remaining_rounds": max(0, required - progress),
            "source_round_index": latest.get("round_index"), "reason": reason}


def _sigmoid(value: float) -> float:
    return float(1.0 / (1.0 + math.exp(-max(-35.0, min(35.0, value)))))


class OpportunityStabilityV4:
    def __init__(self, repository):
        self.repository = repository

    def freeze_research_gate(self) -> dict[str, Any]:
        """Freeze a research-only cutoff from the already frozen development score distribution."""
        config = self.repository.load_application_state(CONFIG_KEY) or {}
        if config.get("status") != "FROZEN" or not config.get("configuration_hash"):
            return {"status": "SELECTIVE_GATE_NOT_DEFINED", "reason": "frozen V3 configuration is unavailable"}
        existing = self.repository.load_opportunity_research_gate(config["version"], config["configuration_hash"])
        if existing:
            return existing
        cutoffs = (config.get("coverage_reference") or {}).get("cutoffs") or {}
        threshold = cutoffs.get("top_5%")
        if threshold is None:
            return {"status": "SELECTIVE_GATE_NOT_DEFINED", "reason": "frozen development score cutoff is unavailable"}
        body = {
            "model_version": config["version"], "model_hash": config["configuration_hash"],
            "status": "FROZEN_RESEARCH_ONLY", "policy_version": "v3-development-top5-cutoff-max4-per-100-v1",
            "created_at": _utcnow(),
            "source": "V3 frozen score distribution from pre-test development feature rows; outcomes unused",
            "policy": {"score_threshold": float(threshold), "threshold_source": "coverage_reference.cutoffs.top_5%",
                       "max_selections_per_100": 4, "window_rounds": 100,
                       "selection_rule": "select in arrival order when frozen score meets development cutoff and a slot remains",
                       "outcomes_used": False, "prospective_outcomes_used": False,
                       "validated_live_gate": False, "research_only": True},
        }
        hash_material = {key: value for key, value in body.items() if key != "created_at"}
        body["gate_hash"] = hashlib.sha256(json.dumps(
            hash_material, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")).hexdigest()
        return self.repository.save_opportunity_research_gate_once(body)

    def ensure_rare_opportunity_selection_policy(self) -> dict[str, Any]:
        """Research only against strictly pre-experiment resolved V3 rows.

        A passing policy is persisted once and activated at a locked future
        target boundary. A weak/undersupported research result remains an
        immutable NO_RELIABLE_POLICY report and never selects live rounds.
        """
        from app.ml.rare_opportunity_policy import (
            DEVELOPMENT_CUTOFF_ROUND, RareOpportunitySelectionPolicyV1,
        )

        config = self.repository.load_application_state(CONFIG_KEY) or {}
        if (config.get("status") != "FROZEN" or config.get("version") != MODEL_VERSION
                or not config.get("configuration_hash")):
            return {"status": "NO_RELIABLE_POLICY", "reason": "Frozen V3 configuration is unavailable."}
        existing = self.repository.load_rare_opportunity_selection_policy(
            MODEL_VERSION, config["configuration_hash"])
        if existing:
            if existing.get("status") == "FROZEN":
                experiment = self.repository.active_opportunity_experiment()
                if experiment and not experiment.get("selection_policy_hash"):
                    self.repository.activate_rare_opportunity_selection_policy(
                        experiment["experiment_id"], existing)
            return existing
        development_rows = self.repository.load_rare_opportunity_policy_development_rows(
            model_version=MODEL_VERSION, model_hash=config["configuration_hash"],
            target_cutoff_round=DEVELOPMENT_CUTOFF_ROUND)
        for row in development_rows:
            row["pre_outcome_verified"] = True
        report = RareOpportunitySelectionPolicyV1.develop(
            development_rows, model_version=MODEL_VERSION,
            model_hash=config["configuration_hash"],
            development_cutoff_round=DEVELOPMENT_CUTOFF_ROUND)
        persisted = self.repository.save_rare_opportunity_selection_policy_once(report)
        if persisted.get("status") == "FROZEN":
            experiment = self.repository.active_opportunity_experiment()
            if experiment:
                activated = self.repository.activate_rare_opportunity_selection_policy(
                    experiment["experiment_id"], persisted)
                if activated:
                    persisted["activation_target_index"] = activated.get("policy_activation_target_index")
                    persisted["activation_source_round_index"] = activated.get("policy_activation_source_round_index")
        return persisted

    def start_prospective_experiment(self, *, explicit: bool = False) -> dict[str, Any]:
        """Create one durable timeline at the current PostgreSQL tail; never backfill old outcomes."""
        if not getattr(self.repository, "database_url", None):
            return {"status": "PAUSED", "reason": "authoritative PostgreSQL is unavailable"}
        active = self.repository.active_opportunity_experiment()
        if active:
            return active
        latest = self.repository.latest_opportunity_experiment()
        if not explicit:
            return latest or {"status": "PAUSED", "reason": "new experiment requires explicit operator action"}
        config = self.repository.load_application_state(CONFIG_KEY) or {}
        rare_policy = self.ensure_rare_opportunity_selection_policy()
        policy_frozen = rare_policy.get("status") == "FROZEN"
        now = _utcnow()
        payload = {
            "experiment_id": uuid.uuid4().hex, "model_version": config["version"],
            "model_hash": config["configuration_hash"],
            "gate_hash": "NOT_DEFINED",
            "status": "ACTIVE", "started_at": now,
            "start_from_next_round": True,
            "target_threshold": PROSPECTIVE_RESULT_THRESHOLD,
            "max_selections": 4 if policy_frozen else None,
            "four_selection_policy": "DEFINED" if policy_frozen else "NOT_DEFINED",
            "selection_policy_status": "NOT_ACTIVE" if policy_frozen else rare_policy.get("status", "NO_RELIABLE_POLICY"),
            "selection_policy_reason": rare_policy.get("reason"),
            "window_rounds": 100, "evaluation_rounds": 500,
            "rounds_observed": 0, "rounds_scored": 0, "rounds_missed": 0,
            "rounds_invalid": 0, "rounds_gaps": 0, "rounds_unverified": 0,
            "predictions_selected": 0, "predictions_resolved": 0, "true_predictions": 0,
            "false_predictions": 0, "pending_predictions": 0,
            "start_reason": "INITIAL_FROZEN_PROSPECTIVE_BOUNDARY" if not explicit else "OPERATOR_STARTED_NEW_EXPERIMENT",
            "updated_at": now,
        }
        created = self.repository.create_opportunity_experiment(payload)
        if created.get("experiment_id") == payload["experiment_id"] and policy_frozen:
            created = self.repository.activate_rare_opportunity_selection_policy(created["experiment_id"], rare_policy) or created
        return created

    def sync_prospective_experiment(self) -> dict[str, Any] | None:
        experiment = self.repository.sync_active_opportunity_experiment()
        if not experiment:
            experiment = self.repository.latest_opportunity_experiment()
        if experiment:
            self._persist_prospective_checkpoints(experiment)
        return experiment

    def _persist_prospective_checkpoints(self, experiment: dict[str, Any]) -> None:
        targets = self.repository.opportunity_experiment_targets(experiment["experiment_id"])
        predictions = self.repository.opportunity_experiment_predictions(experiment["experiment_id"])
        existing = {int(row["checkpoint_rounds"]) for row in self.repository.opportunity_experiment_checkpoints(
            experiment["experiment_id"])}
        for size in (100, 200, 300, 400, 500):
            if size in existing or int(experiment.get("rounds_observed", 0)) < size:
                continue
            through = [row for row in targets if row.get("observed_ordinal") is not None
                       and int(row["observed_ordinal"]) <= size]
            selected = [row for row in predictions if int(row.get("observed_ordinal", size + 1)) <= size]
            if any(row.get("status") == "PENDING" for row in selected):
                continue
            resolved = [row for row in selected if row.get("status") in {"TRUE", "FALSE"}]
            true_count = sum(row.get("status") == "TRUE" for row in resolved)
            valid_baseline = [row for row in through if row.get("classification") not in {"GAP", "UNVERIFIED"}
                              and row.get("actual_multiplier") is not None]
            result_threshold = float(experiment.get("target_threshold", TARGET))
            baseline_hits = sum(float(row["actual_multiplier"]) >= result_threshold for row in valid_baseline)
            precision = true_count / len(resolved) if resolved else None
            baseline = baseline_hits / len(valid_baseline) if valid_baseline else None
            ordered = sorted(resolved, key=lambda row: int(row.get("observed_ordinal", 0)))
            gaps = [int(right["observed_ordinal"]) - int(left["observed_ordinal"])
                    for left, right in zip(ordered, ordered[1:])]
            scores = [float(row["opportunity_score"]) for row in resolved if row.get("opportunity_score") is not None]
            true_scores = [float(row["opportunity_score"]) for row in resolved
                           if row.get("status") == "TRUE" and row.get("opportunity_score") is not None]
            false_scores = [float(row["opportunity_score"]) for row in resolved
                            if row.get("status") == "FALSE" and row.get("opportunity_score") is not None]

            def distribution(values):
                if not values:
                    return {"count": 0, "mean": None, "median": None, "minimum": None, "maximum": None}
                return {"count": len(values), "mean": float(np.mean(values)), "median": float(np.median(values)),
                        "minimum": float(np.min(values)), "maximum": float(np.max(values))}

            report = {
                "experiment_id": experiment["experiment_id"], "checkpoint_rounds": size,
                "completed_at": _utcnow(), "rounds_observed": min(size, int(experiment["rounds_observed"])),
                "rounds_scored": sum(row.get("classification") == "ELIGIBLE_SCORED" for row in through),
                "rounds_missed": sum(row.get("classification") == "MISSED" for row in through),
                "rounds_invalid": sum(row.get("classification") == "INVALID" for row in through),
                "rounds_gaps": sum(row.get("classification") == "GAP" for row in targets
                                    if row.get("observed_ordinal") is None or int(row.get("observed_ordinal") or 0) <= size),
                "selected": len(selected), "true": true_count,
                "false": sum(row.get("status") == "FALSE" for row in resolved),
                "invalid_predictions": sum(row.get("status") == "INVALID" for row in selected),
                "pending": sum(row.get("status") == "PENDING" for row in selected),
                "precision": precision, "precision_ci95": wilson(true_count, len(resolved)),
                "baseline_true": baseline_hits, "baseline_rounds": len(valid_baseline),
                "baseline_rate": baseline,
                "absolute_lift": precision - baseline if precision is not None and baseline is not None else None,
                "relative_lift": ((precision / baseline) - 1) if precision is not None and baseline else None,
                "coverage": len(resolved) / size, "selection_frequency": len(selected) / size,
                "average_gap_between_selections": float(np.mean(gaps)) if gaps else None,
                "score_distribution": distribution(scores), "true_score_distribution": distribution(true_scores),
                "false_score_distribution": distribution(false_scores),
                "selected_predictions": selected,
                "method": "pre-outcome selected predictions only; same experiment actual-round baseline",
            }
            self.repository.persist_opportunity_experiment_checkpoint(experiment["experiment_id"], report)

    def prospective_experiment_snapshot(self) -> dict[str, Any]:
        if not getattr(self.repository, "database_url", None):
            return {"status": "PAUSED", "state_available": False,
                    "reason": "authoritative PostgreSQL is unavailable", "experiment_id": None,
                    "research_gate": None, "selected_predictions": [],
                    "internal_score_monitor": [], "checkpoints": []}
        experiment = self.repository.active_opportunity_experiment() or self.repository.latest_opportunity_experiment()
        if not experiment:
            gate = self.repository.load_opportunity_research_gate(MODEL_VERSION,
                    (self.repository.load_application_state(CONFIG_KEY) or {}).get("configuration_hash", ""))
            policy = self.repository.load_rare_opportunity_selection_policy(MODEL_VERSION,
                    (self.repository.load_application_state(CONFIG_KEY) or {}).get("configuration_hash", ""))
            return {"status": "NOT_STARTED", "state_available": True,
                    "reason": "prospective experiment has not been initialized",
                    "experiment_id": None, "research_gate": gate, "selected_predictions": [],
                    "rare_selection_policy": policy,
                    "selection_policy_status": (policy or {}).get("status", "NOT_ACTIVE"),
                    "four_selection_policy": ("DEFINED" if int(((gate or {}).get("policy") or {}).get("max_selections_per_500") or 0) == 4 else "NOT_DEFINED"),
                    "internal_score_monitor": [], "checkpoints": []}
        targets = self.repository.opportunity_experiment_targets(experiment["experiment_id"])
        predictions = self.repository.opportunity_experiment_predictions(experiment["experiment_id"])
        checkpoints = self.repository.opportunity_experiment_checkpoints(experiment["experiment_id"])
        observed = int(experiment.get("rounds_observed", 0))
        latest_window = max(0, min(4, (observed - 1) // 100)) if observed else 0
        # The primary 100-round checkpoint is cumulative and never rolls back
        # into a fresh 1/100 counter; full progress carries the same timeline
        # forward through 500 rounds.
        primary_progress = min(100, observed)
        current_window_predictions = [row for row in predictions if int(row.get("window_number", 0)) == latest_window]
        current_window_predictions.sort(key=lambda row: (int(row.get("rank_at_selection", 999)),
                                                          int(row.get("target_round_index", 0))))
        all_selected_predictions = sorted(predictions, key=lambda row: (
            int(row.get("target_round_index", 0)), str(row.get("prediction_id", ""))))
        completed = {int(row["checkpoint_rounds"]): row for row in checkpoints}
        gate = self.repository.load_opportunity_research_gate(
            experiment.get("model_version", MODEL_VERSION), experiment.get("model_hash", ""))
        rare_policy = self.repository.load_rare_opportunity_selection_policy(
            experiment.get("model_version", MODEL_VERSION), experiment.get("model_hash", ""))
        queue_snapshot = self.repository.frozen_observer_queue_snapshot()
        true_count = sum(row.get("status") == "TRUE" for row in predictions)
        false_count = sum(row.get("status") == "FALSE" for row in predictions)
        pending_count = sum(row.get("status") == "PENDING" for row in predictions)
        invalid_prediction_count = sum(row.get("status") == "INVALID" for row in predictions)
        return {
            "state_available": True, "experiment_id": experiment["experiment_id"], "model_version": experiment.get("model_version"),
            "model_hash": experiment.get("model_hash"), "gate_hash": experiment.get("gate_hash"),
            "target_threshold": experiment.get("target_threshold", TARGET), "status": experiment.get("status"),
            "reason": experiment.get("reason"), "started_at": experiment.get("started_at"),
            "start_round_index": experiment.get("start_round_index"),
            "start_source_round_index": experiment.get("start_source_round_index"),
            "first_target_round_index": experiment.get("first_target_round_index", experiment.get("start_round_index")),
            "selection_policy_status": ("FROZEN" if experiment.get("selection_policy_hash") else
                                        (rare_policy or {}).get("status", experiment.get("selection_policy_status", "NOT_ACTIVE"))),
            "selection_policy_reason": ((rare_policy or {}).get("reason")
                                        or experiment.get("selection_policy_reason")),
            "four_selection_policy": ("DEFINED" if experiment.get("max_selections") == 4 else "NOT_DEFINED"),
            "rare_selection_policy": rare_policy,
            "policy_activation_target_index": experiment.get("policy_activation_target_index"),
            "policy_activation_source_round_index": experiment.get("policy_activation_source_round_index"),
            "last_round_index": experiment.get("last_round_index"),
            "rounds_observed": observed, "primary_progress": primary_progress,
            "full_progress": min(500, observed), "evaluation_rounds": 500,
            "rounds_scored": sum(row.get("classification") == "ELIGIBLE_SCORED" for row in targets),
            "rounds_missed": sum(row.get("classification") == "MISSED" for row in targets),
            "rounds_invalid": sum(row.get("classification") == "INVALID" for row in targets),
            "rounds_gaps": sum(row.get("classification") == "GAP" for row in targets),
            "rounds_unverified": sum(row.get("classification") == "UNVERIFIED" for row in targets),
            "assessment_coverage": {
                "scored": sum(row.get("classification") == "ELIGIBLE_SCORED" for row in targets),
                "missed": sum(row.get("classification") == "MISSED" for row in targets),
                "coverage": (sum(row.get("classification") == "ELIGIBLE_SCORED" for row in targets) /
                    max(1, sum(row.get("classification") in {"ELIGIBLE_SCORED", "MISSED"} for row in targets))),
                "post_fix_scored": queue_snapshot.get("post_fix_scored", 0),
                "post_fix_missed": queue_snapshot.get("post_fix_missed", 0),
                "post_fix_invalid": queue_snapshot.get("post_fix_invalid", 0),
                "post_fix_coverage": queue_snapshot.get("post_fix_coverage", 0.0),
            },
            "observer_queue": queue_snapshot,
            "predictions_selected": len(predictions),
            "predictions_resolved": true_count + false_count,
            "predictions_invalid": invalid_prediction_count,
            "true_predictions": true_count,
            "false_predictions": false_count,
            "pending_predictions": pending_count,
            "no_signal_count": sum(row.get("selection_state") == "NO_SIGNAL" for row in targets),
            "watch_count": sum(row.get("selection_state") == "WATCH" for row in targets),
            "current_window_number": latest_window + 1,
            "selection_slots_used": len([row for row in current_window_predictions if row.get("selected")]),
            "selection_slots_available": max(0, 4 - len([row for row in current_window_predictions if row.get("selected")])),
            "research_gate": gate, "selected_predictions": all_selected_predictions,
            "current_window_selected_predictions": current_window_predictions,
            "internal_score_monitor": targets[-100:], "checkpoints": checkpoints,
            "checkpoint_reports": {str(size): completed.get(size) for size in (100, 200, 300, 400, 500)},
            "precision": (true_count / (true_count + false_count) if true_count + false_count else None),
            "automatic_real_money_execution": "OFF",
        }

    @staticmethod
    def _verified_round_segments(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
        """Split persisted rounds using the same identity/order proof as scoring."""
        segments: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for row in rows:
            if not bool(row.get("continuity_verified")):
                if current:
                    segments.append(current)
                    current = []
                continue
            boundary = bool(row.get("gap_before"))
            if current:
                previous = current[-1]
                boundary = (boundary
                            or int(row["round_index"]) != int(previous["round_index"]) + 1
                            or str(row["round_id"]) == str(previous["round_id"]))
            if boundary and current:
                segments.append(current)
                current = []
            current.append(row)
        if current:
            segments.append(current)
        return segments

    def _live_warmup_snapshot(self, latest_round: dict[str, Any] | None,
                              assessments: list[dict[str, Any]] | None = None,
                              configuration_hash: str | None = None,
                              round_context: dict[str, Any] | None = None) -> dict[str, Any]:
        """Derive warm-up only from the current verified PostgreSQL round suffix."""
        required = 102
        if not hasattr(self.repository, "live_round_continuity_snapshot"):
            # Narrow compatibility for repository test doubles. The deployed
            # Repository always has the PostgreSQL method above; no production
            # dashboard value uses this assessment-derived branch.
            legacy = scoring_warmup_snapshot(assessments, configuration_hash)
            progress = int(legacy.get("verified_rounds") or 0)
            return {**legacy,
                    "status": "COMPLETE" if progress >= required else "WARMING_UP",
                    "current_contiguous_verified_rounds": progress,
                    "estimated_rounds_remaining": max(0, required - progress),
                    "progress_percent": round(min(required, progress) / required * 100, 1),
                    "continuity_break_reason": None,
                    "continuity_break_at_round_index": None,
                    "reset": None}
        rounds_available = bool(getattr(self.repository, "database_url", None))
        round_context = (round_context or self.repository.live_round_continuity_snapshot(required * 2 + 1)
                         if rounds_available else {"latest_round": None, "recent_rounds": [], "verified_suffix": []})
        latest_round = round_context.get("latest_round")
        recent = round_context.get("recent_rounds") or []
        suffix = round_context.get("verified_suffix") or []
        recent_segments = self._verified_round_segments(recent)

        latest_matches = bool(latest_round and suffix
                              and int(suffix[-1]["round_index"]) == int(latest_round["round_index"])
                              and str(suffix[-1]["round_id"]) == str(latest_round["round_id"])
                              and bool(latest_round.get("continuity_verified")))
        suffix_segments = self._verified_round_segments(suffix) if latest_matches else []
        current_segment = suffix_segments[-1] if suffix_segments else []
        current_count = len(current_segment)
        progress = min(required, current_count)
        latest_index = int(latest_round["round_index"]) if latest_round else None
        current_start_index = (int(current_segment[0]["round_index"]) if current_segment else None)

        if not rounds_available:
            break_reason = "DATABASE_GAP"
        elif not latest_round:
            break_reason = "IDENTITY_UNVERIFIED"
        elif not bool(latest_round.get("continuity_verified")):
            break_reason = "IDENTITY_UNVERIFIED"
        elif current_segment and current_start_index is not None:
            recent_by_index = {int(row["round_index"]): row for row in recent}
            start_row = recent_by_index.get(current_start_index, current_segment[0])
            previous_row = next((row for row in reversed(recent)
                                 if int(row["round_index"]) < current_start_index), None)
            if bool(start_row.get("gap_before")):
                break_reason = "COLLECTOR_GAP"
            elif previous_row and int(previous_row["round_index"]) + 1 != current_start_index:
                break_reason = "MISSING_ROUND"
            elif previous_row and not bool(previous_row.get("continuity_verified")):
                break_reason = "IDENTITY_UNVERIFIED"
            elif (not previous_row and recent and current_start_index > int(recent[0]["round_index"])):
                break_reason = "OTHER"
            else:
                break_reason = None
        elif recent and len(recent) > 1 and int(recent[-1]["round_index"]) - int(recent[-2]["round_index"]) > 1:
            break_reason = "MISSING_ROUND"
        else:
            break_reason = "IDENTITY_UNVERIFIED"

        # On first dashboard load, recover the immediately preceding verified
        # run from durable round rows so a reset that happened while the UI was
        # closed is still visible.
        if latest_matches and current_segment:
            matching_recent_segment = next((i for i, segment in enumerate(recent_segments)
                                            if int(segment[-1]["round_index"]) == latest_index), None)
            previous_segment = (recent_segments[matching_recent_segment - 1]
                               if matching_recent_segment is not None and matching_recent_segment > 0 else [])
        else:
            previous_segment = recent_segments[-1] if recent_segments else []
        previous_segment_progress = min(required, len(previous_segment))

        monitor = self.repository.load_application_state(WARMUP_MONITOR_KEY) or {}
        previous_progress = monitor.get("current_progress")
        reset = monitor.get("last_reset")
        if previous_progress is None:
            prior_progress = previous_segment_progress
        else:
            try:
                prior_progress = max(0, min(required, int(previous_progress)))
            except (TypeError, ValueError):
                prior_progress = previous_segment_progress

        if prior_progress > progress:
            reset = {
                "active": True,
                "previous_progress": prior_progress,
                "current_progress": progress,
                "break_at_round_index": current_start_index or latest_index,
                "reason": break_reason or "OTHER",
                "detected_at": _utcnow(),
            }
        elif reset:
            reset = {**reset, "active": progress < int(reset.get("previous_progress", required))}

        now = _utcnow()
        if hasattr(self.repository, "save_application_state"):
            self.repository.save_application_state(WARMUP_MONITOR_KEY, {
                "current_progress": progress,
                "current_contiguous_verified_rounds": current_count,
                "last_round_index": latest_index,
                "last_reset": reset,
                "updated_at": now,
            }, now)

        return {
            "status": "COMPLETE" if progress >= required else "WARMING_UP",
            "verified_rounds": progress,
            "current_contiguous_verified_rounds": current_count,
            "required_rounds": required,
            "remaining_rounds": max(0, required - progress),
            "estimated_rounds_remaining": max(0, required - progress),
            "progress_percent": round((progress / required) * 100, 1),
            "source_round_index": latest_index,
            "reason": None if progress >= required else "INSUFFICIENT_CONTIGUOUS_HISTORY",
            "continuity_break_reason": break_reason,
            "continuity_break_at_round_index": ((current_start_index or latest_index)
                                                if break_reason else None),
            "reset": reset,
        }

    def freeze_v3(self, rounds: pd.DataFrame) -> dict[str, Any]:
        """Persist the V3 logistic ranker once, trained only before its old test."""
        existing = self.repository.load_application_state(CONFIG_KEY)
        if existing:
            return existing
        from app.ml.opportunity_v2 import V1_DIAGNOSTIC_START_INDEX, V1_SOURCE_END_INDEX
        from app.ml.opportunity_v3 import OpportunityDiscoveryV3

        clean, _ = validated_rounds(rounds.copy())
        dev = clean.loc[clean.round_index < V1_DIAGNOSTIC_START_INDEX].reset_index(drop=True)
        helper = OpportunityDiscoveryV3(self.repository)
        rows, labels, *_ = helper._build_rows(dev)
        if len(labels) < 500:
            return {"version": MODEL_VERSION, "status": "FREEZE_BLOCKED", "reason": "fewer_than_500_development_targets"}
        feature_names = ["pattern_sequence_5_successes", "pattern_sequence_5_rate"]
        x = np.asarray([[row[name] for name in feature_names] for row in rows], dtype=float)
        train_end = int(len(labels) * .65)
        scaler = StandardScaler().fit(x[:train_end])
        estimator = LogisticRegression(C=.1, max_iter=800, random_state=42)
        estimator.fit(scaler.transform(x[:train_end]), labels[:train_end])
        # Coverage ranks are relative to a fixed, outcome-blind reference
        # distribution. Labels are not used to calculate these cutoffs.
        reference_scores = estimator.predict_proba(scaler.transform(x))[:, 1]
        coverage_cutoffs = {
            f"top_{fraction:g}%": float(np.quantile(reference_scores, 1 - fraction / 100, method="higher"))
            for fraction in (.5, 1, 2, 5, 10, 20)
        }
        core = {
            "version": MODEL_VERSION, "status": "FROZEN",
            "source": "V3 original logistic ranking policy; pre-V1-diagnostic development partition only",
            "target": ">=2.10x", "feature_names": feature_names,
            "feature_version": "v3-sequence5-successes-rate-v1",
            "score_version": "frozen-logistic-probability-v1",
            "feature_mean": scaler.mean_.astype(float).tolist(),
            "feature_scale": scaler.scale_.astype(float).tolist(),
            "coefficients": estimator.coef_[0].astype(float).tolist(),
            "intercept": float(estimator.intercept_[0]),
            "coverage_reference": {
                "source": "all pre-V1-test development feature rows scored by frozen estimator; outcomes unused",
                "scores": int(len(reference_scores)), "cutoffs": coverage_cutoffs,
            },
            "training_targets": int(train_end), "development_targets": int(len(labels)),
            "development_last_round_index": int(dev.round_index.max()),
            "v1_diagnostic_start_round_index": V1_DIAGNOSTIC_START_INDEX,
            "v1_source_end_round_index": V1_SOURCE_END_INDEX,
            "threshold_or_gates_changed": False,
            "frozen_at": _utcnow(),
        }
        core["configuration_hash"] = hashlib.sha256(
            repr(sorted(core.items())).encode("utf-8")).hexdigest()
        # Read-before-write makes the artifact immutable under repeated startup.
        return self.repository.save_application_state_once(CONFIG_KEY, core, core["frozen_at"])

    def historical_diagnostics(self, rounds: pd.DataFrame) -> dict[str, Any]:
        """Recreate V3's frozen pre-test split for diagnosis only; never selects a policy."""
        prior = self.repository.load_application_state(DIAGNOSTICS_KEY)
        if prior:
            return prior
        from app.ml.opportunity_v2 import V1_DIAGNOSTIC_START_INDEX
        from app.ml.opportunity_v3 import OpportunityDiscoveryV3
        from app.ml.opportunity import _factories

        clean, _ = validated_rounds(rounds.copy())
        dev = clean.loc[clean.round_index < V1_DIAGNOSTIC_START_INDEX].reset_index(drop=True)
        helper = OpportunityDiscoveryV3(self.repository)
        rows, y, sources, seqs, _ = helper._build_rows(dev)
        if len(y) < 500:
            return {"status": "INSUFFICIENT_DATA", "targets": len(y), "models": {}}
        names = ["pattern_sequence_5_successes", "pattern_sequence_5_rate"]
        x = np.asarray([[row[name] for name in names] for row in rows], dtype=float)
        train_end = int(len(y) * .65)
        eval_y = y[train_end:]
        models: dict[str, np.ndarray] = {}
        errors: dict[str, str] = {}
        for name in ("logistic_regression", "random_forest", "extra_trees", "gradient_boosting", "hist_gradient_boosting"):
            factory = _factories().get(name)
            if factory is None:
                errors[name] = "unavailable"; continue
            try:
                estimator = factory()
                estimator.fit(x[:train_end], y[:train_end])
                models[name] = estimator.predict_proba(x[train_end:])[:, 1]
            except Exception as exc:
                errors[name] = f"{type(exc).__name__}: {exc}"

        def metrics(labels, score, fraction):
            n = min(len(labels), max(1, int(math.ceil(len(labels) * fraction))))
            chosen = np.argsort(score)[-n:]
            hits = int(labels[chosen].sum()); ci = wilson(hits, n)
            base = float(np.mean(labels)) if len(labels) else None
            return {"support": n, "successes": hits, "precision": hits / n,
                    "ci95": ci, "baseline": base,
                    "lift": hits / n - base if base is not None else None}

        block_metrics = []
        all_values = dev.multiplier.to_numpy(float)
        analog_scaler = StandardScaler().fit(x[:train_end])
        analog_model = NearestNeighbors(n_neighbors=min(100, train_end), algorithm="auto").fit(
            analog_scaler.transform(x[:train_end]))
        analog_distances, analog_ids = analog_model.kneighbors(analog_scaler.transform(x[train_end:]))
        analog_rates = np.asarray([float(np.mean(y[ids])) for ids in analog_ids], dtype=float)
        state_values = np.select([all_values < 1.2, all_values < 1.5, all_values < 2.0,
                                  all_values < 3.0, all_values < 5.0], [0, 1, 2, 3, 4], default=5)

        def distribution(values):
            values = np.asarray(values, dtype=float)
            return {"mean": float(np.mean(values)), "median": float(np.median(values)),
                    "q10": float(np.quantile(values, .10)), "q25": float(np.quantile(values, .25)),
                    "q75": float(np.quantile(values, .75)), "q90": float(np.quantile(values, .90)),
                    "std": float(np.std(values))}

        def curve_metrics(labels, scores, fraction):
            n = max(1, int(math.ceil(len(labels) * fraction)))
            chosen = np.argsort(scores)[-n:]
            hits = int(labels[chosen].sum())
            ordered_scores = np.sort(scores)
            threshold = float(ordered_scores[-n])
            strict = scores > threshold
            boundary = scores == threshold
            strict_hits = int(labels[strict].sum())
            slots = max(0, n - int(strict.sum()))
            tie_expected_hits = strict_hits + slots * float(np.mean(labels[boundary])) if np.any(boundary) else strict_hits
            return {"support": n, "successes": hits, "precision": hits / n,
                    "ci95": wilson(hits, n), "lift": hits / n - float(np.mean(labels)),
                    "score_cutoff": threshold, "boundary_tie_count": int(boundary.sum()),
                    "slots_sampled_from_boundary_tie": slots,
                    "boundary_tie_fraction": slots / int(boundary.sum()) if np.any(boundary) else 0.0,
                    "tie_adjusted_precision_expectation": tie_expected_hits / n,
                    "tie_sensitive": bool(int(boundary.sum()) > slots)}

        for bno, local in enumerate(np.array_split(np.arange(len(eval_y)), 4), 1):
            if not len(local): continue
            yy = eval_y[local]
            block_start = train_end + int(local[0]); block_end = train_end + int(local[-1]) + 1
            source_positions = np.asarray(sources[block_start:block_end], dtype=int)
            outcomes = all_values[source_positions + 1]
            source_values = all_values[source_positions]
            recent_windows = [all_values[max(0, pos - 99):pos + 1] for pos in source_positions]
            recent_rates = np.asarray([np.mean(window >= TARGET) for window in recent_windows])
            recent_volatility = np.asarray([np.std(window) for window in recent_windows])
            recent_entropy = np.asarray([
                -sum(p * math.log2(p) for p in np.unique(state_values[max(0, pos - 99):pos + 1], return_counts=True)[1] / len(state_values[max(0, pos - 99):pos + 1])
                     if p > 0) for pos in source_positions
            ])
            sequence_counts = Counter(seqs[i][-5:] for i in range(block_start, block_end))
            block = {"block": bno, "targets": int(len(local)), "baseline": float(yy.mean()),
                     "outcome_quantiles": distribution(outcomes),
                     "source_multiplier_quantiles": distribution(source_values),
                     "analog_distance_distribution": distribution(analog_distances[local, 0]),
                     "mean_100_analog_distance_distribution": distribution(np.mean(analog_distances[local], axis=1)),
                     "past_analog_neighbor_rate_distribution": distribution(analog_rates[local]),
                     "features": {"pattern_sequence_5_successes": distribution(x[block_start:block_end, 0]),
                         "pattern_sequence_5_rate": distribution(x[block_start:block_end, 1]),
                         "seq5_states": dict(sequence_counts),
                         "recent_100_target_rate": distribution(recent_rates),
                         "recent_100_volatility": distribution(recent_volatility),
                         "recent_100_entropy": distribution(recent_entropy),
                         "threshold_frequencies": {f">={threshold:g}x": float(np.mean(outcomes >= threshold))
                                                   for threshold in (1.2, 1.5, 2.0, 2.1, 3.0, 5.0, 10.0)}},
                     "models": {}, "feature_distributions": {}}
            for name, pred in models.items():
                p = pred[local]
                block["models"][name] = {"brier": float(brier_score_loss(yy, p)),
                    "calibration_gap_mean_probability_minus_rate": float(np.mean(p) - np.mean(yy)),
                    "score_distribution": distribution(p),
                    **{f"top_{fraction:g}%": curve_metrics(yy, p, fraction / 100)
                       for fraction in (.5, 1, 2, 5, 10, 20)}}
            source_slice = slice(train_end + int(local[0]), train_end + int(local[-1]) + 1)
            for feature in names:
                values = x[source_slice, names.index(feature)]
                block["feature_distributions"][feature] = {"mean": float(np.mean(values)),
                    "median": float(np.median(values)), "q25": float(np.quantile(values, .25)),
                    "q75": float(np.quantile(values, .75))}
            block_metrics.append(block)

        curves = {}
        calibration = {}
        block_stability = {}
        score_ties = {}
        for name, pred in models.items():
            curves[name] = {f"top_{pct:g}%": curve_metrics(eval_y, pred, pct / 100)
                            for pct in (.5, 1, 2, 5, 10, 20)}
            order = np.argsort(pred)
            calibration[name] = []
            _, score_counts = np.unique(pred, return_counts=True)
            top1_threshold = np.sort(pred)[-max(1, int(math.ceil(len(pred) * .01)))]
            score_ties[name] = {"unique_score_count": int(len(score_counts)),
                "largest_tie_count": int(score_counts.max()) if len(score_counts) else 0,
                "fraction_in_tied_score_values": float(np.sum(score_counts[score_counts > 1]) / len(pred)) if len(pred) else 0.0,
                "top_1_boundary_tie_count": int(np.sum(pred == top1_threshold))}
            for bucket_no, ids in enumerate(np.array_split(order, 10), 1):
                if len(ids):
                    calibration[name].append({"score_decile": bucket_no, "support": int(len(ids)),
                        "mean_probability": float(np.mean(pred[ids])), "observed_rate": float(np.mean(eval_y[ids]))})
            top5_lifts = [block["models"].get(name, {}).get("top_5%", {}).get("lift") for block in block_metrics]
            top5_lifts = [float(value) for value in top5_lifts if value is not None]
            block_stability[name] = {"top5_lift_by_block": top5_lifts,
                "positive_lift_blocks": sum(value > 0 for value in top5_lifts),
                "blocks": len(top5_lifts), "mean_lift": float(np.mean(top5_lifts)) if top5_lifts else None,
                "lift_stddev": float(np.std(top5_lifts)) if top5_lifts else None}
        seq5_associations = {
            "pattern_sequence_5_successes": self._feature_target_association(x[train_end:, 0], eval_y),
            "pattern_sequence_5_rate": self._feature_target_association(x[train_end:, 1], eval_y),
            "correlation_between_features": (float(np.corrcoef(x[train_end:, 0], x[train_end:, 1])[0, 1])
                if np.std(x[train_end:, 0]) and np.std(x[train_end:, 1]) else None),
            "independent_votes": False,
        }
        pair = None
        if "logistic_regression" in models and "gradient_boosting" in models:
            lr, gb = models["logistic_regression"], models["gradient_boosting"]
            k = max(1, int(math.ceil(len(eval_y) * .05)))
            lr_ids, gb_ids = set(np.argsort(lr)[-k:]), set(np.argsort(gb)[-k:])
            both = lr_ids & gb_ids
            lr_hits = {int(i) for i in lr_ids if eval_y[i]}
            gb_hits = {int(i) for i in gb_ids if eval_y[i]}
            pair = {"top_5_support_each": k, "top_5_overlap": len(both),
                    "overlap_fraction_of_lr": len(both) / k,
                    "overlap_successes": int(eval_y[list(both)].sum()) if both else 0,
                    "logistic_successes": len(lr_hits), "gradient_boosting_successes": len(gb_hits),
                    "successful_overlap": len(lr_hits & gb_hits),
                    "logistic_unique_candidates": len(lr_ids - gb_ids),
                    "gradient_boosting_unique_candidates": len(gb_ids - lr_ids),
                    "spearman_rank_correlation": float(spearmanr(lr, gb).statistic),
                    "block_overlap": []}
            for bno, local in enumerate(np.array_split(np.arange(len(eval_y)), 4), 1):
                if not len(local):
                    continue
                block_k = max(1, int(math.ceil(len(local) * .05)))
                lr_top = set(local[np.argsort(lr[local])[-block_k:]])
                gb_top = set(local[np.argsort(gb[local])[-block_k:]])
                overlap = lr_top & gb_top
                pair["block_overlap"].append({"block": bno, "support_each": block_k,
                    "overlap": len(overlap), "successful_overlap": int(sum(bool(eval_y[i]) for i in overlap)),
                    "rank_correlation": float(spearmanr(lr[local], gb[local]).statistic)})
        old = self.repository.load_application_state("selective_opportunity_v3") or {}
        deciles = old.get("evidence_deciles", [])
        rates = [row.get("precision") for row in deciles if row.get("precision") is not None]
        monotonic = {"decile_rates_bottom_to_top": rates,
                     "adjacent_increases": sum(b > a for a, b in zip(rates, rates[1:])),
                     "adjacent_pairs": max(0, len(rates) - 1),
                     "strictly_monotonic": bool(len(rates) > 1 and all(b > a for a, b in zip(rates, rates[1:]))),
                     "top_decile_beats_bottom": bool(len(rates) > 1 and rates[-1] > rates[0])}
        successful_blocks = [b for b in block_metrics
                             if b.get("models", {}).get("logistic_regression", {}).get("top_1%", {}).get("lift", -1) > 0]
        failed_blocks = [b for b in block_metrics if b not in successful_blocks]
        compared_features = ("pattern_sequence_5_successes", "pattern_sequence_5_rate",
                             "recent_100_target_rate", "recent_100_volatility", "recent_100_entropy")
        works_means = {name: (float(np.mean([b["features"][name]["mean"] for b in successful_blocks]))
                              if successful_blocks else None) for name in compared_features}
        fails_means = {name: (float(np.mean([b["features"][name]["mean"] for b in failed_blocks]))
                              if failed_blocks else None) for name in compared_features}
        works_vs_fails = {name: {"ranking_works_mean": works_means[name],
                                 "ranking_fails_mean": fails_means[name],
                                 "difference_works_minus_fails": (works_means[name] - fails_means[name]
                                    if works_means[name] is not None and fails_means[name] is not None else None)}
                          for name in compared_features}
        payload = {"version": "v4-diagnostic-only-2026-10", "status": "DIAGNOSTIC_ONLY",
                   "source": "recreated V3 chronological train/evaluation split before V1 diagnostic boundary",
                   "old_untouched_test": "DIAGNOSTIC ONLY; not read or evaluated",
                   "training_targets": train_end, "evaluation_targets": len(eval_y),
                   "baseline": float(np.mean(eval_y)), "model_precision_coverage": curves,
                   "calibration_by_score_decile": calibration,
                   "score_tie_diagnostics": score_ties,
                   "model_block_stability": block_stability,
                   "sequence5_feature_associations": seq5_associations,
                   "most_stable_model_descriptive": (min(block_stability,
                       key=lambda name: (-block_stability[name]["positive_lift_blocks"],
                                         block_stability[name]["lift_stddev"] if block_stability[name]["lift_stddev"] is not None else float("inf")))
                       if block_stability else None),
                   "performance_by_chronological_block": block_metrics,
                   "ranking_works_blocks": [b["block"] for b in successful_blocks],
                   "ranking_fails_blocks": [b["block"] for b in failed_blocks],
                   "works_vs_fails_feature_means": works_vs_fails,
                   "block_label_rule": "RANKING_WORKS when frozen Logistic top-1% lift is positive within block; descriptive only",
                   "works_vs_fails_replication": {"working_blocks": len(successful_blocks),
                       "failed_blocks": len(failed_blocks),
                       "evidence_state": "INSUFFICIENT_BLOCK_REPLICATION" if len(successful_blocks) < 2 or len(failed_blocks) < 2 else "DESCRIPTIVE_ONLY",
                       "predictive_regime_claim": False},
                   "logistic_vs_gradient_boosting": pair,
                   "ranking_monotonicity": monotonic,
                   "feature_names": names, "model_errors": errors,
                   "selection_or_threshold_updated": False,
                   "created_at": _utcnow()}
        return self.repository.save_application_state_once(DIAGNOSTICS_KEY, payload, payload["created_at"])

    @staticmethod
    def _feature_target_association(values, labels):
        values = np.asarray(values, dtype=float)
        labels = np.asarray(labels, dtype=int)
        if not len(values):
            return {"support": 0, "baseline": None, "groups": []}
        exact = np.all(np.equal(values, np.floor(values))) and len(np.unique(values)) <= 20
        if exact:
            bins = [(float(value), float(value), values == value) for value in np.unique(values)]
        else:
            edges = np.unique(np.quantile(values, [0, .25, .5, .75, 1]))
            bins = [(float(left), float(right), (values >= left) &
                     (values <= right if position == len(edges) - 2 else values < right))
                    for position, (left, right) in enumerate(zip(edges[:-1], edges[1:]))]
        baseline = float(np.mean(labels))
        groups = []
        for left, right, mask in bins:
            n = int(mask.sum())
            hits = int(labels[mask].sum()) if n else 0
            rate = hits / n if n else None
            groups.append({"value" if exact else "range": left if exact else [left, right],
                "support": n, "successes": hits, "target_rate": rate,
                "baseline": baseline, "lift": rate - baseline if rate is not None else None,
                "ci95": wilson(hits, n),
                "evidence_state": "INSUFFICIENT_SUPPORT" if n < 30 else "DESCRIPTIVE_ONLY",
                "promotion_eligible": False})
        block_rows = []
        for block_no, indexes in enumerate(np.array_split(np.arange(len(labels)), 4), 1):
            if not len(indexes):
                continue
            local_x, local_y = values[indexes], labels[indexes]
            local_base = float(np.mean(local_y))
            cutoff = float(np.quantile(local_x, .75))
            mask = local_x >= cutoff
            n = int(mask.sum()); hits = int(local_y[mask].sum())
            rate = hits / n if n else None
            block_rows.append({"block": block_no, "support": int(len(indexes)),
                "feature_support": n, "successes": hits, "baseline": local_base,
                "target_rate": rate, "lift": rate - local_base if rate is not None else None,
                "ci95": wilson(hits, n),
                "direction": "POSITIVE" if rate is not None and rate > local_base else
                    "NEGATIVE_OR_FLAT" if rate is not None else "INSUFFICIENT",
                "evidence_state": "INSUFFICIENT_SUPPORT" if n < 30 else "DESCRIPTIVE_ONLY"})
        return {"support": int(len(labels)), "baseline": baseline, "groups": groups,
                "chronological_blocks": block_rows}

    @staticmethod
    def _latest_features(frame: pd.DataFrame) -> tuple[dict[str, float], dict[str, Any]]:
        values = frame.multiplier.to_numpy(float)
        if len(values) < 102:
            raise ValueError("at least 102 verified contiguous rounds are required")
        from app.ml.opportunity import _bucket

        counts: dict[tuple[int, ...], list[int]] = {}
        past_hits = past_count = 0
        for source in range(100, len(values) - 1):
            hist = values[:source + 1]
            key = tuple(map(int, _bucket(hist[-1000:])[-5:]))
            pair = counts.setdefault(key, [0, 0])
            label = int(values[source + 1] >= TARGET)
            pair[0] += 1
            pair[1] += label
            past_count += 1
            past_hits += label
        current_key = tuple(map(int, _bucket(values[-1000:])[-5:]))
        count, hits = counts.get(current_key, [0, 0])
        baseline = (past_hits + 10) / (past_count + 20)
        features = {
            "pattern_sequence_5_successes": float(hits),
            "pattern_sequence_5_rate": float((hits + 10 * baseline) / (count + 10)),
        }
        return features, {"support": int(count), "successes": int(hits),
                          "success_rate": hits / count if count else None,
                          "smoothed_rate": features["pattern_sequence_5_rate"],
                          "past_baseline": float(baseline), "ci95": wilson(hits, count)}

    def observe(self, rounds: pd.DataFrame, collector_latest_round_id: str | None = None,
                *, already_validated: bool = False):
        if rounds.empty:
            return None
        clean = rounds if already_validated else validated_rounds(rounds.copy())[0]
        if clean.empty:
            return None
        from app.ml.opportunity_v2 import V1_SOURCE_END_INDEX
        segments = self._verified_segments(clean)
        if not segments:
            return None
        left, right = segments[-1]
        state = clean.iloc[left:right].reset_index(drop=True)
        source = state.iloc[-1]
        if int(source.round_index) <= V1_SOURCE_END_INDEX:
            return None
        if collector_latest_round_id and str(source.round_id) != str(collector_latest_round_id):
            return None
        if self.repository.get_v4_assessment(str(source.round_id)):
            return None
        config = self.repository.load_application_state(CONFIG_KEY)
        if not config or config.get("status") != "FROZEN":
            return None
        pattern, state_lengths = self._pattern_state(state.multiplier.to_numpy(float)) if len(state) >= 3 else (None, {})
        if len(state) < 102:
            now = _utcnow()
            warmup = {
                "assessment_id": hashlib.sha256(f"{MODEL_VERSION}|{source.round_id}".encode()).hexdigest(),
                "model_version": MODEL_VERSION,
                "feature_version": config.get("feature_version", "v3-sequence5-successes-rate-v1"),
                "score_version": config.get("score_version", "frozen-logistic-probability-v1"),
                "round_id": str(source.round_id), "round_index": int(source.round_index),
                "target_round_id": None, "target_round_index": int(source.round_index) + 1,
                "observed_at": now, "created_at": now,
                "feature_snapshot": {"verified_segment_rounds": int(len(state)), "required_rounds": 102,
                                     "sequence_state_3": pattern,
                                     "sequence_state_5": ("".join(np.select([state.multiplier.to_numpy(float) < 1.2,
                                         state.multiplier.to_numpy(float) < 1.5, state.multiplier.to_numpy(float) < 2.0,
                                         state.multiplier.to_numpy(float) < 3.0, state.multiplier.to_numpy(float) < 5.0],
                                         ["A", "B", "C", "D", "E"], default="F")[-5:]) if len(state) >= 5 else None)},
                "model_scores": {}, "pattern_score": None, "analog_score": None,
                "opportunity_score": None, "coverage_rank": None,
                "coverage_rank_status": "NOT_SCORABLE_CONTINUITY_WARMUP",
                "regime_state": {"classification": "UNKNOWN_WARMUP", "predictive_claim": False},
                "candidate_state": "WAITING_CONTINUITY_WARMUP",
                "model_configuration_hash": config["configuration_hash"],
                "score_components": {"ml_contribution": None, "sequence_contribution": None,
                    "seq5_contribution": None, "analog_contribution": None,
                    "baseline_adjustment": None, "regime_contribution": None,
                    "uncertainty_penalty": None, "note": "No score is emitted before the frozen feature history is available."},
                "seq5_evidence": {"support": 0, "successes": 0, "state": "INSUFFICIENT_CONTIGUOUS_HISTORY"},
                "sequence_state_3": pattern, "bba_research_occurrence": pattern == "BBA",
                "stability": {"state": "STABILITY_UNKNOWN", "evidence": "continuity warm-up incomplete"},
                "target_threshold": TARGET, "candidate_is_prediction": False,
                "signal": False, "execution_eligible": False, "assessment_immutable": True,
                "scorable": False, "failed_gate": "CONTINUITY_WARMUP",
                "failed_reason": f"verified_segment_rounds={len(state)}; required=102",
            }
            return self.repository.save_v4_assessment(warmup)
        try:
            features, seq5 = self._latest_features(state)
        except ValueError:
            return None
        x = np.asarray([features[name] for name in config["feature_names"]], dtype=float)
        scaled = (x - np.asarray(config["feature_mean"], dtype=float)) / np.asarray(config["feature_scale"], dtype=float)
        terms = scaled * np.asarray(config["coefficients"], dtype=float)
        logit = float(np.sum(terms) + config["intercept"])
        probability = _sigmoid(logit)
        reference_cutoffs = config.get("coverage_reference", {}).get("cutoffs", {})
        reference_size = int(config.get("coverage_reference", {}).get("scores", 0))
        reference_scores = np.asarray(config.get("coverage_reference", {}).get("sorted_scores", []), dtype=float)
        coverage_rank = (float(np.searchsorted(reference_scores, probability, side="right") / len(reference_scores))
                         if len(reference_scores) else None)
        coverage = next((float(label[4:-1]) / 100 for label, cutoff in sorted(
            reference_cutoffs.items(), key=lambda item: float(item[0][4:-1])) if probability >= float(cutoff)), None)
        sequence5 = "".join(np.select([state.multiplier.to_numpy(float) < 1.2,
            state.multiplier.to_numpy(float) < 1.5, state.multiplier.to_numpy(float) < 2.0,
            state.multiplier.to_numpy(float) < 3.0, state.multiplier.to_numpy(float) < 5.0],
            ["A", "B", "C", "D", "E"], default="F")[-5:])
        values = state.multiplier.to_numpy(float)
        tail = values[-min(100, len(values)):]
        tail_states = np.select([tail < 1.2, tail < 1.5, tail < 2.0,
                                 tail < 3.0, tail < 5.0], [0, 1, 2, 3, 4], default=5)
        _, tail_counts = np.unique(tail_states, return_counts=True)
        tail_p = tail_counts / tail_counts.sum()
        entropy = float(-(tail_p * np.log2(tail_p)).sum())
        # V3's four chronological OOS blocks included failed and successful
        # periods. This prior evidence means the ranking is currently LOW.
        stability = self.stability_state()
        now = _utcnow()
        assessment = {
            "assessment_id": hashlib.sha256(f"{MODEL_VERSION}|{source.round_id}".encode()).hexdigest(),
            "model_version": MODEL_VERSION,
            "feature_version": config.get("feature_version", "v3-sequence5-successes-rate-v1"),
            "score_version": config.get("score_version", "frozen-logistic-probability-v1"),
            "round_id": str(source.round_id), "round_index": int(source.round_index),
            "target_round_id": None, "target_round_index": int(source.round_index) + 1,
            "observed_at": now, "created_at": now, "feature_snapshot": {**features,
                "sequence5_state": sequence5, "last_multiplier": float(values[-1]),
                "recent_100_mean": float(np.mean(tail)), "recent_100_volatility": float(np.std(tail)),
                "recent_100_entropy": entropy, "recent_100_target_rate": float(np.mean(tail >= TARGET)),
                "recent_100_threshold_frequencies": {f">={threshold:g}x": float(np.mean(tail >= threshold))
                    for threshold in (1.2, 1.5, 2.0, 2.1, 3.0, 5.0, 10.0)}},
            "model_scores": {"logistic_regression_probability": probability,
                             "linear_predictor": logit},
            "pattern_score": pattern, "analog_score": None,
            "opportunity_score": probability,
            "coverage_rank": coverage_rank,
            "coverage_rank_semantics": "fraction of frozen reference scores at or below this score",
            "coverage_rank_status": ("FROZEN_REFERENCE_COVERAGE_BAND" if reference_size else "UNAVAILABLE_NO_FROZEN_REFERENCE_DISTRIBUTION"),
            "coverage_band": f"TOP_{coverage:g}%" if coverage is not None else None,
            "coverage_reference_scores": reference_size,
            "regime_state": self._regime_snapshot(state),
            "candidate_state": ("RESEARCH_CANDIDATE" if stability["state"] == "STABILITY_ACCEPTABLE"
                                and coverage is not None and coverage <= .05 else
                                "ABSTAIN_STABILITY_LOW" if stability["state"] == "STABILITY_LOW" else
                                "ABSTAIN_STABILITY_UNKNOWN" if stability["state"] == "STABILITY_UNKNOWN" else
                                "RANKED_BELOW_RESEARCH_CUTOFF"),
            "model_configuration_hash": config["configuration_hash"],
            "score_components": {
                "ml_contribution": 0.0,
                "sequence_contribution": 0.0,
                "seq5_contribution": float(np.sum(terms)),
                "seq5_feature_terms_logit_scale": {name: float(value) for name, value in zip(config["feature_names"], terms)},
                "analog_contribution": None,
                "baseline_adjustment": float(config["intercept"]),
                "regime_contribution": 0.0,
                "uncertainty_penalty": None,
                "ml_transform": "frozen Logistic Regression sigmoid; no separate non-seq5 model term exists",
                "decomposition_scale": "additive contributions are on logit scale; unsupported analog/regime/uncertainty components are unavailable, not imputed",
                "reconstructed_logit": float(0.0 + 0.0 + np.sum(terms) + config["intercept"] + 0.0),
                "note": "Frozen V3 Logistic ranking is unchanged; ML's nonlinear probability transform is reported separately from additive logit contributions.",
            },
            "seq5_evidence": seq5, "sequence_state_3": pattern,
            "bba_research_occurrence": pattern == "BBA",
            "stability": stability, "target_threshold": TARGET,
            "candidate_is_prediction": False, "signal": False, "execution_eligible": False,
            # V3 has no configured live selection gate. Keep the score as a
            # diagnostic and fail closed instead of promoting score rank to a
            # selected opportunity. This is frozen with the assessment before
            # the target row is allowed into PostgreSQL.
            "selection_gate_status": "SELECTIVE_GATE_NOT_DEFINED",
            "selection_state": "NO_SIGNAL",
            "selected": False,
            "selection_reason": "SELECTIVE_GATE_NOT_DEFINED_FAIL_CLOSED",
            "selection_decided_at": now,
            "assessment_immutable": True, "scorable": True,
        }
        return self.repository.save_v4_assessment(assessment)

    @staticmethod
    def _order_proof_verified(assessment: dict, outcome: dict, configuration_hash: str | None = None) -> bool:
        """Accept outcomes only when their immutable source/target order is proven."""
        proof = outcome.get("round_order_proof") or {}
        try:
            source_index = int(assessment["round_index"])
            target_index = int(outcome["target_round_index"])
        except (KeyError, TypeError, ValueError):
            return False
        if (not bool(assessment.get("assessment_immutable"))
                or source_index + 1 != target_index
                or str(assessment.get("round_id")) == str(outcome.get("target_round_id") or "")
                or proof.get("assessment_source_round_index") != source_index
                or proof.get("target_outcome_round_index") != target_index
                or proof.get("consecutive_indices") is not True
                or proof.get("assessment_inserted_while_source_latest") is not True
                or proof.get("target_absent_at_assessment_commit") is not True):
            return False
        assessment_proof = assessment.get("round_order_proof")
        if assessment_proof is not None and (
                assessment_proof.get("assessment_source_round_index") != source_index
                or assessment_proof.get("assessment_source_round_id") != str(assessment.get("round_id"))
                or assessment_proof.get("latest_round_at_assessment_commit_index") != source_index
                or assessment_proof.get("latest_round_at_assessment_commit_id") != str(assessment.get("round_id"))
                or assessment_proof.get("target_outcome_round_index") != target_index
                or assessment_proof.get("source_was_latest_at_assessment_commit") is not True
                or assessment_proof.get("target_absent_at_assessment_commit") is not True
                or assessment_proof.get("proof_method") != "postgres_advisory_transaction_lock"):
            return False
        if configuration_hash and assessment.get("model_configuration_hash") != configuration_hash:
            return False
        return True

    @staticmethod
    def _resolved_continuity(assessments, outcomes, repository) -> dict[str, Any]:
        outcome_by_id = {row["assessment_id"]: row for row in outcomes}
        assessment_by_target: dict[int, list[dict]] = {}
        for assessment in assessments:
            try:
                target = int(assessment.get("target_round_index", int(assessment["round_index"]) + 1))
            except (KeyError, TypeError, ValueError):
                continue
            assessment_by_target.setdefault(target, []).append(assessment)

        scorable = [row for row in assessments if row.get("scorable")]
        config = repository.load_application_state(CONFIG_KEY) or {}
        resolved = [
            (int(outcome_by_id[row["assessment_id"]]["target_round_index"]), row,
             outcome_by_id[row["assessment_id"]])
            for row in scorable if row["assessment_id"] in outcome_by_id
            and OpportunityStabilityV4._order_proof_verified(
                row, outcome_by_id[row["assessment_id"]], config.get("configuration_hash"))
        ]
        resolved.sort(key=lambda entry: entry[0])
        indexes = [entry[0] for entry in resolved]
        runs: list[list[int]] = []
        run: list[int] = []
        for index in indexes:
            if run and index != run[-1] + 1:
                runs.append(run)
                run = []
            run.append(index)
        if run:
            runs.append(run)

        reasons = Counter()
        breaks = []
        round_rows = (repository.round_continuity_range(indexes[0] + 1, indexes[-1] - 1)
                      if len(indexes) > 1 and indexes[-1] > indexes[0] + 1 else {})
        for left, right in zip(indexes, indexes[1:]):
            if right == left + 1:
                continue
            missing = list(range(left + 1, right))
            classified = []
            for target_index in missing:
                candidates = assessment_by_target.get(target_index, [])
                round_row = round_rows.get(target_index)
                detail = None
                if len(candidates) > 1:
                    reason = "DUPLICATE"
                elif round_row is None:
                    reason = "TARGET_MISSING"
                elif bool(round_row.get("gap_before")):
                    reason = "COLLECTOR_GAP"
                elif not bool(round_row.get("continuity_verified")) or str(
                        round_row.get("identity_confidence") or "UNKNOWN").upper() in {"UNKNOWN", "UNVERIFIED"}:
                    reason = "IDENTITY_UNVERIFIED"
                elif not candidates:
                    # Older rounds do not have a durable observer heartbeat
                    # history, so absence of a score is reported literally.
                    reason = "NO_ASSESSMENT"
                elif not candidates[0].get("scorable"):
                    reason = "OTHER"
                    detail = "ASSESSMENT_NOT_SCORABLE:" + str(candidates[0].get("failed_gate") or "UNKNOWN")
                elif candidates[0]["assessment_id"] not in outcome_by_id:
                    reason = "UNRESOLVED"
                else:
                    reason = "OTHER"
                reasons[reason] += 1
                classified.append((target_index, reason, detail))
            grouped = []
            for target_index, reason, detail in classified:
                if (grouped and grouped[-1]["reason"] == reason
                        and grouped[-1].get("detail") == detail
                        and grouped[-1]["last_target_index"] + 1 == target_index):
                    grouped[-1]["last_target_index"] = target_index
                    grouped[-1]["rounds"] += 1
                else:
                    grouped.append({"reason": reason, "first_target_index": target_index,
                                    "last_target_index": target_index, "rounds": 1,
                                    **({"detail": detail} if detail else {})})
            breaks.append({"previous_resolved_target_index": left,
                           "next_resolved_target_index": right,
                           "missing_target_rounds": len(missing), "reasons": grouped})
        return {
            "current_contiguous_resolved": len(runs[-1]) if runs else 0,
            "longest_contiguous_resolved": max((len(values) for values in runs), default=0),
            "resolved_scorable_assessments": len(resolved),
            "unresolved_scorable_assessments": max(0, len(scorable) - len(resolved)),
            "break_reason_counts": dict(reasons),
            "breaks": breaks,
        }

    def observer_status(self) -> dict[str, Any]:
        """Lightweight durable observer and continuity status; avoids diagnostics work."""
        assessments = self.repository.list_v4_assessments()
        outcomes = self.repository.list_v4_outcomes()
        config = self.repository.load_application_state(CONFIG_KEY) or {}
        runtime = self.repository.load_application_state(OBSERVER_RUNTIME_KEY) or {}
        continuity = self._resolved_continuity(assessments, outcomes, self.repository)
        try:
            latest_marker = self.repository.latest_round_marker()
        except Exception:
            latest_marker = None
        from app.ml.opportunity_v2 import V1_SOURCE_END_INDEX
        try:
            observed_rounds = self.repository.count_rounds_after(V1_SOURCE_END_INDEX)
        except Exception:
            observed_rounds = None
        try:
            verified_suffix = self.repository.load_verified_round_suffix()
            verified_suffix_rounds = len(verified_suffix)
        except Exception:
            verified_suffix_rounds = None
        heartbeat = runtime.get("heartbeat_at")
        heartbeat_age = None
        if heartbeat:
            try:
                timestamp = datetime.fromisoformat(str(heartbeat).replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                heartbeat_age = max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds())
            except ValueError:
                pass
        processed_at = runtime.get("last_processed_at")
        processed_age = None
        if processed_at:
            try:
                timestamp = datetime.fromisoformat(str(processed_at).replace("Z", "+00:00"))
                if timestamp.tzinfo is None:
                    timestamp = timestamp.replace(tzinfo=timezone.utc)
                processed_age = max(0.0, (datetime.now(timezone.utc) - timestamp).total_seconds())
            except ValueError:
                pass
        latest_index = int(latest_marker[0]) if latest_marker else None
        latest_round_id = str(latest_marker[1]) if latest_marker else None
        processed_index = runtime.get("last_processed_round_index")
        caught_up = bool(latest_marker and processed_index is not None
                         and int(processed_index) == latest_index
                         and str(runtime.get("last_processed_round_id")) == latest_round_id
                         and str(runtime.get("last_assessment_round_id")) == latest_round_id)
        lag_rounds = max(0, latest_index - int(processed_index)) if latest_index is not None and processed_index is not None else None
        latest_assessment = assessments[-1] if assessments else None
        last_resolved = max(outcomes, key=lambda row: int(row.get("target_round_index", 0)), default=None)
        active = bool(config.get("status") == "FROZEN" and runtime.get("state") == "RUNNING"
                      and heartbeat_age is not None
                      and heartbeat_age <= OBSERVER_HEARTBEAT_STALE_SECONDS)
        runtime_state = runtime.get("state") or "NOT_STARTED"
        reason = runtime.get("reason")
        if config.get("status") != "FROZEN":
            runtime_state = "PAUSED_REQUIRES_OPERATOR"
            reason = "frozen V3 configuration is unavailable"
        elif heartbeat_age is None or heartbeat_age > OBSERVER_HEARTBEAT_STALE_SECONDS:
            runtime_state = "PAUSED_REQUIRES_OPERATOR"
            reason = "observer heartbeat is missing or stale"
        elif latest_marker and not caught_up and (
                processed_age is None or processed_age > OBSERVER_HEARTBEAT_STALE_SECONDS):
            runtime_state = "DEGRADED"
            reason = "latest persisted round marker has not been assessed"
        return {
            "observer_active": active,
            "observer_state": runtime_state,
            "observer_reason": reason,
            "frozen_model_version": config.get("version"),
            "frozen_configuration_hash": config.get("configuration_hash"),
            "feature_version": config.get("feature_version") or (latest_assessment or {}).get("feature_version"),
            "score_version": config.get("score_version") or (latest_assessment or {}).get("score_version"),
            "observed_rounds": observed_rounds,
            "verified_contiguous_rounds": verified_suffix_rounds,
            "scorable_assessments": sum(bool(row.get("scorable")) for row in assessments),
            **continuity,
            "assessments_total": len(assessments),
            "resolved_assessments_total": len(outcomes),
            "last_assessment_round_index": int(latest_assessment["round_index"]) if latest_assessment else None,
            "last_assessment_round_id": latest_assessment.get("round_id") if latest_assessment else None,
            "last_assessment_id": latest_assessment.get("assessment_id") if latest_assessment else None,
            "last_assessment_created_at": latest_assessment.get("created_at") if latest_assessment else None,
            "last_resolved_round_index": int(last_resolved["target_round_index"]) if last_resolved else None,
            "last_resolved_round_id": last_resolved.get("target_round_id") if last_resolved else None,
            "last_observer_heartbeat": heartbeat,
            "observer_heartbeat_age_seconds": heartbeat_age,
            "observer_last_processed_age_seconds": processed_age,
            "observer_caught_up_to_latest_round": caught_up,
            "real_data_alignment": real_data_alignment_snapshot(runtime),
            "observer_lag_rounds": lag_rounds,
            "latest_round_index": latest_index,
            "latest_round_id": latest_round_id,
            "last_processed_round_index": runtime.get("last_processed_round_index"),
            "last_processed_round_id": runtime.get("last_processed_round_id"),
            "last_processed_at": runtime.get("last_processed_at"),
            "last_error": runtime.get("last_error"),
        }

    @staticmethod
    def _assessment_order_proof_verified(assessment: dict[str, Any], configuration_hash: str | None) -> bool:
        proof = assessment.get("round_order_proof") or {}
        try:
            source_index = int(assessment["round_index"])
            target_index = int(assessment.get("target_round_index", source_index + 1))
        except (KeyError, TypeError, ValueError):
            return False
        return bool(
            assessment.get("assessment_immutable") is True
            and target_index == source_index + 1
            and proof.get("assessment_source_round_index") == source_index
            and proof.get("assessment_source_round_id") == str(assessment.get("round_id"))
            and proof.get("latest_round_at_assessment_commit_index") == source_index
            and proof.get("latest_round_at_assessment_commit_id") == str(assessment.get("round_id"))
            and proof.get("target_outcome_round_index") == target_index
            and proof.get("source_was_latest_at_assessment_commit") is True
            and proof.get("target_absent_at_assessment_commit") is True
            and proof.get("proof_method") == "postgres_advisory_transaction_lock"
            and (not configuration_hash or assessment.get("model_configuration_hash") == configuration_hash)
        )

    @staticmethod
    def _live_result_row(rank: int, assessment: dict[str, Any], outcome: dict[str, Any] | None) -> dict[str, Any]:
        model_scores = assessment.get("model_scores") or {}
        model_score = assessment.get("model_score", model_scores.get("logistic_regression_probability"))
        target_index = int(assessment.get("target_round_index", int(assessment["round_index"]) + 1))
        actual = float(outcome["actual_multiplier"]) if outcome and outcome.get("actual_multiplier") is not None else None
        result = "TRUE" if actual is not None and actual >= TARGET else "FALSE" if actual is not None else None
        true_2_10x = (outcome.get("is_true_2_10x", outcome.get("target_hit_2_10x"))
                      if outcome is not None else None)
        if true_2_10x is None and actual is not None:
            true_2_10x = actual >= TARGET
        return {
            "rank": rank,
            "current_display_rank": rank,
            "assessment_id": assessment.get("assessment_id"),
            "target_round_id": (outcome or {}).get("target_round_id") or assessment.get("target_round_id"),
            "target_round_index": target_index,
            "opportunity_score": assessment.get("opportunity_score"),
            "model_score": model_score,
            "model_score_label": "frozen logistic raw score; calibration not claimed",
            "pattern_score": assessment.get("pattern_score"),
            "analog_score": assessment.get("analog_score"),
            "stability_state": assessment.get("stability_state") or (assessment.get("stability") or {}).get("state"),
            "evidence": {
                "sequence_state_5": (assessment.get("feature_snapshot") or {}).get("sequence5_state"),
                "sequence": assessment.get("seq5_evidence"),
                "coverage_band": assessment.get("coverage_band"),
            },
            "created_at": assessment.get("created_at") or assessment.get("observed_at"),
            "rank_at_observation": assessment.get("rank_at_observation"),
            "selection_state": assessment.get("selection_state"),
            "selected": assessment.get("selected") is True,
            "selected_at": assessment.get("selection_decided_at"),
            "selection_reason": assessment.get("selection_reason"),
            "actual_multiplier": actual,
            "is_true_2_10x": bool(true_2_10x) if true_2_10x is not None else None,
            "result": result,
            "status": result or assessment.get("status") or "PENDING_RESULT",
            "resolved_at": (outcome or {}).get("resolved_at"),
        }

    @staticmethod
    def _frozen_selection_gate(config: dict[str, Any]) -> dict[str, Any] | None:
        """Return only an explicitly frozen, V3-hash-bound live gate.

        The current frozen V3 artifact has no such object. A score distribution,
        research cutoff, stability diagnostic, or rank is not a live selection
        gate by itself.
        """
        gate = config.get("selection_gate") if isinstance(config, dict) else None
        if not isinstance(gate, dict):
            return None
        if (gate.get("status") != "FROZEN"
                or not gate.get("policy_version")
                or gate.get("configuration_hash") != config.get("configuration_hash")
                or not isinstance(gate.get("policy"), dict)):
            return None
        return gate

    @classmethod
    def _selected_before_outcome(cls, assessment: dict[str, Any],
                                 configuration_hash: str | None,
                                 gate: dict[str, Any] | None) -> bool:
        """Accept only an explicit selection frozen into a proven pre-outcome row."""
        if gate is None or assessment.get("selection_state") != "SELECTED_OPPORTUNITY":
            return False
        if assessment.get("selected") is not True:
            return False
        if assessment.get("selection_gate_hash") != gate.get("configuration_hash"):
            return False
        if assessment.get("selection_frozen_with_assessment") is not True:
            return False
        return cls._assessment_order_proof_verified(assessment, configuration_hash)

    @classmethod
    def _display_selection_state(cls, assessment: dict[str, Any], gate: dict[str, Any] | None,
                                 configuration_hash: str | None) -> tuple[str, str]:
        state = assessment.get("selection_state")
        if state == "SELECTED_OPPORTUNITY" and cls._selected_before_outcome(
                assessment, configuration_hash, gate):
            return state, str(assessment.get("selection_reason") or "frozen gate selected before outcome")
        if state == "WATCH" and gate is not None and cls._assessment_order_proof_verified(
                assessment, configuration_hash):
            return state, str(assessment.get("selection_reason") or "frozen gate watch state")
        if state == "NO_SIGNAL" and cls._assessment_order_proof_verified(assessment, configuration_hash):
            return state, str(assessment.get("selection_reason") or "frozen no-signal decision")
        # Legacy V3 assessments froze signal=False, candidate_is_prediction=False
        # and their research/abstention state before the target outcome. Preserve
        # that diagnostic state without treating score rank as selection.
        if (assessment.get("signal") is False
                and assessment.get("candidate_is_prediction") is False
                and cls._assessment_order_proof_verified(assessment, configuration_hash)):
            return "NO_SIGNAL", ("SELECTIVE_GATE_NOT_DEFINED; V3 assessment was diagnostic and not selected"
                                  if gate is None else "assessment explicitly recorded signal=false")
        if gate is None:
            return "SELECTIVE_GATE_NOT_DEFINED", "no frozen live selection gate is configured"
        return "WATCH", "no explicit selected-opportunity decision was stored"

    @classmethod
    def _rolling_window_report(cls, resolved_run: list[tuple[dict[str, Any], dict[str, Any]]], size: int) -> dict[str, Any]:
        progress = min(len(resolved_run), size)
        report: dict[str, Any] = {"window_size": size, "status": "WAITING", "progress": progress,
                                  "required": size, "selected": [], "true": None, "false": None,
                                  "precision": None, "baseline": None, "lift": None}
        if len(resolved_run) < size:
            return report
        sample = resolved_run[-size:]
        ranked = sorted(sample, key=lambda pair: (-float(pair[0]["opportunity_score"]),
                                                   int(pair[0].get("target_round_index", 0))))
        selected = [cls._live_result_row(rank, assessment, outcome) for rank, (assessment, outcome) in enumerate(ranked[:4], 1)]
        true_count = sum(row["result"] == "TRUE" for row in selected)
        false_count = sum(row["result"] == "FALSE" for row in selected)
        baseline_true = sum(float(outcome["actual_multiplier"]) >= TARGET for _, outcome in sample)
        precision = true_count / 4.0
        baseline = baseline_true / float(size)
        report.update({
            "status": "COMPLETE",
            "start_target_round_index": int(sample[0][0].get("target_round_index", 0)),
            "end_target_round_index": int(sample[-1][0].get("target_round_index", 0)),
            "selected": selected, "true": true_count, "false": false_count,
            "precision": precision, "baseline_true": int(baseline_true),
            "baseline_rounds": size, "baseline": baseline,
            "lift": precision - baseline,
            "rank_performance": {f"rank_{row['rank']}": {
                "true": int(row["result"] == "TRUE"), "false": int(row["result"] == "FALSE"),
                "hit_rate": 1.0 if row["result"] == "TRUE" else 0.0,
                "support": 1,
            } for row in selected},
            "window_policy": "latest rolling block of consecutive verified prospective resolved assessments; four highest frozen scores",
        })
        return report

    def lightweight_live_dashboard(self) -> dict[str, Any]:
        """Return the Models-page live state without loading research history.

        The detailed diagnostic board decodes every assessment/outcome and is
        not a suitable three-second polling endpoint. Keep this response to
        indexed latest-row reads, the current verified suffix, and the durable
        experiment ledger so dashboard traffic cannot starve live scoring.
        """
        config = self.repository.load_application_state(CONFIG_KEY) or {}
        runtime = self.repository.load_application_state(OBSERVER_RUNTIME_KEY) or {}
        version = config.get("version") if config.get("status") == "FROZEN" else None
        model_loaded = bool(version and config.get("configuration_hash"))
        latest_round = self.repository.latest_round_live_details()
        latest_assessment = (self.repository.latest_v4_assessment(version) if version else None)
        try:
            verified_count = len(self.repository.load_verified_round_suffix())
        except Exception:
            verified_count = 0
        required = 102
        warmup_complete = verified_count >= required
        warmup = {
            "status": "COMPLETE" if warmup_complete else "WARMING_UP",
            "verified_rounds": min(required, verified_count),
            "current_contiguous_verified_rounds": verified_count,
            "required_rounds": required,
            "remaining_rounds": max(0, required - verified_count),
            "estimated_rounds_remaining": max(0, required - verified_count),
            "source_round_index": (latest_round or {}).get("round_index"),
            "reason": None if warmup_complete else "INSUFFICIENT_CONTIGUOUS_HISTORY",
        }
        heartbeat = runtime.get("heartbeat_at")
        heartbeat_age = None
        if heartbeat:
            try:
                stamp = datetime.fromisoformat(str(heartbeat).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                heartbeat_age = max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())
            except ValueError:
                pass
        processed_index = runtime.get("last_processed_round_index")
        latest_index = int(latest_round["round_index"]) if latest_round else None
        caught_up = bool(latest_round and processed_index is not None
                         and int(processed_index) == latest_index
                         and str(runtime.get("last_processed_round_id")) == str(latest_round["round_id"])
                         and str(runtime.get("last_assessment_round_id")) == str(latest_round["round_id"]))
        observer_active = bool(model_loaded and runtime.get("state") == "RUNNING"
                               and heartbeat_age is not None
                               and heartbeat_age <= OBSERVER_HEARTBEAT_STALE_SECONDS)
        if not model_loaded:
            observer_state, observer_reason = "PAUSED_REQUIRES_OPERATOR", "frozen V3 configuration is unavailable"
        elif not observer_active:
            observer_state, observer_reason = "PAUSED_REQUIRES_OPERATOR", "observer heartbeat is missing or stale"
        elif not caught_up:
            observer_state, observer_reason = "CATCHING_UP", runtime.get("reason") or "processing persisted real rounds"
        elif not warmup_complete:
            observer_state, observer_reason = "WARMING_UP", "INSUFFICIENT_CONTIGUOUS_HISTORY"
        else:
            observer_state, observer_reason = "ACTIVE", None
        frozen_status = "NOT_LOADED" if not model_loaded else "ACTIVE" if warmup_complete else "WARMING_UP"
        experiment = self.prospective_experiment_snapshot()
        return {
            "pipeline_state": "LIVE" if observer_active and caught_up else "LIVE_OBSERVATION_PAUSED",
            "automatic_real_money_execution": "OFF",
            "frozen_model_active": bool(model_loaded and warmup_complete),
            "frozen_model_loaded": model_loaded,
            "frozen_model_status": frozen_status,
            "training_status": "OFF",
            "frozen_model_version": version,
            "frozen_configuration_hash": config.get("configuration_hash"),
            "feature_version": config.get("feature_version") or (latest_assessment or {}).get("feature_version"),
            "score_version": config.get("score_version") or (latest_assessment or {}).get("score_version"),
            "observer_active": observer_active,
            "observer_state": observer_state,
            "observer_status": observer_state,
            "observer_reason": observer_reason,
            "observer_heartbeat": heartbeat,
            "observer_heartbeat_age_seconds": heartbeat_age,
            "observer_caught_up_to_latest_round": caught_up,
            "observer_lag_rounds": (max(0, latest_index - int(processed_index))
                                    if latest_index is not None and processed_index is not None else None),
            "last_processed_round_index": processed_index,
            "last_processed_round_id": runtime.get("last_processed_round_id"),
            "last_processed_at": runtime.get("last_processed_at"),
            "last_assessment_round_index": (latest_assessment or {}).get("round_index"),
            "last_assessment_id": (latest_assessment or {}).get("assessment_id"),
            "last_assessment_created_at": ((latest_assessment or {}).get("created_at")
                                            or (latest_assessment or {}).get("observed_at")),
            "last_real_round": latest_round,
            "scoring_warmup": warmup,
            "real_data_alignment": real_data_alignment_snapshot(runtime),
            "observed_rounds": experiment.get("rounds_observed"),
            "scored": experiment.get("rounds_scored", 0),
            "resolved": experiment.get("predictions_resolved", 0),
            "pending": experiment.get("pending_predictions", 0),
            "selection_gate_status": "SELECTIVE_GATE_NOT_DEFINED",
            "real_prospective_experiment": experiment,
        }

    def live_dashboard(self) -> dict[str, Any]:
        """Build a prospective-only display from immutable assessments and real outcomes."""
        assessments = self.repository.list_v4_assessments()
        outcomes = self.repository.list_v4_outcomes()
        config = self.repository.load_application_state(CONFIG_KEY) or {}
        runtime = self.repository.load_application_state(OBSERVER_RUNTIME_KEY) or {}
        configuration_hash = config.get("configuration_hash")
        selection_gate = self._frozen_selection_gate(config)
        selection_gate_status = ("DEFINED_FROZEN" if selection_gate is not None
                                 else "SELECTIVE_GATE_NOT_DEFINED")
        frozen_version = config.get("version") if config.get("status") == "FROZEN" else None
        frozen_assessments = [row for row in assessments if row.get("model_version") == frozen_version]
        latest_persisted_assessment = max(
            frozen_assessments, key=lambda row: int(row.get("round_index", 0)), default=None)
        assessment_by_id = {row.get("assessment_id"): row for row in frozen_assessments}
        outcome_by_id = {str(row.get("assessment_id")): row for row in outcomes}
        verified_assessments = [
            row for row in frozen_assessments if row.get("scorable")
            and (self._assessment_order_proof_verified(row, configuration_hash)
                 or (str(row.get("assessment_id")) in outcome_by_id
                     and self._order_proof_verified(row, outcome_by_id[str(row["assessment_id"])], configuration_hash)))
            and row.get("opportunity_score") is not None
        ]
        verified_assessments.sort(key=lambda row: int(row.get("target_round_index", 0)))
        verified_outcomes: dict[str, dict[str, Any]] = {}
        for outcome in outcomes:
            assessment = assessment_by_id.get(str(outcome.get("assessment_id")))
            if (assessment and assessment.get("scorable")
                    and self._order_proof_verified(assessment, outcome, configuration_hash)):
                verified_outcomes[str(outcome["assessment_id"])] = outcome

        resolved = [(row, verified_outcomes[str(row["assessment_id"])]) for row in verified_assessments
                    if str(row.get("assessment_id")) in verified_outcomes]
        resolved.sort(key=lambda pair: int(pair[1].get("target_round_index", 0)))
        runs: list[list[tuple[dict[str, Any], dict[str, Any]]]] = []
        for pair in resolved:
            target_index = int(pair[1].get("target_round_index", 0))
            if (not runs or target_index != int(runs[-1][-1][1].get("target_round_index", 0)) + 1):
                runs.append([pair])
            else:
                runs[-1].append(pair)
        resolved_run = runs[-1] if runs else []

        candidate_runs: list[list[dict[str, Any]]] = []
        for assessment in verified_assessments:
            target_index = int(assessment.get("target_round_index", 0))
            if (not candidate_runs or target_index != int(candidate_runs[-1][-1].get("target_round_index", 0)) + 1):
                candidate_runs.append([assessment])
            else:
                candidate_runs[-1].append(assessment)
        current_candidates = (candidate_runs[-1] if candidate_runs else [])[-100:]
        # The scored set is diagnostic. Only an explicit gate decision that
        # was frozen with a verified pre-outcome assessment may appear in the
        # selected list. Never promote the four highest scores as live signals.
        selected_candidates = [row for row in current_candidates
                               if self._selected_before_outcome(row, configuration_hash, selection_gate)]
        current_top = sorted(selected_candidates, key=lambda row: (-float(row["opportunity_score"]),
                                                                     int(row.get("target_round_index", 0))))[:4]

        round_context = (self.repository.live_round_continuity_snapshot()
                         if hasattr(self.repository, "live_round_continuity_snapshot")
                         else None)
        if round_context is not None:
            latest_round = round_context.get("latest_round")
            marker = ((int(latest_round["round_index"]), str(latest_round["round_id"]),
                       str(latest_round.get("platform_timestamp") or latest_round.get("timestamp") or ""),
                       float(latest_round["multiplier"])) if latest_round else None)
        else:
            marker = self.repository.latest_round_marker()
            latest_round = (self.repository.latest_round_live_details()
                            if hasattr(self.repository, "latest_round_live_details") else None)
        latest_index = int(marker[0]) if marker else None
        if latest_round is None and marker:
            latest_round = {"round_index": marker[0], "round_id": marker[1],
                            "platform_timestamp": marker[2], "multiplier": marker[3]}
        scoring_warmup = self._live_warmup_snapshot(latest_round, frozen_assessments,
                                                    configuration_hash, round_context)
        heartbeat = runtime.get("heartbeat_at")
        heartbeat_age = None
        if heartbeat:
            try:
                stamp = datetime.fromisoformat(str(heartbeat).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                heartbeat_age = max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())
            except ValueError:
                pass
        processed_index = runtime.get("last_processed_round_index")
        caught_up = bool(marker and processed_index is not None
                         and int(processed_index) == int(marker[0])
                         and str(runtime.get("last_processed_round_id")) == str(marker[1])
                         and str(runtime.get("last_assessment_round_id")) == str(marker[1]))
        alignment_snapshot = real_data_alignment_snapshot(runtime)
        alignment_snapshot["observer_lag"] = (max(0, latest_index - int(processed_index))
                                               if latest_index is not None and processed_index is not None else None)
        # Older audit state retained only the current run, so recover a break
        # from durable round/assessment rows without reconstructing any score.
        audit_rows = list(runtime.get("alignment_audit_rounds") or [])
        if round_context is not None and audit_rows:
            try:
                first_audited_index = int(audit_rows[0]["round_index"])
                last_audited_index = int(audit_rows[-1]["round_index"])
                audit_started = datetime.fromisoformat(
                    str(runtime.get("alignment_audit_started_at")).replace("Z", "+00:00"))
                recent_by_index = {int(row["round_index"]): row
                                   for row in round_context.get("recent_rounds") or []}
                assessments_by_round_id = {str(row.get("round_id")): row for row in frozen_assessments}

                def failed_source(row):
                    stored = row.get("stored_at")
                    if isinstance(stored, str):
                        stored = datetime.fromisoformat(stored.replace("Z", "+00:00"))
                    if isinstance(stored, datetime):
                        stored = (stored.replace(tzinfo=timezone.utc) if stored.tzinfo is None
                                  else stored.astimezone(timezone.utc))
                    if audit_started.tzinfo is None:
                        audit_started_utc = audit_started.replace(tzinfo=timezone.utc)
                    else:
                        audit_started_utc = audit_started.astimezone(timezone.utc)
                    if not stored or stored < audit_started_utc:
                        return None
                    if row.get("gap_before"):
                        reason = "COLLECTOR_GAP"
                    elif not row.get("continuity_verified"):
                        reason = "IDENTITY_UNVERIFIED"
                    elif str(row.get("round_id")) not in assessments_by_round_id:
                        reason = "MISSING_ASSESSMENT"
                    elif not self._assessment_order_proof_verified(
                            assessments_by_round_id[str(row.get("round_id"))], configuration_hash):
                        reason = "IDENTITY_UNVERIFIED"
                    else:
                        return None
                    return reason

                # First check rounds after the latest persisted match whose
                # outcome already exists; they can no longer be assessed safely.
                failure_row = None
                for index in range(last_audited_index + 1, latest_index or last_audited_index):
                    candidate = recent_by_index.get(index)
                    if candidate and failed_source(candidate):
                        failure_row = candidate
                        break
                reset_progress = 0
                reset_round = None
                if failure_row is not None:
                    failed_index = int(failure_row["round_index"])
                    failure_reason = failed_source(failure_row) or "OTHER"
                    reset_progress = alignment_snapshot.get("current_batch_progress", 0)
                    reset_round = failed_index
                    alignment_snapshot["status"] = "UNVERIFIED"
                    alignment_snapshot["current_batch_progress"] = 0
                    alignment_snapshot["consecutive_rounds"] = 0
                    alignment_snapshot["consecutive_aligned_rounds"] = 0
                elif (runtime.get("alignment_tracker_version") != 1
                      and not runtime.get("alignment_last_failure")
                      and len(audit_rows) < REAL_DATA_ALIGNMENT_REQUIRED_ROUNDS):
                    previous_row = recent_by_index.get(first_audited_index - 1)
                    if previous_row and failed_source(previous_row):
                        failure_row = previous_row
                        failed_index = int(previous_row["round_index"])
                        failure_reason = failed_source(previous_row) or "OTHER"
                        reset_progress = 0
                        probe = failed_index - 1
                        while probe in recent_by_index:
                            prior = recent_by_index[probe]
                            prior_assessment = assessments_by_round_id.get(str(prior.get("round_id")))
                            if (not prior.get("continuity_verified") or prior.get("gap_before")
                                    or not prior_assessment or not self._assessment_order_proof_verified(
                                        prior_assessment, configuration_hash)):
                                break
                            reset_progress += 1
                            probe -= 1
                        reset_progress = min(REAL_DATA_ALIGNMENT_REQUIRED_ROUNDS, reset_progress)
                        reset_round = first_audited_index
                if failure_row is not None:
                    failure = {
                        "failed_round_index": failed_index,
                        "reason": failure_reason,
                        "collector_index": latest_index,
                        "observer_index": int(processed_index) if processed_index is not None else None,
                        "observer_lag": alignment_snapshot["observer_lag"],
                        "missing_assessment": failure_reason == "MISSING_ASSESSMENT",
                        "detail": "reconstructed from durable rows; collector/observer indexes are current, not failure-time",
                        "indices_are_current_snapshot": True,
                    }
                    alignment_snapshot["display_status"] = "DEGRADED"
                    alignment_snapshot["last_failure"] = failure
                    alignment_snapshot["reset"] = {
                        "previous_progress": reset_progress,
                        "current_progress": (1 if reset_round == first_audited_index else 0),
                        "round_index": reset_round,
                        "failed_round_index": failed_index,
                        "reason": failure_reason,
                        "detected_at": None,
                        "reconstructed": True,
                    }
            except (KeyError, TypeError, ValueError):
                pass
        observer_active = bool(config.get("status") == "FROZEN" and runtime.get("state") == "RUNNING"
                               and heartbeat_age is not None
                               and heartbeat_age <= OBSERVER_HEARTBEAT_STALE_SECONDS)
        observer_state = runtime.get("state") or "NOT_STARTED"
        observer_reason = runtime.get("reason")
        if config.get("status") != "FROZEN":
            observer_active, observer_state = False, "PAUSED_REQUIRES_OPERATOR"
            observer_reason = "frozen V3 configuration is unavailable"
        elif not observer_active:
            observer_state = "PAUSED_REQUIRES_OPERATOR"
            observer_reason = "observer heartbeat is missing or stale"
        elif not caught_up:
            observer_state = "CATCHING_UP"
            observer_reason = "processing persisted real rounds"

        feature_names = config.get("feature_names")
        coefficients = config.get("coefficients")
        feature_mean = config.get("feature_mean")
        feature_scale = config.get("feature_scale")
        model_loaded = bool(
            config.get("status") == "FROZEN" and config.get("version")
            and config.get("configuration_hash")
            and isinstance(feature_names, list) and len(feature_names) > 0
            and isinstance(coefficients, list) and len(feature_names) == len(coefficients)
            and isinstance(feature_mean, list) and len(feature_names) == len(feature_mean)
            and isinstance(feature_scale, list) and len(feature_names) == len(feature_scale)
        )
        if not model_loaded:
            observer_status = "PAUSED"
        elif not observer_active:
            observer_status = "PAUSED"
        elif not caught_up:
            observer_status = "LAGGING"
        elif scoring_warmup["verified_rounds"] < scoring_warmup["required_rounds"]:
            observer_status = "WARMING_UP"
        else:
            observer_status = "ACTIVE"
        model_warmup_complete = (scoring_warmup["verified_rounds"]
                                 >= scoring_warmup["required_rounds"])
        # Keep model readiness separate from observer/pipeline health. The
        # observer may be catching up while the unchanged frozen model remains
        # loaded and past its verified warm-up.
        frozen_model_status = ("NOT_LOADED" if not model_loaded else
                               "ACTIVE" if model_warmup_complete else "WARMING_UP")

        from app.ml.opportunity_v2 import V1_SOURCE_END_INDEX
        latest_assessment = max(verified_assessments, key=lambda row: int(row.get("round_index", 0)), default=None)
        last_resolved = max((pair[1] for pair in resolved), key=lambda row: int(row.get("target_round_index", 0)), default=None)
        rolling = {str(size): self._rolling_window_report(resolved_run, size)
                   for size in (100, 200, 300, 400, 500)}
        try:
            prospective_experiment = self.prospective_experiment_snapshot()
        except (AttributeError, KeyError):
            prospective_experiment = {"status": "PAUSED", "reason": "durable prospective experiment tables are unavailable"}
        selection_counts = {"no_signal": 0, "watch": 0, "selected": 0, "gate_undefined": 0}
        for assessment in verified_assessments:
            decision, _ = self._display_selection_state(assessment, selection_gate, configuration_hash)
            if decision == "NO_SIGNAL":
                selection_counts["no_signal"] += 1
            elif decision == "WATCH":
                selection_counts["watch"] += 1
            elif decision == "SELECTED_OPPORTUNITY" and self._selected_before_outcome(
                    assessment, configuration_hash, selection_gate):
                selection_counts["selected"] += 1
            else:
                selection_counts["gate_undefined"] += 1

        diagnostic_assessments = verified_assessments[-100:]
        live_internal_scoring = []
        for assessment in diagnostic_assessments:
            assessment_id = str(assessment.get("assessment_id"))
            decision, reason = self._display_selection_state(assessment, selection_gate, configuration_hash)
            outcome = verified_outcomes.get(assessment_id)
            row = self._live_result_row(None, assessment, outcome)
            row.update({
                "current_display_rank": None,
                "selection_state": decision,
                "selection_reason": reason,
                "selection_gate_status": selection_gate_status,
                "selected_before_outcome": self._selected_before_outcome(
                    assessment, configuration_hash, selection_gate),
            })
            live_internal_scoring.append(row)

        eligible_ids = {str(row.get("assessment_id")) for row in verified_assessments}
        invalid_count = sum(bool(row.get("scorable")) and str(row.get("assessment_id")) not in eligible_ids
                            for row in frozen_assessments)
        current_top_rows = []
        for rank, row in enumerate(current_top, 1):
            outcome = verified_outcomes.get(str(row.get("assessment_id")))
            entry = self._live_result_row(rank, row, outcome)
            entry["selection_state"] = "SELECTED_OPPORTUNITY"
            entry["selection_gate_status"] = selection_gate_status
            entry["selected_before_outcome"] = True
            entry["selection_reason"] = row.get("selection_reason")
            if outcome is None and latest_index is not None and entry["target_round_index"] > latest_index:
                entry["status"] = "WATCHING"
            current_top_rows.append(entry)
        return {
            "pipeline_state": "LIVE" if observer_active else "LIVE_OBSERVATION_PAUSED",
            "automatic_real_money_execution": "OFF",
            "frozen_model_active": bool(model_loaded and model_warmup_complete),
            "frozen_model_loaded": model_loaded,
            "frozen_model_status": frozen_model_status,
            "training_status": "OFF",
            "frozen_model_version": frozen_version,
            "frozen_configuration_hash": configuration_hash,
            "feature_version": config.get("feature_version") or (latest_assessment or {}).get("feature_version"),
            "score_version": config.get("score_version") or (latest_assessment or {}).get("score_version"),
            "observer_active": observer_active, "observer_state": observer_state,
            "observer_status": observer_status,
            "observer_reason": observer_reason, "observer_heartbeat": heartbeat,
            "observer_heartbeat_age_seconds": heartbeat_age,
            "observer_caught_up_to_latest_round": caught_up,
            "real_data_alignment": alignment_snapshot,
            "scoring_warmup": scoring_warmup,
            "round_storage_summary": (round_context.get("round_storage_summary")
                                      if round_context is not None else None),
            "observer_lag_rounds": (max(0, int(marker[0]) - int(processed_index))
                                     if marker and processed_index is not None else None),
            "last_processed_round_index": processed_index,
            "last_processed_round_id": runtime.get("last_processed_round_id"),
            "last_processed_at": runtime.get("last_processed_at"),
            "last_real_round": latest_round,
            "observed_rounds": self.repository.count_rounds_after(V1_SOURCE_END_INDEX),
            "scored": len(verified_assessments), "resolved": len(resolved),
            "pending": max(0, len(verified_assessments) - len(resolved)), "invalid": invalid_count,
            "selection_gate_status": selection_gate_status,
            "v3_live_selection_gate_status": selection_gate_status,
            "research_candidate_gate_status": ((prospective_experiment.get("research_gate") or {}).get("status")
                                                if isinstance(prospective_experiment, dict) else None),
            "research_candidate_gate_reason": ((prospective_experiment.get("research_gate") or {}).get("policy", {}).get("selection_rule")
                                                if isinstance(prospective_experiment, dict) else None),
            "selection_gate_reason": ("Frozen V3 configuration contains no valid live selection gate; scores remain diagnostic."
                                      if selection_gate is None else None),
            "selection_counts": selection_counts,
            "live_internal_scoring": live_internal_scoring,
            "selected_opportunities": current_top_rows,
            "available_selected_slots": max(0, 4 - len(current_top_rows)),
            "current_contiguous_resolved": len(resolved_run),
            "longest_contiguous_resolved": max((len(run) for run in runs), default=0),
            "last_assessment_round_index": (int(latest_persisted_assessment["round_index"])
                                             if latest_persisted_assessment else None),
            "last_assessment_target_round_index": (int(latest_persisted_assessment.get("target_round_index", 0))
                                                    if latest_persisted_assessment else None),
            "last_assessment_id": (latest_persisted_assessment.get("assessment_id")
                                   if latest_persisted_assessment else None),
            "last_assessment_created_at": ((latest_persisted_assessment.get("created_at")
                                             or latest_persisted_assessment.get("observed_at"))
                                            if latest_persisted_assessment else None),
            "last_assessment_scorable": bool(latest_persisted_assessment and latest_persisted_assessment.get("scorable")),
            "last_scored_assessment_round_index": (int(latest_assessment["round_index"])
                                                    if latest_assessment else None),
            "last_resolved_round_index": (int(last_resolved["target_round_index"]) if last_resolved else None),
            "last_resolved_round_id": (last_resolved.get("target_round_id") if last_resolved else None),
            "last_resolved_at": (last_resolved.get("resolved_at") if last_resolved else None),
            "current_ranking_basis": ("highest frozen scores among explicitly selected, pre-outcome assessments"
                                      if selection_gate is not None else "no live selection ranking; frozen gate is undefined"),
            "current_top4": current_top_rows,
            "rolling_windows": rolling,
            "real_prospective_experiment": prospective_experiment,
        }

    @staticmethod
    def _pattern_state(values: np.ndarray) -> tuple[str, dict[str, Any]]:
        states = np.select([values < 1.2, values < 1.5, values < 2.0,
                            values < 3.0, values < 5.0],
                           ["A", "B", "C", "D", "E"], default="F")
        return "".join(states[-3:]), {"alphabet": {"A": "<1.20x", "B": "1.20-1.49x",
                "C": "1.50-1.99x", "D": "2.00-2.99x", "E": "3.00-4.99x", "F": ">=5.00x"}}

    @staticmethod
    def _regime_snapshot(frame: pd.DataFrame) -> dict[str, Any]:
        values = frame.multiplier.to_numpy(float)
        tail = values[-min(1000, len(values)):]
        _, counts = np.unique(np.where(tail < 1.5, 0, np.where(tail < 4, 1, 2)), return_counts=True)
        proportions = counts / len(tail)
        entropy = float(-(proportions * np.log2(proportions)).sum())
        return {"classification": "DESCRIPTIVE_ONLY", "rolling_rounds": len(tail),
                "rate_ge_2_10x": float(np.mean(tail >= TARGET)), "median": float(np.median(tail)),
                "q90": float(np.quantile(tail, .9)), "state_entropy": entropy,
                "predictive_claim": False}

    def stability_state(self) -> dict[str, Any]:
        report = self.repository.load_application_state("selective_opportunity_v3") or {}
        blocks = report.get("performance_by_chronological_block", [])
        top1 = [block.get("coverage", {}).get("1%", {}) for block in blocks]
        lifts = [item.get("lift") for item in top1 if item.get("lift") is not None]
        if len(lifts) >= 4 and any(value <= 0 for value in lifts):
            return {"state": "STABILITY_LOW", "evidence": "V3 frozen historical top-1% lift changed direction across chronological OOS blocks",
                    "historical_block_lifts": lifts, "prospective_confirmation": False}
        if len(lifts) >= 4 and all(value > 0 for value in lifts):
            return {"state": "STABILITY_ACCEPTABLE", "evidence": "V3 frozen historical top-1% lift stayed positive in every required chronological OOS block",
                    "historical_block_lifts": lifts, "prospective_confirmation": False,
                    "candidate_use": "research-only; still requires prospective confirmation"}
        return {"state": "STABILITY_UNKNOWN", "evidence": "Insufficient frozen block evidence to establish ranking reliability",
                "historical_block_lifts": lifts, "prospective_confirmation": False}

    def reconcile(self, rounds: pd.DataFrame, target_round_index: int | None = None,
                  *, already_validated: bool = False) -> int:
        clean = rounds if already_validated else validated_rounds(rounds.copy())[0]
        if clean.empty:
            return 0
        by_index = {int(row.round_index): row for row in clean.itertuples(index=False)}
        first_source_index = min(by_index)
        last_source_index = max(by_index) - 1
        if target_round_index is not None:
            # Live collection resolves only the assessment for this arriving
            # target. Scanning the entire 600+ row verified suffix on every
            # marker was redundant and delayed the next pre-outcome score.
            last_source_index = min(last_source_index, int(target_round_index) - 1)
            first_source_index = max(first_source_index, last_source_index)
        pending_loader = getattr(self.repository, "list_unresolved_v4_assessments", None)
        if pending_loader is not None:
            # The observer runs once per new marker. Query the indexed pending
            # assessment range that can resolve from this verified suffix
            # instead of decoding every historical V3 assessment and outcome
            # on every round. This changes only retrieval cost; outcome proof
            # checks below remain identical.
            candidates = pending_loader(first_source_index, last_source_index)
            existing = set()
        else:
            existing = {item["assessment_id"] for item in self.repository.list_v4_outcomes()}
            candidates = self.repository.list_v4_assessments()
        added = 0
        for item in candidates:
            if item["assessment_id"] in existing:
                continue
            source_idx = int(item["round_index"])
            # A newer collector marker can supersede a marker while its
            # snapshot is loading. Reconcile every pending target that has
            # arrived by this marker so a missed intermediate index cannot
            # remain pending forever.
            if (target_round_index is not None
                    and int(item.get("target_round_index", source_idx + 1)) > int(target_round_index)):
                continue
            target = by_index.get(source_idx + 1)
            source = by_index.get(source_idx)
            if (target is None or source is None or str(source.round_id) != str(item["round_id"])
                    or not bool(getattr(source, "continuity_verified", False))
                    or not bool(getattr(target, "continuity_verified", False))
                    or bool(getattr(target, "gap_before", False))
                    or pd.isna(getattr(target, "stored_at_dt", pd.NaT))
                    or pd.to_datetime(target.stored_at_dt, utc=True) <= pd.to_datetime(item["observed_at"], format="mixed", utc=True, errors="coerce")):
                continue
            if str(target.round_id) == str(source.round_id):
                continue
            now = _utcnow()
            assessment_proof = item.get("round_order_proof") or {}
            assessment_created_at = item.get("created_at") or item.get("observed_at")
            legacy_commit_proven = False
            if (not assessment_proof and getattr(self.repository, "database_url", None)
                    and assessment_created_at):
                try:
                    committed = pd.to_datetime(assessment_created_at, format="mixed", utc=True, errors="raise")
                    target_stored = pd.to_datetime(target.stored_at, format="mixed", utc=True, errors="raise")
                    later = clean.loc[clean.round_index > source_idx]
                    later_stored = pd.to_datetime(later.stored_at, format="mixed", utc=True, errors="coerce")
                    legacy_commit_proven = bool(
                        target_stored > committed
                        and not later_stored.isna().any()
                        and not (later_stored <= committed).any()
                    )
                except (TypeError, ValueError):
                    legacy_commit_proven = False
            commit_proven = bool(
                assessment_proof.get("source_was_latest_at_assessment_commit") is True
                and assessment_proof.get("target_absent_at_assessment_commit") is True
                and assessment_proof.get("assessment_source_round_index") == source_idx
                and assessment_proof.get("target_outcome_round_index") == source_idx + 1
            ) or legacy_commit_proven
            self.repository.save_v4_outcome_once({
                "assessment_id": item["assessment_id"], "target_round_id": str(target.round_id),
                "target_round_index": source_idx + 1, "actual_multiplier": float(target.multiplier),
                "is_true_2_10x": bool(float(target.multiplier) >= TARGET),
                "status": "TRUE" if float(target.multiplier) >= TARGET else "FALSE",
                "assessment_created_at": item.get("created_at", item["observed_at"]),
                "target_observed_at": (str(target.observed_at) if not pd.isna(getattr(target, "observed_at", pd.NaT)) else None),
                "target_stored_at": str(target.stored_at),
                "round_order_proof": {
                    "assessment_source_round_index": source_idx,
                    "assessment_source_round_id": str(source.round_id),
                    "target_outcome_round_index": int(target.round_index),
                    "latest_round_at_assessment_commit_index": source_idx,
                    "latest_round_at_assessment_commit_id": str(source.round_id),
                    "consecutive_indices": int(target.round_index) == source_idx + 1,
                    "assessment_inserted_while_source_latest": commit_proven,
                    "target_absent_at_assessment_commit": commit_proven,
                    "proof_method": (assessment_proof.get("proof_method")
                                     or ("postgres_stored_at_and_round_order" if legacy_commit_proven else None)),
                },
                "target_hit_2_10x": bool(float(target.multiplier) >= TARGET), "resolved_at": now,
            })
            added += 1
        return added

    def current(self, rounds: pd.DataFrame | None = None) -> dict[str, Any]:
        assessments = self.repository.list_v4_assessments()
        outcomes = self.repository.list_v4_outcomes()
        frozen_config = self.repository.load_application_state(CONFIG_KEY) or {}
        observer_runtime = self.repository.load_application_state(OBSERVER_RUNTIME_KEY) or {}
        assessment_ids = {row["assessment_id"] for row in outcomes}
        assessed = [row for row in assessments if row["assessment_id"] in assessment_ids]
        scorable_ids = {row["assessment_id"] for row in assessments if row.get("scorable")}
        outcome_by_id = {row["assessment_id"]: row for row in outcomes}
        assessment_by_id = {row["assessment_id"]: row for row in assessments}
        scorable_outcomes = [
            row for row in outcomes if row["assessment_id"] in scorable_ids
            and self._order_proof_verified(
                assessment_by_id[row["assessment_id"]], row, frozen_config.get("configuration_hash"))
        ]
        verified_scorable_outcome_ids = {row["assessment_id"] for row in scorable_outcomes}
        ranked_outcomes = [(float(item["opportunity_score"]),
                            bool(outcome_by_id[item["assessment_id"]]["target_hit_2_10x"]), item,
                            outcome_by_id[item["assessment_id"]])
                           for item in assessments if item.get("scorable")
                           and item["assessment_id"] in verified_scorable_outcome_ids
                           and item.get("opportunity_score") is not None]
        prospective_performance = self._prospective_performance(ranked_outcomes)
        clean, overall_quality = validated_rounds(rounds.copy() if rounds is not None else pd.DataFrame())
        from app.ml.opportunity_v2 import V1_SOURCE_END_INDEX
        prospective_clean = clean.loc[clean.round_index > V1_SOURCE_END_INDEX].reset_index(drop=True)
        _, quality = validated_rounds(prospective_clean)
        successes = sum(bool(row["target_hit_2_10x"]) for row in outcomes)
        scorable_successes = sum(bool(row["target_hit_2_10x"]) for row in scorable_outcomes)
        latest_round_id = str(clean.iloc[-1].round_id) if len(clean) else None
        latest_assessment = assessments[-1] if assessments else None
        heartbeat = observer_runtime.get("heartbeat_at")
        heartbeat_age = None
        if heartbeat:
            try:
                stamp = datetime.fromisoformat(str(heartbeat).replace("Z", "+00:00"))
                if stamp.tzinfo is None:
                    stamp = stamp.replace(tzinfo=timezone.utc)
                heartbeat_age = max(0.0, (datetime.now(timezone.utc) - stamp).total_seconds())
            except ValueError:
                pass
        observer_active = bool(frozen_config.get("status") == "FROZEN"
                               and observer_runtime.get("state") == "RUNNING"
                               and heartbeat_age is not None
                               and heartbeat_age <= OBSERVER_HEARTBEAT_STALE_SECONDS)
        # Legacy runtimes have no durable heartbeat row; retain the prior
        # round-match signal until the next supervised observer startup.
        if not observer_runtime:
            observer_active = bool(frozen_config.get("status") == "FROZEN" and latest_assessment
                                   and latest_assessment.get("round_id") == latest_round_id)
        latest_marker = None
        try:
            latest_marker = self.repository.latest_round_marker()
        except Exception:
            pass
        observer_caught_up = bool(observer_active and latest_marker
            and (observer_runtime.get("last_processed_round_index"), observer_runtime.get("last_processed_round_id"))
            == (latest_marker[0], latest_marker[1])) if observer_runtime else observer_active
        observer_state = observer_runtime.get("state") or ("RUNNING" if observer_active else "NOT_STARTED")
        observer_reason = observer_runtime.get("reason")
        if frozen_config.get("status") != "FROZEN":
            observer_state, observer_reason = "PAUSED_REQUIRES_OPERATOR", "frozen V3 configuration is unavailable"
        elif observer_runtime and (
                heartbeat_age is None or heartbeat_age > OBSERVER_HEARTBEAT_STALE_SECONDS):
            observer_state, observer_reason = "PAUSED_REQUIRES_OPERATOR", "observer heartbeat is missing or stale"
        summary = self.repository.load_application_state("opportunity_v4_summary") or {}
        diagnostics = self.repository.load_application_state(DIAGNOSTICS_KEY) or {}
        v3_report = self.repository.load_application_state("selective_opportunity_v3") or {}
        continuity = self._continuity_report(prospective_clean)
        assessment_continuity = self._resolved_continuity(assessments, outcomes, self.repository)
        verified_segments = self._verified_segments(prospective_clean)
        current_segment_rounds = (verified_segments[-1][1] - verified_segments[-1][0]) if verified_segments else 0
        # Use the same collector-proof segment boundary for the displayed
        # suffix and assessment gate; timestamp anomalies are not boundaries.
        contiguous = current_segment_rounds
        if current_segment_rounds < 102:
            reason = (f"Waiting for 102 consecutive overlap-verified collector rounds; current verified suffix is {current_segment_rounds}. "
                      "Warm-up assessments are recorded without scores; no past outcomes are reconstructed.")
        elif len(scorable_outcomes) < 5000:
            reason = (f"Scorable prospective outcomes {len(scorable_outcomes)}/5000; this provides about 50 observations at 1% coverage "
                      "for the first meaningful rare-ranking audit. No historical outcomes are backfilled.")
        else:
            reason = "Prospective milestone reached; historical V3 block instability still keeps the separate stability gate LOW."
        return {"version": "opportunity-v4-stability-2026-10", "mode": "RESEARCH_ONLY",
                "opportunity_ranker": "V3_FROZEN", "stability_gate": "SEPARATE",
                "observer_active": observer_active,
                "observer_state": observer_state,
                "observer_reason": observer_reason,
                "observer_caught_up_to_latest_round": observer_caught_up,
                "observer_loaded": frozen_config.get("status") == "FROZEN",
                "frozen_model_version": frozen_config.get("version"),
                "frozen_configuration_hash": frozen_config.get("configuration_hash"),
                "feature_version": frozen_config.get("feature_version") or (latest_assessment or {}).get("feature_version"),
                "score_version": frozen_config.get("score_version") or (latest_assessment or {}).get("score_version"),
                "last_observer_heartbeat": heartbeat,
                "observer_heartbeat_age_seconds": heartbeat_age,
                "stability_state": self.stability_state(), "observed_rounds": int(len(prospective_clean)),
                "verified_contiguous_rounds": contiguous,
                "genuine_gaps": quality.get("genuine_gaps"),
                "collector_gaps": quality.get("collector_gaps"),
                "collector_gap_attribution": "UNKNOWN: old observations lack overlap-proof markers",
                "timestamp_anomalies": int(quality.get("timestamp_anomalies", 0)),
                "timestamp_order_reversals": int(quality.get("timestamp_order_reversals", 0)),
                "timestamp_gaps_over_120s": int(quality.get("timestamp_gaps_over_120s", 0)),
                "timestamps_missing_or_invalid": int(quality.get("timestamps_missing_or_invalid", 0)),
                "rounds_assessed": len(assessments), "resolved_assessments": len(assessed),
                "current_verified_segment_rounds": current_segment_rounds,
                "scorable_assessments": len(scorable_ids),
                "scorable_resolved_assessments": len(scorable_outcomes),
                "unresolved_scorable_assessments": max(0, len(scorable_ids) - len(scorable_outcomes)),
                "outcomes_excluded_without_order_proof": sum(
                    row["assessment_id"] in scorable_ids for row in outcomes) - len(scorable_outcomes),
                "current_contiguous_resolved": assessment_continuity["current_contiguous_resolved"],
                "longest_contiguous_resolved": assessment_continuity["longest_contiguous_resolved"],
                "assessment_continuity_breaks": assessment_continuity["breaks"],
                "assessment_continuity_break_reason_counts": assessment_continuity["break_reason_counts"],
                "warmup_assessments": len(assessments) - len(scorable_ids),
                "pending_assessments": len(assessments) - len(assessed),
                "collection_tracking": {
                    "scored": len(scorable_ids),
                    "resolved": len(scorable_outcomes),
                    "unresolved": max(0, len(scorable_ids) - len(scorable_outcomes)),
                    "invalid": int(quality.get("invalid", 0)),
                    "gaps": int(continuity.get("states", {}).get("GAP", 0)),
                    "identity_unknown": int(continuity.get("states", {}).get("IDENTITY_UNKNOWN", 0)),
                    "timestamp_anomalies": int(continuity.get("states", {}).get("TIMESTAMP_ANOMALY", 0)),
                },
                "warmup": {"verified_rounds": contiguous, "required_rounds": 102,
                           "remaining_rounds": max(0, 102 - contiguous),
                           "status": "READY" if contiguous >= 102 else "WAITING"},
                "prospective_baseline": successes / len(outcomes) if outcomes else None,
                "scorable_prospective_baseline": scorable_successes / len(scorable_outcomes) if scorable_outcomes else None,
                "prospective_outcomes": len(outcomes), "research_candidates": 0,
                "confirmed_candidates": 0, "final_opportunities": 0,
                "bba_registry": {"pattern": "BBA", "state_alphabet": "V3 six-state multiplier alphabet",
                                 "historical_result": "26/36 (exploratory; immutable)",
                                 "prospective_occurrences": sum(bool(x.get("bba_research_occurrence")) for x in assessments),
                                 "resolved_occurrences": sum(bool(x.get("bba_research_occurrence")) for x in assessed),
                                 "successes": sum(bool(x.get("bba_research_occurrence")) and bool(outcome_by_id.get(x["assessment_id"], {}).get("target_hit_2_10x")) for x in assessments)
                                    if outcome_by_id else 0,
                                 "rate": (sum(bool(x.get("bba_research_occurrence")) and bool(outcome_by_id.get(x["assessment_id"], {}).get("target_hit_2_10x")) for x in assessments)
                                          / sum(bool(x.get("bba_research_occurrence")) and x["assessment_id"] in outcome_by_id for x in assessments)
                                          if sum(bool(x.get("bba_research_occurrence")) and x["assessment_id"] in outcome_by_id for x in assessments) else None),
                                 "ci95": wilson(
                                     sum(bool(x.get("bba_research_occurrence")) and bool(outcome_by_id.get(x["assessment_id"], {}).get("target_hit_2_10x")) for x in assessments),
                                     sum(bool(x.get("bba_research_occurrence")) and x["assessment_id"] in outcome_by_id for x in assessments)),
                                 "promoted": False},
                "milestones": [100, 200, 300, 400, 500, 1000],
                "milestone_reports": self._milestone_reports(ranked_outcomes),
                "prospective_ranking_quality": prospective_performance,
                "top4_prospective_experiment": prospective_performance.get("top4_experiment"),
                "HISTORICAL_DIAGNOSTIC": diagnostics,
                "FROZEN_PROSPECTIVE": prospective_performance,
                "last_assessment": assessments[-1] if assessments else None,
                "last_assessment_round_index": int(latest_assessment["round_index"]) if latest_assessment else None,
                "last_assessment_round_id": latest_assessment.get("round_id") if latest_assessment else None,
                "last_resolved_round_index": int(max(outcomes, key=lambda row: int(row.get("target_round_index", 0)))["target_round_index"]) if outcomes else None,
                "last_resolved_round_id": max(outcomes, key=lambda row: int(row.get("target_round_index", 0))).get("target_round_id") if outcomes else None,
                "historical_diagnostics": diagnostics,
                "v3_historical_context": {
                    "performance_by_chronological_block": v3_report.get("performance_by_chronological_block", []),
                    "pattern_sequence_5_successes": v3_report.get("pattern_sequence_5_analysis"),
                    "pattern_sequence_5_rate": v3_report.get("pattern_sequence_5_rate_analysis"),
                    "seq5_redundancy_correlation": v3_report.get("sequence_5_redundancy_correlation"),
                    "seq5_behavior_by_regime": v3_report.get("feature_behavior_by_regime"),
                    "feature_stability": v3_report.get("features"),
                    "regime_analysis": v3_report.get("regime_analysis"),
                    "source_status": v3_report.get("status", "UNAVAILABLE"),
                    "source_note": "Frozen V3 descriptive diagnostics; not used to update V3_FROZEN or choose V4 gates.",
                },
                "continuity_audit": continuity,
                "last_updated_at": summary.get("updated_at"),
                "research_shadow_ready": bool(len(scorable_outcomes) >= 5000 and self.continuity_is_trustworthy(prospective_clean)),
                "production_shadow_ready": False,
                "reason": reason,
                "quality": {**quality, "observed_consecutive_index_suffix": quality.get("observed_consecutive_index_suffix", 0),
                            "all_history_valid_rounds": int(len(clean)),
                            "all_history_genuine_gaps": overall_quality.get("genuine_gaps"),
                            "all_history_timestamp_anomalies": int(overall_quality.get("timestamp_anomalies", 0))}}

    @staticmethod
    def _prospective_performance(rows, *, include_top4: bool = True):
        if not rows:
            return {"rounds": 0, "baseline": None, "coverage": {},
                    "score_deciles_low_to_high": [], "monotonicity": None,
                    **({"top4_experiment": OpportunityStabilityV4._top4_experiment([])} if include_top4 else {})}
        rows = sorted(rows, key=lambda row: row[2].get("target_round_index", 0))
        scores = np.asarray([row[0] for row in rows], dtype=float)
        y = np.asarray([row[1] for row in rows], dtype=int)
        baseline = float(np.mean(y))
        coverage = {}
        for fraction in (.20, .10, .05, .02, .01, .005):
            n = max(1, int(math.ceil(len(y) * fraction)))
            chosen = np.argsort(scores)[-n:]
            hits = int(np.sum(y[chosen]))
            coverage[f"top_{fraction * 100:g}%"] = {"support": n, "successes": hits,
                "precision": hits / n, "lift": hits / n - baseline, "ci95": wilson(hits, n)}
        deciles = []
        for decile, indexes in enumerate(np.array_split(np.argsort(scores), 10), 1):
            if len(indexes):
                deciles.append({"decile": decile, "support": int(len(indexes)),
                    "precision": float(np.mean(y[indexes])), "mean_score": float(np.mean(scores[indexes]))})
        rates = [row["precision"] for row in deciles]
        rho = spearmanr(scores, y).statistic if len(np.unique(scores)) > 1 and len(np.unique(y)) > 1 else None
        return {"rounds": int(len(y)), "baseline": baseline, "coverage": coverage,
                "score_deciles_low_to_high": deciles,
                "monotonicity": {"adjacent_increases": int(sum(b > a for a, b in zip(rates, rates[1:]))),
                    "adjacent_pairs": max(0, len(rates) - 1),
                    "spearman_score_vs_outcome": float(rho) if rho is not None else None},
                **({"top4_experiment": OpportunityStabilityV4._top4_experiment(rows)} if include_top4 else {})}

    @staticmethod
    def _top4_experiment(rows, *, permutations: int = 2000):
        """Evaluate exactly four frozen pre-outcome ranks in disjoint windows."""
        sizes = (100, 200, 300, 400, 500)
        ordered = sorted(rows, key=lambda row: int(row[2].get("target_round_index", 0)))
        segments: list[list[tuple]] = []
        for row in ordered:
            target_index = int(row[2].get("target_round_index", 0))
            if not segments or target_index != int(segments[-1][-1][2].get("target_round_index", 0)) + 1:
                segments.append([row])
            else:
                segments[-1].append(row)

        reports = {}
        for size in sizes:
            windows = []
            available = sum(len(segment) for segment in segments)
            for segment in segments:
                full_count = len(segment) // size
                for block in range(full_count):
                    start = block * size
                    sample = segment[start:start + size]
                    by_rank = sorted(sample, key=lambda row: (-float(row[0]),
                                                               int(row[2].get("target_round_index", 0))))
                    chosen = by_rank[:4]
                    selected = [{
                        "rank": rank, "score": float(row[0]),
                        "target_round_index": int(row[2]["target_round_index"]),
                        "target_round_id": str(row[3].get("target_round_id") or ""),
                        "multiplier": float(row[3]["actual_multiplier"]),
                        "result": "TRUE" if bool(row[1]) else "FALSE",
                    } for rank, row in enumerate(chosen, 1)]
                    start_index = int(sample[0][2]["target_round_index"])
                    end_index = int(sample[-1][2]["target_round_index"])
                    windows.append({
                        "window": f"Window {size} #{len(windows) + 1}",
                        "rounds": size, "start_target_round_index": start_index,
                        "end_target_round_index": end_index,
                        "selected": selected,
                        "true": sum(item["result"] == "TRUE" for item in selected),
                        "false": sum(item["result"] == "FALSE" for item in selected),
                        "baseline_true": int(sum(bool(row[1]) for row in sample)),
                        "_labels": [bool(row[1]) for row in sample],
                    })

            window_count = len(windows)
            chosen_n = 4 * window_count
            top_hits = sum(window["true"] for window in windows)
            baseline_n = size * window_count
            baseline_hits = sum(window["baseline_true"] for window in windows)
            top_precision = top_hits / chosen_n if chosen_n else None
            baseline = baseline_hits / baseline_n if baseline_n else None
            top_ci = wilson(top_hits, chosen_n)
            baseline_ci = wilson(baseline_hits, baseline_n)
            absolute_lift = top_precision - baseline if top_precision is not None and baseline is not None else None
            absolute_lift_ci = ({"lower": top_ci["lower"] - baseline_ci["upper"],
                                 "upper": top_ci["upper"] - baseline_ci["lower"],
                                 "confidence": 0.95, "method": "conservative Wilson interval difference"}
                                if top_ci["lower"] is not None and baseline_ci["lower"] is not None else None)
            relative_lift = (top_precision / baseline - 1.0
                             if top_precision is not None and baseline is not None and baseline > 0 else None)

            rank_stats = {}
            for rank in range(1, 5):
                rank_hits = sum(window["selected"][rank - 1]["result"] == "TRUE" for window in windows)
                rank_stats[f"rank_{rank}"] = {"rank": rank, "support": window_count,
                                               "true": rank_hits, "false": window_count - rank_hits,
                                               "hit_rate": rank_hits / window_count if window_count else None,
                                               "ci95": wilson(rank_hits, window_count)}

            random_result = None
            if windows:
                random_totals = np.zeros(permutations, dtype=np.int64)
                for window in windows:
                    local_seed = int.from_bytes(hashlib.sha256(
                        f"{MODEL_VERSION}|top4|{size}|{window['start_target_round_index']}|{window['end_target_round_index']}".encode()
                    ).digest()[:8], "big")
                    rng = np.random.default_rng(local_seed)
                    labels = np.asarray(window["_labels"], dtype=bool)
                    random_totals += labels[np.argsort(rng.random((permutations, size)), axis=1)[:, :4]].sum(axis=1)
                random_precision = random_totals / chosen_n
                random_result = {
                    "permutations": permutations,
                    "mean_precision": float(np.mean(random_precision)),
                    "ci95": {"lower": float(np.quantile(random_precision, .025)),
                             "upper": float(np.quantile(random_precision, .975)),
                             "confidence": .95},
                    "observed_top4_precision": top_precision,
                    "empirical_p_value_top4_at_least_observed": float((1 + np.sum(random_totals >= top_hits)) / (permutations + 1)),
                    "selection": "uniform random 4 without replacement inside the same completed windows",
                }

            true_distribution = {f"{hits}/4 windows": sum(window["true"] == hits for window in windows)
                                 for hits in range(4, -1, -1)}
            for window in windows:
                window.pop("_labels", None)
            reports[str(size)] = {
                "window_size_rounds": size, "windows_completed": window_count,
                "rounds_available": available,
                "selected": chosen_n, "true": top_hits, "false": chosen_n - top_hits,
                "average_true_per_4": (top_hits / window_count if window_count else None),
                "precision": top_precision, "precision_ci95": top_ci,
                "baseline_true": baseline_hits, "baseline_rounds": baseline_n,
                "baseline": baseline, "absolute_lift": absolute_lift,
                "relative_lift": relative_lift, "absolute_lift_ci95": absolute_lift_ci,
                "window_hit_distribution": true_distribution,
                "rank_performance": rank_stats, "random_selection": random_result,
                "windows": windows,
                "window_policy": "disjoint chronological blocks within verified consecutive resolved assessment indices; incomplete tails omitted",
            }
        return {"threshold": ">=2.10x", "ranking": "V3_FROZEN_2026-10-03",
                "status": "READY" if any(row["windows_completed"] for row in reports.values()) else "WAITING_FOR_PROSPECTIVE_DATA",
                "permutation_count_per_window": permutations, "window_sizes": reports}

    @classmethod
    def _milestone_reports(cls, rows):
        ordered = sorted(rows, key=lambda row: row[2].get("target_round_index", 0))
        return [{"milestone_rounds": n, "status": "REACHED" if len(ordered) >= n else "WAITING",
                 "observed_scorable_outcomes": min(len(ordered), n),
                 "performance": cls._prospective_performance(ordered[:n], include_top4=False) if len(ordered) >= n else None}
                for n in (100, 200, 300, 400, 500, 1000)]

    @staticmethod
    def _continuity_report(frame: pd.DataFrame) -> dict[str, Any]:
        counts = {"round_index_gap_events": 0, "missing_round_indices": 0,
                  "timestamp_order_reversals": 0, "timestamp_gaps_over_120s": 0,
                  "timestamps_missing_or_invalid": 0, "platform_timestamp_order_reversals": 0,
                  "platform_timestamp_gaps_over_120s": 0, "platform_timestamps_missing_or_invalid": 0}
        gaps = []
        for i in range(1, len(frame)):
            left, right = frame.iloc[i - 1], frame.iloc[i]
            delta = int(right.round_index) - int(left.round_index)
            if delta != 1 or str(left.round_id) == str(right.round_id):
                counts["round_index_gap_events"] += 1
                counts["missing_round_indices"] += max(0, delta - 1)
                gaps.append({"previous_index": int(left.round_index), "next_index": int(right.round_index),
                             "missing_index_count": max(0, delta - 1),
                             "previous_round_id": str(left.round_id), "next_round_id": str(right.round_id)})
            left_time = getattr(left, "observed_at_dt", left.timestamp_dt)
            right_time = getattr(right, "observed_at_dt", right.timestamp_dt)
            if pd.isna(left_time) or pd.isna(right_time):
                counts["timestamps_missing_or_invalid"] += 1
            else:
                seconds = (right_time - left_time).total_seconds()
                if seconds < 0:
                    counts["timestamp_order_reversals"] += 1
                elif seconds > 120:
                    counts["timestamp_gaps_over_120s"] += 1
            left_platform = getattr(left, "platform_timestamp_dt", pd.NaT)
            right_platform = getattr(right, "platform_timestamp_dt", pd.NaT)
            if pd.isna(left_platform) or pd.isna(right_platform):
                counts["platform_timestamps_missing_or_invalid"] += 1
            else:
                platform_seconds = (right_platform - left_platform).total_seconds()
                if platform_seconds < 0:
                    counts["platform_timestamp_order_reversals"] += 1
                elif platform_seconds > 120:
                    counts["platform_timestamp_gaps_over_120s"] += 1
        return {"counts": counts, "latest_gaps": gaps[-10:],
                "collector_verified_rounds": int(frame["continuity_verified"].astype(bool).sum()) if "continuity_verified" in frame else 0,
                "unverified_rounds": int((~frame["continuity_verified"].astype(bool)).sum()) if "continuity_verified" in frame else int(len(frame)),
                "collector_gap_boundaries": int(frame["gap_before"].astype(bool).sum()) if "gap_before" in frame else 0,
                "platform_identity_verified": bool(len(frame) and frame["round_identity_type"].eq("PLATFORM").all()) if "round_identity_type" in frame else False,
                "platform_round_ids_available": int(frame["platform_round_id"].notna().sum()) if "platform_round_id" in frame else 0,
                "identity_unknown_rounds": int(frame["identity_confidence"].fillna("UNKNOWN").eq("UNKNOWN").sum()) if "identity_confidence" in frame else int(len(frame)),
                "round_index_source": "PLATFORM" if len(frame) and frame["round_index_source"].eq("PLATFORM").all() else "LOCAL_SEQUENCE_OR_UNKNOWN",
                "identity_fields": ["platform_round_id", "round_id", "round_identity_type", "identity_confidence"],
                "order_fields": ["platform_round_index", "local_round_index", "round_index_source"],
                "time_fields": ["platform_timestamp", "observed_at", "stored_at"],
                "verified_order_rule": "collector history-snapshot overlap plus consecutive locally observed indices; platform and observed clocks are diagnostic only",
                "states": {
                    "VALID": int(len(frame)),
                    "CONTIGUOUS_VERIFIED": int((OpportunityStabilityV4._verified_segments(frame)[-1][1] - OpportunityStabilityV4._verified_segments(frame)[-1][0])
                                                  if OpportunityStabilityV4._verified_segments(frame) else 0),
                    "GAP": int(counts["round_index_gap_events"] + (frame["gap_before"].astype(bool).sum() if "gap_before" in frame else 0)),
                    "IDENTITY_UNKNOWN": int(frame["identity_confidence"].fillna("UNKNOWN").eq("UNKNOWN").sum()) if "identity_confidence" in frame else int(len(frame)),
                    "TIMESTAMP_ANOMALY": int(counts["timestamp_order_reversals"] + counts["timestamp_gaps_over_120s"] + counts["timestamps_missing_or_invalid"]),
                }}

    @staticmethod
    def _verified_segments(frame: pd.DataFrame) -> list[tuple[int, int]]:
        if frame.empty or "continuity_verified" not in frame:
            return []
        indices = frame["round_index"].to_numpy(dtype=np.int64)
        ids = frame["round_id"].to_numpy(dtype=str)
        verified = frame["continuity_verified"].to_numpy(dtype=bool)
        gaps = frame["gap_before"].to_numpy(dtype=bool)
        segments = []
        start = None
        for i in range(len(frame)):
            if not verified[i]:
                if start is not None:
                    segments.append((start, i)); start = None
                continue
            boundary = bool(gaps[i])
            if start is not None:
                boundary = boundary or indices[i] != indices[i - 1] + 1
                boundary = boundary or ids[i] == ids[i - 1]
            if boundary and start is not None:
                segments.append((start, i)); start = i
            elif start is None:
                start = i
        if start is not None:
            segments.append((start, len(frame)))
        return segments

    @staticmethod
    def continuity_is_trustworthy(clean: pd.DataFrame) -> bool:
        if len(clean) < 102:
            return False
        tail = clean.iloc[-101:].reset_index(drop=True)
        if not tail["continuity_verified"].astype(bool).all():
            return False
        return all(int(tail.iloc[i].round_index) == int(tail.iloc[i - 1].round_index) + 1
                   and str(tail.iloc[i].round_id) != str(tail.iloc[i - 1].round_id)
                   and not bool(tail.iloc[i].gap_before) for i in range(1, len(tail)))
