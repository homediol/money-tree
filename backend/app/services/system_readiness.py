"""Central, side-effect-free system readiness model and REAL execution gate.

History continuity and model deployment are intentionally independent. A warm
history buffer never promotes an unvalidated model, and a deployable model
never bypasses a gap in the live collector stream.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable


log = logging.getLogger("APP.readiness")
Broadcaster = Callable[[dict], Awaitable[None]]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return default


class SystemReadiness:
    """Build one readiness snapshot for the API, UI, and REAL executor."""

    def __init__(self, app, *, broadcaster: Broadcaster | None = None,
                 required_rounds: int = 100, automatic_training: bool = True,
                 min_new_rounds: int = 250, cooldown_s: int = 3600):
        self.app = app
        self.broadcaster = broadcaster
        self.required_rounds = max(1, int(required_rounds))
        self.automatic_training = bool(automatic_training)
        self.min_new_rounds = max(1, int(min_new_rounds))
        self.cooldown_s = max(0, int(cooldown_s))
        self._last_snapshot: dict[str, Any] | None = None
        self._last_snapshot_monotonic: float | None = None
        self._last_signature: str | None = None
        self._last_history_fingerprint: tuple[int, int] | None = None
        self._training_task: asyncio.Task | None = None
        self._source_fingerprint: tuple[int, int] | None = None
        self._source_hash: str | None = None
        self._last_attempt_index: int | None = None
        self._last_attempt_hash: str | None = None
        self._last_attempt_at: datetime | None = None
        self._training = {
            "enabled": self.automatic_training,
            "status": "IDLE",
            "model_id": None,
            "reason": None,
            "last_result": None,
        }
        self._lock = asyncio.Lock()
        repository = getattr(getattr(app.state, "wp", None), "repository", None)
        self._last_snapshot = (repository.load_readiness_snapshot()
                               if repository and hasattr(repository, "load_readiness_snapshot") else None)
        self._restored_from_persistence = self._last_snapshot is not None

    def _training_progress(self) -> dict[str, Any]:
        dataset = self.state.wp.dataset_service
        registry = self.state.wp.model_registry
        metadata = registry.performance() or {}
        rounds = dataset.clean_rounds
        last_index = metadata.get("last_training_round_index")
        if self._last_attempt_index is not None and (last_index is None or self._last_attempt_index >= int(last_index)):
            last_index = self._last_attempt_index
        cursor_missing = last_index is None and bool(metadata.get("model_version"))
        never_trained = last_index is None and not cursor_missing
        if cursor_missing:
            new_rounds = None
        elif never_trained:
            new_rounds = len(rounds)
        elif not rounds.empty and "round_index" in rounds:
            new_rounds = int(rounds.loc[rounds["round_index"] > int(last_index), "round_index"].nunique())
        else:
            new_rounds = max(0, len(rounds) - int(last_index))
        raw_path = getattr(dataset, "raw_path", None)
        source_hash = None
        if raw_path and raw_path.exists():
            source_stat = raw_path.stat()
            fingerprint = (source_stat.st_mtime_ns, source_stat.st_size)
            if fingerprint != self._source_fingerprint:
                self._source_hash = hashlib.sha256(raw_path.read_bytes()).hexdigest()
                self._source_fingerprint = fingerprint
            source_hash = self._source_hash
        trained_source_hash = (self._last_attempt_hash
                               if self._last_attempt_index is not None and self._last_attempt_index == last_index
                               else metadata.get("source_sha256"))
        changed_same_size = bool(source_hash and trained_source_hash
                                 and source_hash != trained_source_hash and new_rounds == 0)
        try:
            trained_at = (self._last_attempt_at if self._last_attempt_index == last_index and self._last_attempt_at
                          else datetime.fromisoformat(str(metadata["trained_at"]).replace("Z", "+00:00")))
            cooldown_remaining = max(0, int(self.cooldown_s - (datetime.now(timezone.utc) - trained_at).total_seconds()))
        except (KeyError, TypeError, ValueError):
            cooldown_remaining = 0
        schema_changed = False
        frame = getattr(dataset, "dataset", None)
        if metadata.get("feature_schema_hash") and frame is not None and not frame.empty:
            from app.ml.trainer import feature_schema
            schema_changed = feature_schema(frame)[1] != metadata["feature_schema_hash"]
        # Only processed, previously unseen rounds count toward automatic
        # evaluation. A changed raw hash or feature schema is informative,
        # never an exception to the configured new-round requirement.
        new_rounds_ready = new_rounds is not None and new_rounds >= self.min_new_rounds
        return {"new_rounds_since_training": new_rounds,
                "minimum_new_rounds": self.min_new_rounds,
                "next_retrain_at": f"{new_rounds if new_rounds is not None else 'UNKNOWN'}/{self.min_new_rounds}",
                "cooldown_remaining_s": cooldown_remaining,
                "cooldown_status": "READY" if cooldown_remaining == 0 else "WAITING",
                "last_evaluated_round_index": last_index,
                "new_rounds_ready": new_rounds_ready,
                "training_cursor_missing": cursor_missing,
                "feature_schema_changed": schema_changed,
                "dataset_version": source_hash or metadata.get("source_sha256"),
                "dataset_changed_same_size": changed_same_size,
                "due": new_rounds_ready and cooldown_remaining == 0,
                "due_reason": "new_processed_rounds" if new_rounds_ready else None}

    @property
    def state(self):
        return self.app.state

    def _history_fingerprint(self) -> tuple[int, int] | None:
        """Return the raw history identity without reading or parsing it."""
        path = getattr(self.state.wp.dataset_service, "raw_path", None)
        if path is None:
            return None
        try:
            stat = path.stat()
        except OSError:
            return None
        return (int(stat.st_mtime_ns), int(stat.st_size))

    @staticmethod
    def _collector_health(status: dict[str, Any]) -> tuple[str, bool]:
        raw = str(status.get("status") or "UNKNOWN").upper()
        healthy = bool(status.get("running")) and raw in {"HEALTHY", "COLLECTING"}
        return ("HEALTHY" if healthy else "ERROR"), healthy

    def _model(self) -> dict[str, Any]:
        wp = self.state.wp
        registry = wp.model_registry
        dataset = wp.dataset_service
        health = registry.status(dataset)
        metadata = registry.performance() or {}
        algorithm = metadata.get("algorithm")
        model_metrics = (metadata.get("models") or {}).get(algorithm, {}) if algorithm else {}
        checks = metadata.get("overfitting_checks") or {}
        uncertainty = (checks.get("best_baseline_test_uncertainty")
                       or checks.get("test_brier_advantage_uncertainty") or {})
        folds_passed = _as_int(
            checks.get("folds_beating_baseline",
                       model_metrics.get("folds_beating_baseline")),
        )
        fold_count = _as_int(checks.get("fold_count"), len(model_metrics.get("walk_forward") or []))
        selection_advantage = model_metrics.get("selection_brier_advantage")
        test_metrics = metadata.get("test_metrics") or {}
        deployable = bool(health.get("deployable"))
        reason = None if deployable else (
            metadata.get("selection_reason")
            or health.get("validation_message")
            or metadata.get("message")
            or str(health.get("status") or "model is not deployable")
        )
        return {
            "model_id": health.get("model_version") or metadata.get("model_version"),
            "algorithm": algorithm,
            "runtime_status": health.get("status") or "UNKNOWN",
            "status": "DEPLOYABLE" if deployable else "NOT_DEPLOYABLE",
            "deployable": deployable,
            "baseline_comparison": {
                "baseline": checks.get("best_test_baseline", "base_rate_probability"),
                "selection_brier_advantage": selection_advantage,
                "test_brier_advantage": uncertainty.get("point"),
                "test_brier_advantage_ci95": uncertainty.get("ci95"),
            },
            "validation_folds": {"passed": folds_passed, "total": fold_count},
            "test_performance": {
                key: test_metrics.get(key)
                for key in ("accuracy", "brier_score", "log_loss", "roc_auc", "pr_auc")
            },
            "rejection_reason": reason,
        }

    @staticmethod
    def _decision(decision: dict[str, Any] | None) -> dict[str, Any]:
        if not decision:
            return {"status": "NONE", "valid": False, "decision_id": None,
                    "reason": "no current decision"}
        raw_status = str(decision.get("status") or "INVALID").upper()
        expired = raw_status == "EXPIRED"
        expiry = decision.get("expires_at")
        if expiry:
            try:
                expired = expired or datetime.fromisoformat(
                    str(expiry).replace("Z", "+00:00")
                ) <= datetime.now(timezone.utc)
            except (TypeError, ValueError):
                expired = True
        risk = decision.get("risk") or decision.get("risk_evaluation") or {}
        risk_approved = (
            decision.get("risk_status") == "APPROVED"
            and bool(risk.get("approved"))
        )
        executable = (
            raw_status == "READY_FOR_EXECUTION"
            and decision.get("execution_status") == "READY"
            and risk_approved
            and not expired
        )
        if expired:
            state, reason = "EXPIRED", "current decision is expired"
        elif executable:
            state, reason = "READY", None
        elif raw_status in {"CREATED", "VALIDATING", "CANDIDATE", "PENDING_RISK", "RISK_APPROVED"}:
            state, reason = "BLOCKED", "current decision is not ready for execution"
        else:
            state, reason = "BLOCKED", f"current decision status is {raw_status}"
        return {
            "status": state,
            "valid": executable,
            "decision_id": decision.get("decision_id"),
            "raw_status": raw_status,
            "risk_approved": risk_approved,
            "expires_at": expiry,
            "reason": reason,
        }

    def snapshot(self, *, decision: dict[str, Any] | None = None,
                 betting_status: dict[str, Any] | None = None,
                 require_real_mode: bool = True) -> dict[str, Any]:
        dataset = self.state.wp.dataset_service
        collector = self.state.history_collector
        collector_raw = (collector.health_status() if hasattr(collector, "health_status")
                         else collector.status())
        collector_status, collector_healthy = self._collector_health(collector_raw)
        persisted_collector = getattr(self.state.wp, "collector_state", None) or {}
        continuous = max(
            _as_int(dataset.quality.get("latest_contiguous_rounds")),
            _as_int(persisted_collector.get("contiguous_rounds")),
        )
        progress = min(100.0, round(continuous * 100.0 / self.required_rounds, 1))
        history_ready = continuous >= self.required_rounds
        model = self._model()

        betting = betting_status or self.state.betting.status()
        current = decision if decision is not None else self.state.decision_engine.current()
        decision_state = self._decision(current)
        risk_manager = getattr(self.state, "risk", None)
        emergency = bool(getattr(risk_manager, "emergency_latched", False))
        risk_raw = risk_manager.status(betting) if risk_manager else {}
        if emergency or risk_raw.get("risk_status") == "BLOCKED":
            risk_status = "BLOCKED"
        elif decision_state["valid"] or risk_raw.get("risk_status") == "APPROVED":
            risk_status = "READY"
        else:
            risk_status = "WAITING"
        risk_healthy = risk_manager is not None and not emergency

        shadow = getattr(self.state, "shadow", None)
        shadow_status = shadow.status() if shadow else {}
        raw_mode = str(betting.get("mode") or "OFF").upper()
        if raw_mode == "REAL":
            mode = "REAL"
        elif shadow_status.get("mode") == "SHADOW":
            mode = "SHADOW"
        else:
            mode = "OFF"

        reasons: list[str] = []
        if not collector_healthy:
            reasons.append("collector_not_healthy")
        if not history_ready:
            reasons.append(f"history_warming_up:{continuous}/{self.required_rounds}")
        if not model["deployable"]:
            reasons.append(f"model_not_deployable:{model['runtime_status']}")
        if emergency:
            reasons.append("emergency_stop")
        if not risk_healthy:
            reasons.append("risk_engine_unhealthy")
        if not decision_state["valid"]:
            reasons.append(f"decision_{decision_state['status'].lower()}")
        if require_real_mode and mode != "REAL":
            reasons.append("mode_not_real")
        if require_real_mode and not betting.get("automatic_enabled"):
            reasons.append("automatic_mode_off")

        health = getattr(self.state, "system_health", None)
        health_gate = health.can_bet_now(mode="REAL") if health else {
            "allowed": False, "reasons": ["system_health_unavailable"],
        }
        if not health_gate.get("allowed"):
            reasons.extend(f"health:{reason}" for reason in health_gate.get("reasons", []))

        training = self._training_progress()
        registry = self.state.wp.model_registry
        registry_lock = getattr(registry, "_train_lock", None)
        registry_busy = bool(getattr(registry, "_training", False))
        if registry_lock is not None:
            registry_busy = registry_busy or registry_lock.locked()
        task_busy = bool(self._training_task and not self._training_task.done())
        training_busy = registry_busy or task_busy or self._training["status"] in {"TRAINING", "EVALUATING"}
        evaluation_blockers = []
        if not self.automatic_training:
            evaluation_blockers.append("automatic_training_disabled")
        if not collector_healthy:
            evaluation_blockers.append("collector_not_healthy")
        if training["training_cursor_missing"]:
            evaluation_blockers.append("training_cursor_unknown")
        elif not training["new_rounds_ready"]:
            evaluation_blockers.append(
                f"new_processed_rounds:{training['new_rounds_since_training']}/{self.min_new_rounds}")
        if not history_ready:
            evaluation_blockers.append(f"contiguous_rounds:{continuous}/{self.required_rounds}")
        if training["cooldown_remaining_s"]:
            evaluation_blockers.append(f"cooldown_remaining:{training['cooldown_remaining_s']}s")
        if training_busy:
            evaluation_blockers.append("training_lock_busy")
        eligible = not evaluation_blockers
        if self._training["status"] in {"TRAINING", "EVALUATING"} or registry_busy:
            next_evaluation = "TRAINING"
        elif self._training["last_result"] is not None and training["new_rounds_since_training"] == 0:
            next_evaluation = "COMPLETED"
        else:
            next_evaluation = "ELIGIBLE" if eligible else "WAITING"

        reasons = list(dict.fromkeys(reasons))
        if not history_ready and collector_healthy:
            overall_status = "WARMING_UP"
        else:
            overall_status = "READY" if not reasons else "NOT_READY"
        return {
            "restored_from_persistence": bool(self._restored_from_persistence),
            "collector": {
                "status": collector_status,
                "healthy": collector_healthy,
                "raw_status": collector_raw.get("status"),
            },
            "history": {
                "total_rounds": max(
                    _as_int(collector_raw.get("count"), len(dataset.clean_rounds)),
                    _as_int(persisted_collector.get("total_history"), len(dataset.clean_rounds)),
                ),
                "continuous_rounds": continuous,
                "required_rounds": self.required_rounds,
                "progress_percentage": progress,
                "history_ready": history_ready,
                "status": "READY" if history_ready else "WARMING_UP",
            },
            "ml": model,
            "risk_engine": {
                "status": risk_status,
                "healthy": risk_healthy,
                "reason": risk_raw.get("reason") or ("risk engine unavailable" if not risk_manager else None),
            },
            "decision": decision_state,
            "betting": {
                "mode": mode,
                "raw_mode": raw_mode,
                "automatic_enabled": bool(betting.get("automatic_enabled")),
                "simulation_active": raw_mode == "SIMULATION",
            },
            "emergency_stop": emergency,
            "overall": {
                "status": overall_status,
                "ready": not reasons,
                "reasons": reasons,
            },
            "automatic_training": {
                **self._training, **training,
                "latest_candidate": self._training.get("model_id") or model["model_id"],
                "training_lock": "BUSY" if training_busy else "FREE",
                "next_evaluation": next_evaluation,
                "eligible": eligible,
                "blockers": evaluation_blockers,
                "prospective_rounds": training["new_rounds_since_training"],
                "prospective_evidence_status": "UNSEEN_BEFORE_NEXT_EVALUATION",
                "latest_candidate_evidence_status": "HISTORICAL_CHRONOLOGICAL_EVALUATION",
            },
            "updated_at": _utcnow(),
        }

    def execution_gate(self, decision: dict[str, Any] | None = None,
                       betting_status: dict[str, Any] | None = None,
                       *, require_real_mode: bool = True) -> dict[str, Any]:
        snapshot = self.snapshot(
            decision=decision,
            betting_status=betting_status,
            require_real_mode=require_real_mode,
        )
        return {
            "allowed": bool(snapshot["overall"]["ready"]),
            "state": snapshot["overall"]["status"],
            "reasons": snapshot["overall"]["reasons"],
            "checked_at": snapshot["updated_at"],
            "readiness": snapshot,
        }

    @staticmethod
    def _signature(snapshot: dict[str, Any]) -> str:
        comparable = {key: value for key, value in snapshot.items() if key != "updated_at"}
        return json.dumps(comparable, sort_keys=True, default=str)

    async def _emit(self, payload: dict[str, Any]) -> None:
        if self.broadcaster:
            await self.broadcaster(payload)

    async def _run_training(self) -> None:
        self._training.update(status="TRAINING", reason=None)
        await self.refresh(trigger_training=False, force_emit=True)
        try:
            progress = self._training_progress()
            dataset = self.state.wp.dataset_service
            rounds = dataset.clean_rounds
            self._last_attempt_index = (int(rounds.iloc[-1]["round_index"])
                                        if not rounds.empty and "round_index" in rounds else len(rounds))
            self._last_attempt_hash = progress["dataset_version"]
            self._last_attempt_at = datetime.now(timezone.utc)
            loop = asyncio.get_running_loop()
            def progress(phase: str) -> None:
                loop.call_soon_threadsafe(self._set_training_phase, phase)
            result = await asyncio.to_thread(
                self.state.wp.model_registry.train,
                self.state.wp.dataset_service,
                progress=progress,
            )
            wp = self.state.wp
            if hasattr(wp, "_cache_lock"):
                with wp._cache_lock:
                    wp._analysis_cache = None
            self._training.update(
                status="IDLE",
                model_id=result.model_version,
                last_result="DEPLOYABLE" if result.validated else "NOT_DEPLOYABLE",
                reason=None if result.validated else result.selection_reason or result.message,
            )
            await self._emit({
                "type": "model:training_completed",
                "model_id": result.model_version,
                "deployable": bool(result.validated),
                "reason": self._training["reason"],
            })
        except Exception as exc:
            log.exception("automatic readiness training failed")
            self._training.update(status="TRAINING_FAILED", last_result="FAILED", reason=str(exc))
            await self._emit({"type": "model:training_failed", "reason": str(exc)})
        finally:
            self._last_attempt_at = datetime.now(timezone.utc)
            await self.refresh(trigger_training=False, force_emit=True)

    def _set_training_phase(self, phase: str) -> None:
        if self._training["status"] in {"TRAINING", "EVALUATING"}:
            self._training["status"] = phase
            asyncio.create_task(self.refresh(trigger_training=False, force_emit=True))

    async def refresh(self, *, trigger_training: bool = True,
                      force_emit: bool = False) -> dict[str, Any]:
        schedule_training = False
        async with self._lock:
            snapshot = await asyncio.to_thread(self.snapshot)
            if trigger_training and snapshot["automatic_training"]["eligible"]:
                self._training.update(status="TRAINING", reason=None, last_result=None)
                schedule_training = True
                snapshot = await asyncio.to_thread(self.snapshot)
            signature = self._signature(snapshot)
            changed = signature != self._last_signature
            self._last_signature = signature
            self._last_snapshot = snapshot
            self._last_snapshot_monotonic = time.monotonic()
            self._last_history_fingerprint = self._history_fingerprint()
            repository = getattr(getattr(self.app.state, "wp", None), "repository", None)
            if repository and hasattr(repository, "save_readiness_snapshot"):
                repository.save_readiness_snapshot(snapshot, restored=self._restored_from_persistence)
            self._restored_from_persistence = False
            if schedule_training:
                self._training_task = asyncio.create_task(
                    self._run_training(), name="readiness-model-training",
                )
        if changed or force_emit:
            await self._emit({"type": "readiness:updated", "readiness": snapshot})
        return snapshot

    def status(self) -> dict[str, Any]:
        return self.snapshot()

    def cached_status(self) -> dict[str, Any] | None:
        """Return the latest monitor snapshot without disk, DB, or model work."""
        if (self._last_snapshot_monotonic is not None
                and time.monotonic() - self._last_snapshot_monotonic > 15):
            return None
        # A monitor snapshot is only valid for the history file it observed.
        # Returning the previous round count after the collector appended data
        # makes readiness look current while its continuity/model inputs are
        # stale. Let the normal monitor publish a fresh snapshot instead.
        if (self._last_snapshot is not None
                and self._history_fingerprint() != self._last_history_fingerprint):
            return None
        if self._last_snapshot is None:
            return None
        source = "PERSISTED_CACHE" if self._last_snapshot_monotonic is None else "CURRENT_CACHE"
        return {**self._last_snapshot, "snapshot_source": source}
