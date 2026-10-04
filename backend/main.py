from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import hmac
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.api import analysis, analytics, backtesting, betting as betting_api, data, decisions, evidence, history, health, ml, models, opportunities, patterns, readiness as readiness_api, research, results, risk as risk_api, shadow, signals, statistics, live as live_api, operations as operations_api, recovery as recovery_api
from app.api import betting_mode as betting_mode_api, stability as stability_api
from app.betting.config import get_betting_settings
from app.betting.session import BettingManager, configure_default_manager
from app.betting.schemas import DecisionIntent
from app.core.config import get_settings
from app.core.logging import configure_logging, get_logger
from app.core.runtime import InstanceLock
from app.services.app_state import AppState
from app.services.monitoring_engine import MonitoringEngine
from app.services.system_health import SystemHealth
from app.services.system_orchestrator import SystemOrchestrator
from app.services.system_readiness import SystemReadiness
from app.ml.candidate_observer import CandidateObserver
from app.ml.opportunity import OpportunityResearchEngine
from app.services.betting_mode import BettingModeManager
from app.shadow import ShadowManager
from app.risk.engine import RiskManager
from app.history_collector import HistoryCollectorManager
from app.decision.engine import DecisionEngine
from app.reconciliation import ReconciliationService
from app.live import LiveActivationManager
from app.services.operations import OperationsManager
from app.services.disaster_recovery import DisasterRecoveryManager
from app.services.process_lifecycle import ProcessLifecycle

configure_logging()
log = get_logger("APP")


class ConnectionManager:
    def __init__(self):
        self.active: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active.add(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active.discard(websocket)

    async def broadcast(self, payload: dict):
        stale = []
        # Connections may join or leave while send_json yields to the loop.
        for ws in tuple(self.active):
            try:
                await ws.send_json(payload)
            except Exception:
                stale.append(ws)
        for ws in stale:
            self.disconnect(ws)


manager = ConnectionManager()


async def process_history_update(app: FastAPI):
    """Process one Part 3 history event and publish dependent live views."""
    await asyncio.to_thread(app.state.wp.reload)
    dataset = app.state.wp.dataset_service
    observer = getattr(app.state, "ml_observer", None)
    if observer is not None:
        await asyncio.to_thread(observer.reconcile, dataset.clean_rounds.copy())
        collector_latest = app.state.history_collector.status().get("latest") or {}
        observation = await asyncio.to_thread(observer.observe, dataset,
                                              collector_latest_round_id=collector_latest.get("round_id"))
        if observation is not None:
            await manager.broadcast({"type": "ml:experimental_observation", "observation": observation})
    opportunity = getattr(app.state, "opportunity_engine", None)
    if opportunity is not None:
        await asyncio.to_thread(opportunity.reconcile, dataset.clean_rounds.copy())
        collector_latest = app.state.history_collector.status().get("latest") or {}
        opportunity_observation = await asyncio.to_thread(
            opportunity.observe, dataset, collector_latest_round_id=collector_latest.get("round_id"))
        if opportunity_observation is not None:
            await manager.broadcast({"type": "opportunity:observation", "observation": opportunity_observation})
    # Frozen V3 prospective assessments are owned by the independent durable
    # marker monitor below. Do not also score through this general history
    # refresh path: it competes for workers and can finish after the target
    # outcome has already been written.
    analysis_payload = await asyncio.to_thread(app.state.wp.current_analysis)
    await manager.broadcast(
        {
            "type": "updated_analysis",
            "new_round": analysis_payload.get("recent_multipliers", [])[-1:],
            "analysis": analysis_payload,
            "system_status": {"valid_rounds": int(len(app.state.wp.rounds))},
        }
    )
    history = app.state.history_collector
    status = history.status()
    await manager.broadcast({"type": "history:new_round", "round": status["latest"]})
    await manager.broadcast({"type": "history:updated", "history": {"count": status["count"]}})
    analytics_report_engine = getattr(app.state.wp, "analytics_report_engine", None)
    if analytics_report_engine is not None:
        progress = await asyncio.to_thread(analytics_report_engine.report_progress_snapshot)
        await manager.broadcast({"type": "analytics:progress", "report_progress": progress})
    await manager.broadcast({"type": "history:stats", "stats": history.stats()})
    if status.get("latest") and hasattr(app.state, "shadow"):
        await app.state.shadow.reconcile_round(status["latest"])
    if status.get("latest") and hasattr(app.state, "reconciliation"):
        await app.state.reconciliation.reconcile_round(status["latest"])
    await manager.broadcast({"type": "data:updated", "quality": dataset.quality, "dataset": dataset.status()})
    readiness = getattr(app.state, "readiness", None)
    if readiness is not None:
        await readiness.refresh()
    await manager.broadcast({"type": "features:updated", "features": dataset.latest_features()})
    pattern_report = await asyncio.to_thread(app.state.wp.pattern_report)
    await manager.broadcast({"type": "patterns:updated", "baseline": pattern_report["baseline"],
                             "patterns": pattern_report["patterns"][:20]})
    registry = app.state.wp.model_registry
    # The live contract exposes why inference is suppressed before any
    # Evidence/Decision/Risk work is considered. Legacy test doubles may only
    # implement predict_latest; production always provides prediction_payload.
    if hasattr(registry, "prediction_payload"):
        inference = await asyncio.to_thread(registry.prediction_payload, dataset)
        await manager.broadcast({"type": "prediction:status", **inference})
        prediction = inference["prediction"] if inference["usable"] else None
    else:
        prediction = await asyncio.to_thread(registry.predict_latest, dataset)
    if prediction:
        await manager.broadcast({"type": "prediction:new", "prediction": prediction})
        snapshot = await asyncio.to_thread(app.state.wp.build_evidence, prediction)
        if snapshot:
            await manager.broadcast({"type": "prediction:evidence_updated", "evidence": snapshot})
            mode_manager = getattr(app.state, "betting_mode", None)
            betting_status = (mode_manager.betting_status() if mode_manager
                              else app.state.betting.status())
            betting_status["emergency_stop"] = app.state.risk.emergency_latched
            if not dataset.clean_rounds.empty:
                betting_status["latest_history_round_id"] = str(dataset.clean_rounds.iloc[-1]["round_id"])
            decision = await app.state.decision_engine.evaluate(
                prediction, snapshot,
                model_status=app.state.wp.model_registry.status(dataset),
                betting_status=betting_status,
                risk_manager=app.state.risk,
            )
            await manager.broadcast({"type": "decision:updated", "decision": decision})
            if decision.get("status") == "READY_FOR_EXECUTION":
                # One shared prediction/evidence/decision/risk result is sent
                # to exactly one executor selected by backend-owned mode.
                current = (mode_manager.betting_status() if mode_manager
                           else app.state.betting.status())
                if mode_manager and mode_manager.mode == "SHADOW_REALISTIC":
                    shadow_blocks = app.state.risk.validate_execution(decision, current)
                    if shadow_blocks:
                        await manager.broadcast({"type": "execution:rejected",
                            "decision_id": decision.get("decision_id"),
                            "reasons": shadow_blocks})
                        return
                    # Shadow never places platform bets, but its two-panel
                    # exposure still must fit a fresh read-only real balance.
                    observation = await mode_manager._read_platform_balance()
                    if not observation.get("verified"):
                        await manager.broadcast({"type": "execution:rejected",
                            "decision_id": decision.get("decision_id"),
                            "reasons": ["real_balance_unverified_before_shadow_execution",
                                        observation.get("error") or "UNKNOWN"]})
                        return
                    try:
                        from app.risk.config import get_risk_profile
                        risk_profile = get_risk_profile(decision.get("profile", "PROFILE_A"))
                        real_balance = float(observation["balance"])
                        approved_exposure = float((decision.get("risk_evaluation") or {}).get(
                            "approved_bet", (decision.get("risk") or {}).get("approved_bet", 0)))
                        real_exposure_limit = real_balance * risk_profile.maximum_balance_percentage
                        configured_limit = float((app.state.shadow.session or {}).get("config", {}).get(
                            "maximum_combined_exposure", 0))
                        if (approved_exposure <= 0 or approved_exposure > real_balance
                                or approved_exposure > real_exposure_limit
                                or approved_exposure > configured_limit):
                            raise ValueError("combined_exposure_exceeds_fresh_real_balance_or_session_limit")
                    except Exception as exc:
                        await manager.broadcast({"type": "execution:rejected",
                            "decision_id": decision.get("decision_id"),
                            "reasons": [str(exc)]})
                        return
                    app.state.shadow.record_real_balance(observation)
                    shadow_result = await app.state.shadow.submit_decision(
                        decision, prediction,
                        emergency_stop=bool(app.state.risk.emergency_latched))
                    if not shadow_result.get("accepted"):
                        await manager.broadcast({"type": "execution:rejected",
                            "decision_id": decision.get("decision_id"),
                            "reasons": [shadow_result.get("reason", "shadow_rejected")]})
                    return
                if current.get("mode") == "REAL":
                    live_gate = app.state.live.can_execute_live_bet(decision, current)
                    if not live_gate.get("allowed"):
                        if mode_manager:
                            await mode_manager.force_safe("pre_bet_gate_blocked:" + ";".join(
                                live_gate.get("reasons", []) or ["live_safety_gate_blocked"]))
                        await manager.broadcast({"type": "execution:rejected", "decision_id": decision.get("decision_id"), "reasons": live_gate.get("reasons", [])})
                        return
                blocks = app.state.risk.validate_execution(decision, current)
                if not blocks and str(decision.get("source_round_id")) == str(
                        dataset.clean_rounds.iloc[-1]["round_id"]):
                    approved = decision.get("risk") or decision.get("risk_evaluation") or {}
                    try:
                        await app.state.betting.submit_decision(DecisionIntent(
                            decision_id=decision["decision_id"],
                            round_id=decision["target_round_id"], execute=True,
                            profile=decision["profile"],
                            cashout=approved["cashout_target"],
                            bet_amount=approved["approved_bet_amount"],
                            expires_at=decision["expires_at"], source="part9-queue",
                        ))
                    except Exception as exc:
                        log.warning("[EXECUTION] queue handoff refused decision=%s: %s",
                                    decision.get("decision_id"), exc)
                elif blocks:
                    if mode_manager and mode_manager.mode == "LIVE_REAL":
                        await mode_manager.force_safe("pre_bet_risk_blocked:" + ";".join(blocks))
                    await manager.broadcast({"type": "execution:rejected",
                                             "decision_id": decision.get("decision_id"),
                                             "reasons": blocks})


async def monitor_file(app: FastAPI):
    monitor = MonitoringEngine(app.state.wp.settings.data_path)
    repository = app.state.wp.repository
    last_round_marker = None
    while True:
        try:
            await asyncio.sleep(3)
            if repository.database_url:
                marker = await asyncio.to_thread(repository.latest_round_marker)
                changed = marker != last_round_marker
                last_round_marker = marker
            else:
                changed = monitor.has_changed()
            if changed:
                # File parsing, database sync and statistical analysis are
                # synchronous/CPU-bound. Keep them off FastAPI's event loop so
                # health checks and UI requests remain responsive.
                await process_history_update(app)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("history monitor cycle failed; retrying")
            await asyncio.sleep(2)


async def _frozen_observer_call(app: FastAPI, function, *args, **kwargs):
    """Run observer/PostgreSQL work outside request and general history pools."""
    executor = getattr(app.state, "frozen_observer_executor", None)
    if executor is None:
        return await asyncio.to_thread(function, *args, **kwargs)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, partial(function, *args, **kwargs))


def _collector_marker_readiness(status: dict, marker: tuple | None) -> tuple[bool, str | None]:
    if not marker:
        return False, "no PostgreSQL round marker is available"
    if status.get("authRequired") is True or status.get("loggedIn") is not True:
        return False, "collector authentication is not verified"
    if status.get("collectorRunning") is not True or status.get("state") != "COLLECTING":
        return False, "collector is not actively collecting"
    if status.get("health") != "HEALTHY" or status.get("browserConnected") is not True:
        return False, "collector browser is not healthy"
    if status.get("pageConnected") is not True or status.get("frameConnected") is not True:
        return False, "collector History page or Aviator frame is disconnected"
    if str(status.get("lastRoundId")) != str(marker[1]):
        return False, "collector latest round ID does not match PostgreSQL"
    collected_at = _utc_timestamp(status.get("lastSuccessfulCollection"))
    if collected_at is None:
        return False, "collector has no verified recent collection timestamp"
    age = (datetime.now(timezone.utc) - collected_at).total_seconds()
    if age < 0 or age > 120:
        return False, "collector latest verified round is stale"
    return True, None


async def process_frozen_opportunity_marker(app: FastAPI, marker: tuple | None = None,
                                            collector_status: dict | None = None) -> bool:
    """Assess the current next target from the authoritative PostgreSQL snapshot.

    Keep this path independent of dashboard/report refresh work. The repository
    rechecks that the source is still the latest round and the target is absent
    while holding the same advisory lock as collector inserts.
    """
    engine = getattr(app.state, "opportunity_v4_engine", None)
    repository = getattr(getattr(app.state, "wp", None), "repository", None)
    total_started = time.monotonic()
    if engine is None or repository is None or not repository.database_url:
        return False
    collector_status = collector_status if collector_status is not None else _collector_alignment_status()
    collector_ready, collector_reason = _collector_marker_readiness(collector_status, marker)
    if not collector_ready:
        app.state.frozen_observer_attempt_reason = collector_reason
        return False
    app.state.frozen_observer_attempt_reason = None
    load_suffix = getattr(repository, "load_verified_round_suffix", repository.load_rounds)
    rows = await _frozen_observer_call(app, load_suffix)
    if not rows:
        app.state.frozen_observer_attempt_reason = "FEATURES_UNAVAILABLE_NO_VERIFIED_SUFFIX"
        return False
    import pandas as pd
    from app.ml.opportunity import validated_rounds

    rounds = pd.DataFrame(rows)
    validation_started = time.monotonic()
    clean, _ = validated_rounds(rounds, include_time_diagnostics=False)
    validation_seconds = time.monotonic() - validation_started
    if clean.empty:
        app.state.frozen_observer_attempt_reason = "FEATURES_UNAVAILABLE_NO_VALID_ROUNDS"
        return False
    latest = clean.iloc[-1]
    if marker is not None and (int(latest.round_index), str(latest.round_id)) != (int(marker[0]), str(marker[1])):
        app.state.frozen_observer_attempt_reason = "SOURCE_SNAPSHOT_CHANGED_BEFORE_SCORING"
        return False
    # Startup performs a full durable reconciliation. During steady collection
    # only the assessment targeting this arriving result can become resolvable.
    reconcile_started = time.monotonic()
    await _frozen_observer_call(app, engine.reconcile, clean, int(marker[0]), already_validated=True)
    reconcile_seconds = time.monotonic() - reconcile_started
    observe_started = time.monotonic()
    observation = await _frozen_observer_call(
        app, engine.observe, clean, str(latest.round_id), already_validated=True)
    observe_seconds = time.monotonic() - observe_started
    if observation is not None:
        await manager.broadcast({"type": "opportunity:v4_assessment", "assessment": observation})
    # A marker is not complete just because its snapshot was read. Require a
    # durable assessment row (including a warm-up assessment) before advancing
    # the observer checkpoint. This prevents silent gaps when outcome-order
    # proof or persistence rejects a score.
    getter = getattr(repository, "get_v4_assessment", None)
    persisted = observation
    if persisted is None and getter is not None:
        persisted = await _frozen_observer_call(app, getter, str(latest.round_id))
    if persisted is None:
        newest = await _frozen_observer_call(app, repository.latest_round_marker)
        changed = bool(newest and marker and (int(newest[0]), str(newest[1])) !=
                       (int(marker[0]), str(marker[1])))
        app.state.frozen_observer_attempt_reason = (
            "TARGET_ARRIVED_BEFORE_ASSESSMENT" if changed else "OBSERVER_RETURNED_NO_DURABLE_ASSESSMENT")
        log.warning("[OPPORTUNITY_V4] no durable assessment source=%s latest=%s suffix=%s total_ms=%.1f reconcile_ms=%.1f observe_ms=%.1f reason=%s",
                    marker[0] if marker else None, newest[0] if newest else None, len(rows),
                    (time.monotonic() - total_started) * 1000, reconcile_seconds * 1000,
                    observe_seconds * 1000, app.state.frozen_observer_attempt_reason)
    else:
        log.info("[OPPORTUNITY_V4] assessment phases source=%s suffix=%s validation_ms=%.1f reconcile_ms=%.1f observe_persist_ms=%.1f total_ms=%.1f scorable=%s",
                 marker[0] if marker else None, len(clean), validation_seconds * 1000,
                 reconcile_seconds * 1000, observe_seconds * 1000,
                 (time.monotonic() - total_started) * 1000, persisted.get("scorable"))
    return persisted is not None


async def persist_frozen_observer_runtime(app: FastAPI, **updates) -> dict | None:
    repository = getattr(getattr(app.state, "wp", None), "repository", None)
    if repository is None or not repository.database_url:
        return None
    from app.ml.opportunity_v4 import OBSERVER_RUNTIME_KEY

    now = datetime.now(timezone.utc).isoformat()
    current = getattr(app.state, "frozen_observer_runtime", None)
    if current is None:
        try:
            current = await _frozen_observer_call(app, repository.load_application_state, OBSERVER_RUNTIME_KEY) or {}
        except Exception:
            current = {}
    payload = {**current, **updates, "heartbeat_at": now}
    await _frozen_observer_call(app, repository.save_application_state, OBSERVER_RUNTIME_KEY, payload, now)
    app.state.frozen_observer_runtime = payload
    return payload


def _collector_alignment_status() -> dict:
    status_path = Path(__file__).resolve().parents[1] / "data" / "bot" / "status.json"
    try:
        payload = json.loads(status_path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _utc_timestamp(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _json_timestamp(value):
    """Convert database datetime values to JSON-safe ISO timestamps."""
    if not isinstance(value, datetime):
        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


async def _alignment_gap_details(app: FastAPI, repository, previous_index: int, current_index: int,
                                 configuration_hash: str | None) -> dict:
    """Explain a break between successful live alignment records without backfilling scores."""
    if current_index - previous_index > 101:
        return {"failed_round_index": previous_index + 1, "reason": "OTHER",
                "missing_assessment": None, "detail": "alignment audit skipped more than 100 indexes"}
    rows = await _frozen_observer_call(
        app, repository.round_continuity_range, previous_index + 1, current_index - 1)
    for index in range(previous_index + 1, current_index):
        row = rows.get(index)
        if row is None:
            return {"failed_round_index": index, "reason": "MISSING_ROUND",
                    "missing_assessment": None, "detail": "no PostgreSQL row exists for this local index"}
        if row.get("gap_before"):
            return {"failed_round_index": index, "reason": "COLLECTOR_GAP",
                    "missing_assessment": None, "detail": "round is marked as following an unverified collector gap"}
        if not row.get("continuity_verified"):
            return {"failed_round_index": index, "reason": "IDENTITY_UNVERIFIED",
                    "missing_assessment": None, "detail": "round continuity identity is not verified"}
        assessment = await _frozen_observer_call(app, repository.get_v4_assessment, str(row["round_id"]))
        if assessment is None:
            return {"failed_round_index": index, "reason": "MISSING_ASSESSMENT",
                    "missing_assessment": True, "detail": "no frozen V3 assessment was persisted before this target arrived"}
        if not app.state.opportunity_v4_engine._assessment_order_proof_verified(
                assessment, configuration_hash):
            return {"failed_round_index": index, "reason": "IDENTITY_UNVERIFIED",
                    "missing_assessment": False, "detail": "assessment exists but its pre-outcome order proof is invalid"}
    return {"failed_round_index": previous_index + 1, "reason": "OBSERVER_OFFLINE",
            "missing_assessment": None, "detail": "the live alignment audit did not record the intervening round"}


async def _record_alignment_audit_round(app: FastAPI, marker, assessment: dict | None) -> dict:
    """Append one match only when collector, PostgreSQL and pre-outcome V3 agree."""
    from app.ml.opportunity_v4 import (
        CONFIG_KEY, REAL_DATA_ALIGNMENT_AUDIT_VERSION, real_data_alignment_snapshot,
    )

    repository = app.state.wp.repository
    runtime = getattr(app.state, "frozen_observer_runtime", None) or {}
    started_at = _utc_timestamp(runtime.get("alignment_audit_started_at"))
    details = await _frozen_observer_call(app, repository.latest_round_live_details)
    collector = _collector_alignment_status()
    config = await _frozen_observer_call(app, repository.load_application_state, CONFIG_KEY) or {}
    now = datetime.now(timezone.utc)
    collector_fresh_at = _utc_timestamp(collector.get("lastSuccessfulCollection"))
    source_stored_at = _utc_timestamp((details or {}).get("stored_at"))
    assessment_created_at = _utc_timestamp((assessment or {}).get("created_at"))
    source_after_audit_start = bool(started_at and source_stored_at and source_stored_at >= started_at)
    collector_ok = bool(
        collector.get("lastRoundId") == str(marker[1])
        and collector.get("collectorRunning") is True
        and collector.get("state") == "COLLECTING"
        and collector.get("health") == "HEALTHY"
        and collector.get("browserConnected") is True
        and collector.get("pageConnected") is True
        and collector.get("frameConnected") is True
        and collector.get("loggedIn") is True
        and collector.get("authRequired") is not True
        and collector_fresh_at is not None
        and (now - collector_fresh_at).total_seconds() <= 120
    )
    database_ok = bool(details and int(details.get("round_index", -1)) == int(marker[0])
                       and str(details.get("round_id")) == str(marker[1])
                       and bool(details.get("continuity_verified"))
                       and not bool(details.get("gap_before")))
    # Alignment verifies identity and pre-outcome persistence, not score
    # availability. An immutable continuity-warmup assessment is valid proof
    # that the observer saw the real round before its target existed. Scoring
    # remains separately blocked until the frozen 102-round warm-up completes.
    proof_ok = bool(
        assessment and assessment.get("assessment_immutable") is True
        and assessment.get("round_id") == str(marker[1])
        and int(assessment.get("round_index", -1)) == int(marker[0])
        and assessment.get("model_configuration_hash") == config.get("configuration_hash")
        and assessment_created_at is not None and started_at is not None
        and assessment_created_at >= started_at
        and source_after_audit_start
        and app.state.opportunity_v4_engine._assessment_order_proof_verified(
            assessment, config.get("configuration_hash"))
    )

    records = list(runtime.get("alignment_audit_rounds") or [])
    tracker_active = runtime.get("alignment_tracker_version") == 1
    now_text = now.isoformat()
    legacy_snapshot = real_data_alignment_snapshot(runtime) if not tracker_active else None
    legacy_complete = bool(legacy_snapshot and legacy_snapshot.get("status") == "VERIFIED_10_ROUND")
    batch_progress = int(runtime.get("alignment_batch_progress", min(len(records), 10)))
    completed_batches = int(runtime.get("alignment_completed_batches") or (1 if legacy_complete else 0))
    passed_batches = int(runtime.get("alignment_passed_batches") or (1 if legacy_complete else 0))
    failed_batches = int(runtime.get("alignment_failed_batches") or 0)
    total_verified = int(runtime.get("alignment_total_rounds_verified", len(records)))
    consecutive_aligned = int(runtime.get("alignment_consecutive_rounds", len(records)))
    had_verified_batch = bool(runtime.get("alignment_had_verified_batch") or legacy_complete)
    last_completed_batch = runtime.get("alignment_last_completed_batch")
    if not tracker_active and legacy_complete and not last_completed_batch:
        legacy_batch = records[-10:]
        last_row = legacy_batch[-1]
        last_completed_batch = {
            "start_round_index": int(legacy_batch[0]["round_index"]),
            "end_round_index": int(last_row["round_index"]), "rounds": 10,
            "result": "PASS",
            "completed_at": last_row.get("stored_at") or last_row.get("assessment_created_at"),
            "reconstructed_from_retained_audit": True,
        }
    last_failure = runtime.get("alignment_last_failure")
    last_reset = runtime.get("alignment_last_reset")
    last_pass_at = runtime.get("alignment_last_pass_at")
    legacy_started_at = (records[0].get("stored_at") or records[0].get("assessment_created_at")
                         if not tracker_active and records else None)
    tracking_started_at = runtime.get("alignment_tracking_started_at") or legacy_started_at or now_text
    if not tracker_active and records and not last_failure:
        # Migrate the most recent legacy reset once. Older versions retained
        # only the current audit suffix, so inspect the immediately preceding
        # durable source round before initializing the richer batch counters.
        try:
            first_index = int(records[0]["round_index"])
            prior_rows = await _frozen_observer_call(
                app, repository.round_continuity_range, first_index - 1, first_index - 1)
            prior_row = prior_rows.get(first_index - 1)
            prior_stored = _utc_timestamp((prior_row or {}).get("stored_at"))
            if (prior_row and prior_stored and started_at and prior_stored >= started_at
                    and await _frozen_observer_call(
                        app, repository.get_v4_assessment, str(prior_row["round_id"])) is None):
                previous_progress = 0
                probe = first_index - 2
                while probe >= 1:
                    probe_rows = await _frozen_observer_call(app, repository.round_continuity_range, probe, probe)
                    probe_row = probe_rows.get(probe)
                    if (not probe_row or not probe_row.get("continuity_verified") or probe_row.get("gap_before")):
                        break
                    prior_assessment = await _frozen_observer_call(
                        app, repository.get_v4_assessment, str(probe_row["round_id"]))
                    if (not prior_assessment or not app.state.opportunity_v4_engine._assessment_order_proof_verified(
                            prior_assessment, config.get("configuration_hash"))):
                        break
                    previous_progress += 1
                    if previous_progress >= 10:
                        break
                    probe -= 1
                failure = {
                    "failed_round_index": first_index - 1,
                    "reason": ("COLLECTOR_GAP" if prior_row.get("gap_before") else
                               "IDENTITY_UNVERIFIED" if not prior_row.get("continuity_verified") else
                               "MISSING_ASSESSMENT"),
                    "collector_index": int(marker[0]),
                    "observer_index": int(runtime.get("last_processed_round_index") or records[-1].get("round_index", -1)),
                    "observer_lag": max(0, int(marker[0]) - int(runtime.get("last_processed_round_index") or records[-1].get("round_index", -1))),
                    "missing_assessment": True,
                    "detail": "reconstructed from durable rows; collector/observer indexes are current, not failure-time",
                    "indices_are_current_snapshot": True,
                    "detected_at": now_text,
                }
                last_failure = failure
                last_reset = {"previous_progress": min(10, previous_progress),
                              "current_progress": 1, "round_index": first_index,
                              "failed_round_index": first_index - 1,
                              "reason": failure["reason"], "detected_at": now_text,
                              "reconstructed": True}
                failed_batches = max(1, failed_batches)
                completed_batches = max(1, completed_batches)
                    # A database row plus an assessment proves continuity and
                    # pre-outcome ordering, but cannot reconstruct the old
                    # collector-side audit records that were discarded.
        except (KeyError, TypeError, ValueError):
            pass
    reason = None
    failure = None
    if source_after_audit_start:
        if not collector_ok:
            reason = "collector latest round ID or health did not match PostgreSQL"
        elif not database_ok:
            reason = "latest PostgreSQL round identity or continuity proof did not qualify"
        elif not proof_ok:
            reason = "no matching frozen V3 assessment with verified pre-outcome order proof"
        else:
            record = {
                "round_index": int(marker[0]),
                "collector_round_id": str(collector.get("lastRoundId")),
                "postgres_round_id": str(details["round_id"]),
                "multiplier": float(details["multiplier"]),
                "stored_at": _json_timestamp(details.get("stored_at")),
                "assessment_id": assessment.get("assessment_id"),
                "assessment_source_round_id": assessment.get("round_id"),
                "assessment_source_round_index": int(assessment.get("round_index")),
                "assessment_created_at": _json_timestamp(assessment.get("created_at")),
                "configuration_hash": config.get("configuration_hash"),
                "pre_outcome_proof_verified": True,
                "assessment_immutable": True,
                "scorable": assessment.get("scorable") is True,
                "failed_gate": assessment.get("failed_gate"),
                "warmup_rounds": (assessment.get("feature_snapshot") or {}).get("verified_segment_rounds"),
            }
            is_duplicate = bool(records and int(records[-1].get("round_index", -1)) == int(marker[0]))
            is_consecutive = bool(records and int(records[-1].get("round_index", -1)) + 1 == int(marker[0]))
            reset_previous_progress = batch_progress
            if records and not is_duplicate and not is_consecutive:
                gap = await _alignment_gap_details(
                    app, repository, int(records[-1].get("round_index", -1)), int(marker[0]),
                    config.get("configuration_hash"))
                failure = {
                    **gap,
                    "collector_index": int(marker[0]),
                    "observer_index": int(runtime.get("last_processed_round_index") or records[-1].get("round_index", -1)),
                    "observer_lag": max(0, int(marker[0]) - int(runtime.get("last_processed_round_index") or records[-1].get("round_index", -1))),
                    "detected_at": now_text,
                }
                failed_batches += 1
                completed_batches += 1
                batch_progress = 0
                consecutive_aligned = 0
                last_failure = failure
                last_reset = {
                    "previous_progress": reset_previous_progress,
                    "current_progress": 1,
                    "round_index": int(marker[0]),
                    "failed_round_index": gap["failed_round_index"],
                    "reason": gap["reason"],
                    "detected_at": now_text,
                }
                had_verified_batch = had_verified_batch or passed_batches > 0
                reason = gap["reason"]
                records = []
            if is_duplicate:
                records[-1] = record
            else:
                if batch_progress >= 10:
                    batch_progress = 0
                batch_progress += 1
                total_verified += 1
                consecutive_aligned += 1
                if batch_progress == 10:
                    completed_batches += 1
                    passed_batches += 1
                    had_verified_batch = True
                    last_pass_at = now_text
                    last_completed_batch = {
                        "start_round_index": int(records[-9]["round_index"]) if len(records) >= 9 else int(marker[0]) - 9,
                        "end_round_index": int(marker[0]), "rounds": 10, "result": "PASS",
                        "completed_at": now_text,
                    }
                records.append(record)
    if source_after_audit_start and reason and failure is None:
        failure = {
            "failed_round_index": int(marker[0]),
            "reason": ("COLLECTOR_GAP" if not database_ok and details and details.get("gap_before") else
                       "IDENTITY_UNVERIFIED" if not database_ok else
                       "MISSING_ASSESSMENT" if assessment is None else "OTHER"),
            "collector_index": int(marker[0]),
            "observer_index": int(runtime.get("last_processed_round_index") or marker[0]),
            "observer_lag": max(0, int(marker[0]) - int(runtime.get("last_processed_round_index") or marker[0])),
            "missing_assessment": assessment is None,
            "detail": reason,
            "detected_at": now_text,
        }
        failed_batches += 1
        completed_batches += 1
        reset_previous_progress = batch_progress
        batch_progress = 0
        consecutive_aligned = 0
        last_failure = failure
        records = []
        last_reset = {"previous_progress": reset_previous_progress, "current_progress": 0,
                      "round_index": int(marker[0]), "failed_round_index": int(marker[0]),
                      "reason": failure["reason"], "detected_at": now_text}
    alignment_runtime = {
        "alignment_audit_version": REAL_DATA_ALIGNMENT_AUDIT_VERSION,
        "alignment_audit_rounds": records[-10:],
        "alignment_audit_reason": reason,
        "alignment_tracker_version": 1,
        "alignment_tracking_started_at": tracking_started_at,
        "alignment_batch_progress": batch_progress,
        "alignment_completed_batches": completed_batches,
        "alignment_passed_batches": passed_batches,
        "alignment_failed_batches": failed_batches,
        "alignment_total_rounds_verified": total_verified,
        "alignment_consecutive_rounds": consecutive_aligned,
        "alignment_had_verified_batch": had_verified_batch,
        "alignment_last_completed_batch": last_completed_batch,
        "alignment_last_failure": last_failure,
        "alignment_last_reset": last_reset,
        "alignment_last_pass_at": last_pass_at,
    }
    return {**runtime, **alignment_runtime,
            "real_data_alignment": real_data_alignment_snapshot({**runtime, **alignment_runtime})}


async def monitor_frozen_opportunities(app: FastAPI):
    """Drain the durable, chronological prospective-observer round queue."""
    repository = app.state.wp.repository
    last_heartbeat_write = 0.0
    log.info("[OPPORTUNITY_V4] durable prospective observer queue started")
    try:
        from app.ml.opportunity_v4 import REAL_DATA_ALIGNMENT_AUDIT_VERSION

        queue_state = await _frozen_observer_call(app, repository.initialize_frozen_observer_queue)
        policy_report = await _frozen_observer_call(
            app, app.state.opportunity_v4_engine.ensure_rare_opportunity_selection_policy)
        prior_runtime = await _frozen_observer_call(
            app, repository.load_application_state, "opportunity_v4_observer_runtime") or {}
        app.state.frozen_observer_runtime = prior_runtime
        if prior_runtime.get("alignment_audit_version") != REAL_DATA_ALIGNMENT_AUDIT_VERSION:
            await persist_frozen_observer_runtime(
                app, alignment_audit_version=REAL_DATA_ALIGNMENT_AUDIT_VERSION,
                alignment_audit_started_at=datetime.now(timezone.utc).isoformat(),
                alignment_audit_rounds=[], alignment_audit_reason="Waiting for 10 new consecutive collector-to-assessment matches.")
        await persist_frozen_observer_runtime(
            app, state="RUNNING", reason=None, process_pid=os.getpid(),
            started_at=datetime.now(timezone.utc).isoformat(), last_error=None,
            durable_queue=await _frozen_observer_call(app, repository.frozen_observer_queue_snapshot),
            queue_initialized_at=queue_state.get("initialized_at"),
            queue_cursor_round_index=queue_state.get("last_enqueued_round_index"),
            rare_selection_policy={key: policy_report.get(key) for key in (
                "status", "policy_id", "policy_hash", "development_cutoff_round",
                "development_sample_count", "reason", "activation_target_index")})
        log.info("[OPPORTUNITY_V4] Rare Opportunity Selection Policy status=%s hash=%s dev_n=%s",
                 policy_report.get("status"), policy_report.get("policy_hash"),
                 policy_report.get("development_sample_count"))
    except Exception:
        log.exception("could not persist frozen observer startup state")
    while True:
        try:
            # Poll the durable queue frequently enough to assess a newly
            # persisted source while its next target is still absent. One
            # atomic DB tick handles marker + enqueue + expiry + queue head.
            await asyncio.sleep(0.25)
            if not repository.database_url:
                await persist_frozen_observer_runtime(
                    app, state="PAUSED_REQUIRES_OPERATOR",
                    reason="authoritative PostgreSQL is unavailable")
                continue
            tick = await _frozen_observer_call(app, repository.frozen_observer_queue_tick)
            marker = tick.get("marker")
            if marker is None:
                if time.monotonic() - last_heartbeat_write >= 5:
                    await persist_frozen_observer_runtime(
                        app, state="RUNNING", reason="waiting for the first verified round marker",
                        last_error=None)
                    last_heartbeat_write = time.monotonic()
                continue
            stale_items = tick.get("stale_items") or []
            if stale_items:
                stale_indexes = sorted(int(row["round_index"]) for row in stale_items)
                log.warning("[OPPORTUNITY_V4] expired %s queued source(s) whose targets already arrived: %s..%s",
                            len(stale_indexes), stale_indexes[0], stale_indexes[-1])
            queue_item = tick.get("queue_item")
            if queue_item and int(queue_item["round_index"]) == int(marker[0]):
                process_started = time.monotonic()
                collector_status = _collector_alignment_status()
                # Resolve the just-arrived target from an earlier frozen
                # assessment, then score this source for its next target.
                await _frozen_observer_call(
                    app, app.state.opportunity_v4_engine.sync_prospective_experiment)
                processed = await process_frozen_opportunity_marker(app, marker, collector_status)
            else:
                processed = False
            if processed:
                assessment = await _frozen_observer_call(app, repository.get_v4_assessment, str(marker[1]))
                await _frozen_observer_call(
                    app, repository.finish_frozen_observer_queue_item,
                    int(marker[0]), status="PROCESSED",
                    assessment_id=(assessment or {}).get("assessment_id"))
                alignment = await _record_alignment_audit_round(app, marker, assessment)
                queue_status = await _frozen_observer_call(app, repository.frozen_observer_queue_snapshot)
                await persist_frozen_observer_runtime(
                    app, state="RUNNING", reason=None, last_error=None,
                    last_processed_round_index=int(marker[0]),
                    last_processed_round_id=str(marker[1]),
                    last_processed_at=datetime.now(timezone.utc).isoformat(),
                    last_assessment_id=(assessment or {}).get("assessment_id"),
                    last_assessment_round_index=(assessment or {}).get("round_index"),
                    last_assessment_round_id=(assessment or {}).get("round_id"),
                    alignment_audit_version=alignment.get("alignment_audit_version"),
                    alignment_audit_rounds=alignment.get("alignment_audit_rounds", []),
                    alignment_audit_reason=alignment.get("alignment_audit_reason"),
                    alignment_tracker_version=alignment.get("alignment_tracker_version"),
                    alignment_tracking_started_at=alignment.get("alignment_tracking_started_at"),
                    alignment_batch_progress=alignment.get("alignment_batch_progress"),
                    alignment_completed_batches=alignment.get("alignment_completed_batches"),
                    alignment_passed_batches=alignment.get("alignment_passed_batches"),
                    alignment_failed_batches=alignment.get("alignment_failed_batches"),
                    alignment_total_rounds_verified=alignment.get("alignment_total_rounds_verified"),
                    alignment_consecutive_rounds=alignment.get("alignment_consecutive_rounds"),
                    alignment_had_verified_batch=alignment.get("alignment_had_verified_batch"),
                    alignment_last_completed_batch=alignment.get("alignment_last_completed_batch"),
                    alignment_last_failure=alignment.get("alignment_last_failure"),
                    alignment_last_reset=alignment.get("alignment_last_reset"),
                    alignment_last_pass_at=alignment.get("alignment_last_pass_at"),
                    real_data_alignment=alignment.get("real_data_alignment"),
                    durable_queue=queue_status)
                last_heartbeat_write = time.monotonic()
                duration_ms = max(0.0, (time.monotonic() - process_started) * 1000)
                log.info("[OPPORTUNITY_V4] prospective queue item processed source_round_index=%s duration_ms=%.1f assessment_id=%s scorable=%s",
                         marker[0], duration_ms, (assessment or {}).get("assessment_id"),
                         (assessment or {}).get("scorable"))
            elif time.monotonic() - last_heartbeat_write >= 5:
                queue_status = await _frozen_observer_call(app, repository.frozen_observer_queue_snapshot)
                await persist_frozen_observer_runtime(
                    app, state="RUNNING",
                    reason=(getattr(app.state, "frozen_observer_attempt_reason", None)
                            or ("waiting for the next queued real round" if not queue_item
                                else "waiting for a durable assessment of the queued source round")),
                    last_error=None, durable_queue=queue_status)
                last_heartbeat_write = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            try:
                await persist_frozen_observer_runtime(
                    app, state="DEGRADED", reason="observer marker processing failed",
                    last_error=f"{type(exc).__name__}: {str(exc)[:240]}",
                    last_error_at=datetime.now(timezone.utc).isoformat())
            except Exception:
                log.exception("could not persist frozen observer failure state")
            log.exception("frozen prospective observer cycle failed; retrying")
            await asyncio.sleep(2)


async def monitor_health(app: FastAPI):
    while True:
        try:
            await app.state.system_health.enforce_safety()
            readiness = getattr(app.state, "readiness", None)
            if readiness is not None:
                await readiness.refresh()
            modes = getattr(app.state, "betting_mode", None)
            if modes is not None:
                await modes.monitor_safety()
            lifecycle = getattr(app.state, "lifecycle", None)
            if lifecycle is not None:
                await manager.broadcast({"type": "backend:stability",
                                         "stability": await backend_stability_snapshot(app)})
            await asyncio.sleep(2)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("system health monitor cycle failed; retrying")
            await asyncio.sleep(2)


async def monitor_analytics_reports(app: FastAPI):
    """Catch up immutable completed-period reports and check once per minute."""
    while True:
        try:
            result = await asyncio.to_thread(app.state.wp.analytics_report_engine.generate_due_reports)
            if result.get("generated"):
                progress = await asyncio.to_thread(app.state.wp.analytics_report_engine.report_progress_snapshot)
                await manager.broadcast({"type": "analytics:reports_updated", **result,
                                         "report_progress": progress})
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("analytics report generation failed; scheduler will retry")
        await asyncio.sleep(60)


async def monitor_platform_balance(app: FastAPI):
    """Keep UI balance fresh using the collector's existing CDP browser."""
    while True:
        try:
            await app.state.betting_mode._read_platform_balance()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("read-only browser balance monitor failed; retrying")
        await asyncio.sleep(5)


async def monitor_operations(app: FastAPI):
    while True:
        try:
            await app.state.operations.evaluate()
        except Exception as exc:
            log.error("operations evaluation failed: %s", exc)
        await asyncio.sleep(15)


async def initialize_app_state(settings, attempts: int = 3) -> AppState:
    """Initialize disk/database state with bounded retries for transient locks."""
    for attempt in range(1, attempts + 1):
        try:
            return await asyncio.to_thread(AppState, settings)
        except sqlite3.OperationalError as exc:
            if attempt == attempts:
                raise
            delay = 0.5 * (2 ** (attempt - 1))
            log.warning("database startup attempt %s/%s failed: %s; retrying in %.1fs",
                        attempt, attempts, exc, delay)
            await asyncio.sleep(delay)
    raise RuntimeError("application state initialization exhausted retries")


async def backend_stability_snapshot(app: FastAPI) -> dict:
    """Build a cheap, non-blocking stability view for the dashboard."""
    lifecycle = getattr(app.state, "lifecycle", None)
    snapshot = lifecycle.status() if lifecycle is not None else {
        "status": "STARTING", "pid": os.getpid(), "ppid": os.getppid(),
    }
    history = getattr(app.state, "history_collector", None)
    snapshot["collector"] = history.health_status() if history else {"status": "STARTING"}
    readiness = getattr(app.state, "readiness", None)
    ml = readiness.cached_status() if readiness and readiness.cached_status() else {}
    snapshot["ml_worker"] = {
        "status": ml.get("automatic_training", {}).get("status", "IDLE") if isinstance(ml, dict) else "IDLE",
        "last_result": ml.get("automatic_training", {}).get("last_result") if isinstance(ml, dict) else None,
        "reason": ml.get("automatic_training", {}).get("reason") if isinstance(ml, dict) else None,
    }
    repository = getattr(getattr(app.state, "wp", None), "repository", None)
    if repository is None:
        snapshot["database"] = {"status": "STARTING"}
    else:
        try:
            with repository.connect() as conn:
                conn.execute("SELECT 1").fetchone()
            snapshot["database"] = {"status": "OK"}
        except Exception as exc:
            snapshot["database"] = {"status": "ERROR", "reason": str(exc)}
    system_health = getattr(app.state, "system_health", None)
    if system_health is None:
        snapshot["browser"] = {"status": "STARTING"}
    else:
        try:
            browser = system_health.snapshot()["components"]["browser"]
            snapshot["browser"] = {"status": browser["state"], "reason": browser["last_error"]}
        except Exception as exc:
            snapshot["browser"] = {"status": "ERROR", "reason": str(exc)}
    reliability_path = Path(__file__).resolve().parents[1] / "data" / "bot" / "browser-reliability.json"
    try:
        browser_reliability = await asyncio.to_thread(
            lambda: json.loads(reliability_path.read_text(encoding="utf-8"))
        )
        updated = datetime.fromisoformat(browser_reliability["updated_at"])
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - updated).total_seconds() > 15:
            browser_reliability["snapshot_stale"] = True
            browser_reliability["state"] = "DEGRADED"
            browser_reliability["reason"] = "Supervisor heartbeat is stale"
        snapshot["browser_reliability"] = browser_reliability
    except (OSError, ValueError, KeyError, TypeError):
        snapshot["browser_reliability"] = {"state": "UNKNOWN", "reason": "Supervisor snapshot unavailable"}
    return snapshot


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.instance_id = uuid.uuid4().hex
    app.state.started_at = datetime.now(timezone.utc).isoformat()
    instance_lock = InstanceLock(settings.instance_lock_path, port=settings.backend_port,
                                 instance_id=app.state.instance_id)
    app.state.instance_lock = instance_lock
    lifecycle = ProcessLifecycle(settings.data_path.parent / "backend_lifecycle.jsonl")
    app.state.lifecycle = lifecycle
    observer_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="frozen-v3-observer")
    app.state.frozen_observer_executor = observer_executor
    orchestrator = None
    started = time.monotonic()
    shutdown_reason = "graceful_lifespan_exit"
    app.state.ready = False
    try:
        if settings.enforce_single_instance:
            instance_lock.acquire()
        previous_events = lifecycle.events()
        prior_start = next((event for event in reversed(previous_events)
                            if event.get("event") == "backend_start"
                            and event.get("pid") != os.getpid()), None)
        lifecycle.start(
            startup_reason=os.environ.get("WINNER_STARTUP_REASON", "manual_or_process_manager"),
            previous_pid=(prior_start or {}).get("pid"),
            restart_count=int(os.environ.get("WINNER_RESTART_COUNT", "0") or 0),
        )
        log.info("backend startup begin pid=%s host=%s port=%s", os.getpid(),
                 settings.backend_host, settings.backend_port)
        app.state.manager = manager
        app.state.started_monotonic = started
        app.state.wp = await initialize_app_state(settings)
        lifecycle.bind_repository(app.state.wp.repository)
        app.state.wp.model_registry.retrain_interval = settings.ml_retrain_min_new_rounds
        app.state.ml_observer = CandidateObserver(app.state.wp.repository, app.state.wp.model_registry,
                                                   settings.ml_max_history_age)
        await asyncio.to_thread(app.state.ml_observer.reconcile, app.state.wp.dataset_service.clean_rounds.copy())
        app.state.opportunity_engine = OpportunityResearchEngine(
            app.state.wp.repository, settings.model_dir, max_feature_age_s=settings.ml_max_history_age)
        await asyncio.to_thread(app.state.opportunity_engine.reconcile,
                                app.state.wp.dataset_service.clean_rounds.copy())
        from app.ml.opportunity_v3 import OpportunityDiscoveryV3
        app.state.opportunity_v3_engine = OpportunityDiscoveryV3(app.state.wp.repository)
        from app.ml.opportunity_v4 import OpportunityStabilityV4
        app.state.opportunity_v4_engine = OpportunityStabilityV4(app.state.wp.repository)
        frozen = await asyncio.to_thread(app.state.opportunity_v4_engine.freeze_v3,
                                         app.state.wp.dataset_service.clean_rounds.copy())
        if frozen.get("status") != "FROZEN":
            log.error("[OPPORTUNITY_V4] frozen V3 model unavailable: %s", frozen.get("reason"))
        else:
            log.info("[OPPORTUNITY_V4] V3_FROZEN hash=%s; prospective assessment observer armed",
                     frozen.get("configuration_hash"))
            try:
                prior_experiment = (await asyncio.to_thread(
                    app.state.wp.repository.active_opportunity_experiment)
                    or await asyncio.to_thread(app.state.wp.repository.latest_opportunity_experiment))
                log.info("[OPPORTUNITY_EXPERIMENT] restored id=%s status=%s; a new timeline requires explicit operator start",
                         (prior_experiment or {}).get("experiment_id"),
                         (prior_experiment or {}).get("status", "NOT_STARTED"))
            except Exception:
                log.exception("[OPPORTUNITY_EXPERIMENT] durable experiment state could not be restored")
            try:
                diagnostic = await asyncio.to_thread(app.state.opportunity_v4_engine.historical_diagnostics,
                                                      app.state.wp.dataset_service.clean_rounds.copy())
                log.info("[OPPORTUNITY_V4] immutable diagnostic created status=%s eval_targets=%s",
                         diagnostic.get("status"), diagnostic.get("evaluation_targets"))
            except Exception:
                log.exception("[OPPORTUNITY_V4] historical diagnostics unavailable; frozen observer remains armed")
        log.info("[DATA] Loaded %s rounds", len(app.state.wp.rounds))

        # Betting automation module (Part 1 — read-only browser verification).
        betting_manager = BettingManager(
            get_betting_settings(), broadcaster=manager.broadcast,
            repository=app.state.wp.repository,
        )
        reconciled = app.state.wp.repository.reconcile_incomplete_executions()
        if reconciled:
            log.warning("[EXECUTION] reconciled %s incomplete executions; none will be retried", reconciled)
        app.state.betting = betting_manager
        configure_default_manager(betting_manager)
        app.state.risk = RiskManager(
            broadcaster=manager.broadcast, betting_manager=betting_manager,
        )
        app.state.reconciliation = ReconciliationService(
            app.state.wp.repository, broadcaster=manager.broadcast,
            betting_manager=betting_manager, risk_manager=app.state.risk,
        )
        betting_manager.reconciler = app.state.reconciliation
        app.state.decision_engine = DecisionEngine(
            settings.decision_path, app.state.wp.repository,
            broadcaster=manager.broadcast,
        )
        app.state.history_collector = HistoryCollectorManager(
            settings.data_path, manager.broadcast,
            round_loader=app.state.wp.repository.load_rounds if app.state.wp.repository.database_url else None,
            repository=app.state.wp.repository,
        )
        app.state.shadow = ShadowManager(repository=app.state.wp.repository, broadcaster=manager.broadcast)
        app.state.shadow.risk_manager = app.state.risk
        app.state.system_health = SystemHealth(
            broadcaster=manager.broadcast, betting_manager=betting_manager,
            history_collector=app.state.history_collector, repository=app.state.wp.repository,
        )
        app.state.live = LiveActivationManager(app)
        app.state.operations = OperationsManager(app)
        app.state.disaster_recovery = DisasterRecoveryManager(app, app.state.operations)
        app.state.readiness = SystemReadiness(
            app,
            broadcaster=manager.broadcast,
            required_rounds=settings.readiness_required_rounds,
            automatic_training=settings.readiness_auto_train,
            min_new_rounds=settings.ml_retrain_min_new_rounds,
            cooldown_s=settings.ml_retrain_cooldown_s,
        )
        app.state.betting_mode = BettingModeManager(app)
        betting_manager.execution_gate = app.state.betting_mode.final_execution_gate
        betting_manager.execution_failure_handler = app.state.betting_mode.handle_execution_failure
        for component in ("features", "patterns", "ml", "evidence", "decision", "risk", "api"):
            app.state.system_health.heartbeat(
                component, ok=True, metadata={"initialized": True}, stale_after_s=3600,
            )
        betting_manager.safety_gate = app.state.system_health.can_bet_now
        app.state.history_monitor = monitor_file
        app.state.opportunity_v4_monitor = monitor_frozen_opportunities
        app.state.analytics_report_monitor = monitor_analytics_reports
        app.state.system_health_monitor = monitor_health
        app.state.platform_balance_monitor = monitor_platform_balance
        app.state.operations_monitor = monitor_operations
        orchestrator = SystemOrchestrator(app)
        app.state.orchestrator = orchestrator
        try:
            await app.state.disaster_recovery.recover("startup")
        except Exception:
            log.exception("startup recovery failed; continuing in safe mode")
        known_rounds = {str(row["round_id"]): row for row in app.state.history_collector.rows()}
        for execution in app.state.wp.repository.open_executions():
            known = known_rounds.get(str(execution.get("target_round_id")))
            if known:
                try:
                    await app.state.reconciliation.reconcile_round(known)
                except Exception:
                    log.exception("startup reconciliation failed execution=%s",
                                  execution.get("execution_id"))
        await orchestrator.start()
        await app.state.readiness.refresh()
        app.state.ready = True
        log.info("backend startup complete pid=%s instance=%s", os.getpid(),
                 app.state.instance_id)
        yield
    except Exception as exc:
        shutdown_reason = f"startup_or_runtime_exception:{type(exc).__name__}"
        lifecycle.exception(exc, phase="startup_or_runtime")
        log.exception("backend startup/runtime failure")
        raise
    finally:
        app.state.ready = False
        lifecycle.shutdown(reason=shutdown_reason)
        log.info("backend graceful shutdown begin pid=%s", os.getpid())
        if orchestrator is not None:
            await orchestrator.shutdown()
        observer_executor.shutdown(wait=True, cancel_futures=True)
        instance_lock.release()
        log.info("backend graceful shutdown complete pid=%s", os.getpid())


app = FastAPI(title="Winner Predict", description="STATISTICAL PATTERN ANALYSIS platform for Aviator round history research.", version="1.0.0", lifespan=lifespan)
settings = get_settings()


@app.exception_handler(Exception)
async def unhandled_exception(request: Request, exc: Exception):
    """Return a stable error response without terminating the ASGI process."""
    request_id = getattr(request.state, "request_id", "unknown")
    log.exception("unhandled API failure request_id=%s method=%s path=%s",
                  request_id, request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": "internal_server_error",
                 "request_id": request_id},
    )


@app.middleware("http")
async def request_logging(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:16]
    request.state.request_id = request_id
    started = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception:
        elapsed = (time.perf_counter() - started) * 1000
        log.exception("API request crashed request_id=%s method=%s path=%s duration_ms=%.1f",
                      request_id, request.method, request.url.path, elapsed)
        response = JSONResponse(
            status_code=500,
            content={"ok": False, "error": "internal_server_error",
                     "request_id": request_id},
        )
    elapsed = (time.perf_counter() - started) * 1000
    response.headers["X-Request-ID"] = request_id
    if response.status_code >= 500:
        log.error("API failure request_id=%s method=%s path=%s status=%s duration_ms=%.1f",
                  request_id, request.method, request.url.path,
                  response.status_code, elapsed)
    elif request.url.path != "/health":
        log.info("API request request_id=%s method=%s path=%s status=%s duration_ms=%.1f",
                 request_id, request.method, request.url.path,
                 response.status_code, elapsed)
    return response


@app.middleware("http")
async def require_api_key(request: Request, call_next):
    """Require a bearer token when API_KEY is configured.

    Public read-only mode permits dashboard GET requests, but all API writes
    still require the backend key.
    """
    public_read = settings.public_read_only and request.method == "GET"
    if settings.api_key and request.url.path.startswith("/api/") and not public_read:
        supplied = request.headers.get("authorization", "")
        expected = f"Bearer {settings.api_key}"
        if not hmac.compare_digest(supplied, expected):
            return JSONResponse(
                status_code=401,
                content={"detail": "Missing or invalid API credentials"},
                headers={"WWW-Authenticate": "Bearer"},
            )
    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(statistics.router)
app.include_router(analysis.router)
app.include_router(signals.router)
app.include_router(history.router)
app.include_router(data.router)
app.include_router(patterns.router)
app.include_router(analytics.router)
app.include_router(models.router)
app.include_router(ml.router)
app.include_router(evidence.router)
app.include_router(decisions.router)
app.include_router(betting_api.router)
app.include_router(risk_api.router)
app.include_router(results.router)
app.include_router(health.router)
app.include_router(backtesting.router)
app.include_router(research.router)
app.include_router(shadow.router)
app.include_router(live_api.router)
app.include_router(operations_api.router)
app.include_router(recovery_api.router)
app.include_router(readiness_api.router)
app.include_router(betting_mode_api.router)
app.include_router(stability_api.router)
app.include_router(opportunities.router)


@app.get("/", tags=["system"])
async def root():
    """Human-friendly landing response for direct browser visits."""
    return {
        "ok": True,
        "service": "Winner Predict API",
        "status": "running",
        "docs": "/docs",
        "betting_status": "/api/betting/status",
        "risk_status": "/api/risk/status",
        "frontend": "http://localhost:5173",
    }


@app.get("/health", tags=["system"])
async def health(request: Request):
    """Liveness/readiness report used by the frontend and process supervisor."""
    database = {"status": "unavailable", "error": "repository not initialized"}
    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    if repository is not None:
        try:
            with repository.connect() as conn:
                conn.execute("SELECT 1").fetchone()
            if repository.database_url:
                database = {"status": "ok", "backend": "postgresql"}
            else:
                database = {"status": "ok", "backend": "sqlite", "path": str(repository.database_path)}
        except Exception as exc:
            database = {"status": "error", "error": str(exc)}
            log.exception("database health check failed")
    ready = bool(getattr(request.app.state, "ready", False))
    database_ok = database["status"] == "ok"
    payload = {
        "ok": ready and database_ok,
        "service": "winner-predict-backend",
        "auth_required": bool(settings.api_key),
        "public_read_only": settings.public_read_only,
        "status": "healthy" if ready and database_ok else "degraded",
        "backend": {
            "status": "ok" if ready else "starting",
            "pid": os.getpid(),
            "instance_id": getattr(request.app.state, "instance_id", None),
            "started_at": getattr(request.app.state, "started_at", None),
            "uptime_seconds": round(
                time.monotonic() - getattr(request.app.state, "started_monotonic", time.monotonic()),
                3,
            ),
        },
        "single_instance": (getattr(request.app.state, "instance_lock", None).status()
                            if getattr(request.app.state, "instance_lock", None) else None),
        "database": database,
        "stability": (getattr(request.app.state, "lifecycle", None).status()
                      if getattr(request.app.state, "lifecycle", None) else None),
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }
    log.info("health check backend=%s database=%s",
             payload["backend"]["status"], database["status"])
    return JSONResponse(status_code=200 if payload["ok"] else 503, content=payload)


@app.get("/api/system/status", tags=["system"])
async def system_status(request: Request):
    """Expose non-sensitive runtime configuration and dataset freshness."""
    wp = request.app.state.wp
    # Freshness metadata is already available from the loader; do not wait on
    # statistical analysis while the collector is updating a large dataset.
    quality = wp.quality.model_dump() if hasattr(wp.quality, "model_dump") else dict(wp.quality or {})
    cfg = wp.settings
    return {
        "ok": True,
        "configuration": {
            "target_multiplier": cfg.target_multiplier,
            "minimum_sample_size": cfg.min_sample_size,
            "signal_threshold": cfg.signal_threshold,
            "strong_signal_threshold": cfg.strong_signal_threshold,
            "api_security_enabled": bool(cfg.api_key),
        },
        "dataset": {
            "source": cfg.data_path.name,
            "valid_records": quality.get("valid_records", 0),
            "first_timestamp": quality.get("first_timestamp"),
            "last_timestamp": quality.get("last_timestamp"),
            "last_round_index": quality.get("last_round_index"),
        },
    }


@app.websocket("/ws/live")
async def websocket_live(websocket: WebSocket):
    if settings.api_key and not settings.public_read_only:
        supplied = websocket.query_params.get("token", "")
        if not hmac.compare_digest(supplied, settings.api_key):
            await websocket.close(code=1008, reason="Invalid API credentials")
            return
    await manager.connect(websocket)
    try:
        await websocket.send_json({"type": "system_status", "status": "connected", "label": "STATISTICAL PATTERN ANALYSIS"})
        readiness = getattr(websocket.app.state, "readiness", None)
        if readiness is not None:
            snapshot = readiness.cached_status()
            if snapshot is not None:
                await websocket.send_json({"type": "readiness:updated", "readiness": snapshot})
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)


if __name__ == "__main__":
    import uvicorn

    # Reload mode intentionally remains opt-in through the uvicorn CLI. An
    # implicit reloader creates an extra process and recurring brief outages.
    uvicorn.run(app, host=settings.backend_host, port=settings.backend_port,
                reload=False, log_level="info")
