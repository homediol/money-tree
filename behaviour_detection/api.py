from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
from copy import deepcopy
from contextlib import asynccontextmanager, nullcontext
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path as FilePath

from fastapi import Body, Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import delete as sql_delete, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from . import __version__
from .analysis import (cached_recovery_reference, classify_multiplier, classify_state,
                       compare_historical_recovery, rolling_features, sequence_features)
from .db import make_engine, make_live_journal_engine, migrate
from .hybrid import (HIGH_DOMINANCE_2X_BATCH_SIZE,
                     HIGH_DOMINANCE_2X_POLICY_VERSION,
                     HIGH_DOMINANCE_STATE_TEST_CHECKPOINTS,
                     HIGH_DOMINANCE_STATE_TEST_POLICY_VERSION,
                     AUTHORITATIVE_ORDER_QUALITIES,
                     POLICY_VERSION, estimated_window_progress, rolling_analysis,
                     time_coverage)
from .models import (
    AnalysisSnapshot, ApiError, BehaviourApiRound, BehaviourStrategySettlement,
    BehaviourStateHistory, CanonicalRound,
    CollectorActivity, CumulativeStats, DiscoveredMotif, DominanceWindow, DistributionSnapshot,
    ForecastEvaluationResult, ForecastHistory, HighDominance2xBatch, HighDominanceSeriesState,
    HighDominanceStateTest, Next100Assessment, OrbitReading,
    HybridCohort, HybridCohortRound, MotifResearchEvidence, ResearchFinding, ResearchImport, RuleDefinition, RuleEvaluation,
    OrbitWatchState, SequenceOutlookResult, SourceRecord, SystemHealth, TimeGroup,
)
from .high_series import (POLICY_VERSION as HIGH_SERIES_POLICY_VERSION,
                          new_state as new_high_series_state,
                          validate_config as validate_high_series_config)
from .live_strategy_processor import LiveStrategyProcessor, hydrate_live_journal_baseline
from . import settings
from .winner_source import insert_verified_api_observation

log = logging.getLogger("behaviour.api")


async def _run_live_eligibility_publisher(engine, processor=None):
    """Retry durable eligibility intents independently of analysis or PostgreSQL."""
    from .worker import _publish_behaviour_eligibility
    while True:
        try:
            result = await asyncio.to_thread(_publish_behaviour_eligibility, engine)
            response = result.get("response") if isinstance(result, dict) else None
            if processor is not None and isinstance(result, dict):
                elapsed = result.get("behaviour_to_rules_response_ms")
                if isinstance(elapsed, (int, float)):
                    processor.record_stage_latency("behaviour_to_rules_response_ms", elapsed)
            if processor is not None and isinstance(response, dict):
                duration = response.get("rules_engine_acceptance_to_durable_lock_ms")
                if isinstance(duration, (int, float)):
                    processor.record_stage_latency("rules_engine_acceptance_to_durable_lock_ms", duration)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("live eligibility publisher iteration failed")
        await asyncio.sleep(0.5)


def _timestamp(value):
    return value.isoformat() if value is not None else None


def _read_orbit_live_status() -> dict:
    """Read collector health for display without treating it as a round event."""
    path = os.path.join(settings.PROJECT_ROOT, "data", "bot", "status.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            status = json.load(handle)
    except (OSError, ValueError, TypeError):
        return {"source_status": "UNKNOWN", "stream_status": "UNKNOWN",
                "round_status": "WAITING_FOR_VERIFIED_ROUND"}
    if not isinstance(status, dict):
        return {"source_status": "UNKNOWN", "stream_status": "UNKNOWN",
                "round_status": "WAITING_FOR_VERIFIED_ROUND"}
    orbit = status.get("orbitApi") if isinstance(status.get("orbitApi"), dict) else {}
    history = (status.get("roundHistoryHealth")
               if isinstance(status.get("roundHistoryHealth"), dict) else {})
    verified_at = history.get("lastVerifiedAt")
    verified_round_id = history.get("latestVerifiedRoundId")
    delivery_mode = history.get("latestVerifiedDeliveryMode")
    if not verified_at:
        round_status = "WAITING_FOR_VERIFIED_ROUND"
    elif not verified_round_id:
        round_status = "VERIFIED_ID_UNAVAILABLE"
    elif delivery_mode == "LIVE":
        round_status = "LIVE_VERIFIED_CANDIDATE"
    elif delivery_mode == "RECOVERED":
        round_status = "RECOVERY_CANDIDATE"
    else:
        round_status = "VERIFIED_DELIVERY_MODE_UNAVAILABLE"
    return {
        "source_status": str(orbit.get("status") or "UNKNOWN"),
        "stream_status": str(orbit.get("streamStatus") or "UNKNOWN"),
        "transport": str(orbit.get("transport") or "UNKNOWN"),
        "collector_state": str(status.get("state") or "UNKNOWN"),
        "last_stream_event_at": orbit.get("lastStreamEventAt"),
        "last_browser_observation_at": history.get("lastObservationAt"),
        "latest_browser_observed_multiplier": history.get("latestBrowserObservedMultiplier"),
        "latest_verified_round_uid": verified_round_id,
        "latest_verified_delivery_mode": delivery_mode,
        "latest_verified_at": verified_at,
        "latest_accepted_verified_multiplier": history.get("latestAcceptedVerifiedMultiplier"),
        "latest_persisted_round_uid": history.get("latestPersistedRoundId"),
        "latest_persisted_at": history.get("lastPersistedAt"),
        "coverage": str(history.get("coverage") or "UNKNOWN"),
        "integrity_reason": history.get("lastIntegrityReason") or orbit.get("lastValidationError"),
        "ingress_backlog": history.get("backlogCount"),
        "round_status": round_status,
    }


def _serialize_high_dominance_2x_batch(row: HighDominance2xBatch) -> dict:
    outcomes = row.outcomes or []
    observed = len(outcomes)
    return {
        "batch_id": row.batch_id,
        "series_anchor_position": row.series_anchor_position,
        "batch_number": row.batch_number,
        "decision_position": row.decision_position,
        "decision_round_id": row.decision_round_id,
        "trigger_horizon": row.trigger_horizon,
        "phase": row.phase,
        "start_position": row.start_position,
        "end_position": row.end_position,
        "state_at_decision": row.state_at_decision,
        "state_at_end": row.state_at_end,
        "status": row.lifecycle_status,
        "coverage_issue": row.coverage_issue,
        "round_progress": {"observed": observed,
                            "required": HIGH_DOMINANCE_2X_BATCH_SIZE,
                            "remaining": max(0, HIGH_DOMINANCE_2X_BATCH_SIZE - observed)},
        "wins": row.wins, "losses": row.losses,
        "net_units": int(row.wins or 0) - int(row.losses or 0),
        "win_rate": row.wins / observed if observed else None,
        "outcomes": outcomes,
        "opened_at": _timestamp(row.opened_at),
        "completed_at": _timestamp(row.completed_at),
    }


def _current_verified_window(session: Session, cohort: HybridCohort,
                             limit: int = 5) -> list[dict]:
    rows = session.execute(
        select(HybridCohortRound, CanonicalRound)
        .join(CanonicalRound, CanonicalRound.identity_key == HybridCohortRound.identity_key)
        .where(HybridCohortRound.cohort_id == cohort.cohort_id,
               CanonicalRound.import_snapshot_id.is_(None),
               CanonicalRound.order_quality.in_(AUTHORITATIVE_ORDER_QUALITIES),
               CanonicalRound.outcome.in_(("H", "L", "E")))
        .order_by(HybridCohortRound.cohort_position.desc()).limit(limit)
    ).all()
    return [{
        "identity_key": round_row.identity_key,
        "round_id": round_row.round_id,
        "cohort_position": cohort_round.cohort_position,
        "outcome": round_row.outcome,
        "multiplier": str(round_row.multiplier_value),
        "result_at_2x": "WIN" if round_row.multiplier_value >= Decimal("2.00") else "LOSS",
    } for cohort_round, round_row in reversed(rows)]


def _reconciled_high_dominance_summary(rows: list[HighDominance2xBatch], machine: dict) -> dict:
    """Union legacy audit and machine results by stable verified round identity."""
    observed: dict[str, dict] = {}
    batch_keys: set[tuple] = set()
    part2_keys: set[tuple] = set()
    fallback_totals: dict[tuple, tuple[int, int]] = {}

    def add_batch(anchor, phase, batch_number, fallback_id, outcomes):
        key = (("anchor", anchor, phase, int(batch_number)) if anchor is not None
               else ("batch", fallback_id))
        batch_keys.add(key)
        if phase == "CONTINUATION":
            part2_keys.add(key)
        if not outcomes:
            fallback_totals[key] = (0, 0)
        for outcome in outcomes or []:
            identity = outcome.get("identity_key") or outcome.get("round_id")
            if not identity:
                continue
            # The machine has committed outcomes; it supersedes an older projection
            # of the same canonical round without adding another observation.
            observed[str(identity)] = outcome

    for row in rows:
        add_batch(row.series_anchor_position, row.phase, row.batch_number,
                  row.batch_id, row.outcomes)
        if not row.outcomes:
            key = ("anchor", row.series_anchor_position, row.phase, int(row.batch_number))
            fallback_totals[key] = (int(row.wins or 0), int(row.losses or 0))

    machine_results_trusted = (machine.get("state") != "UNSAFE_STATE"
                               and machine.get("data_health") != "DATA_STALE"
                               and machine.get("provenance") != "UNVERIFIABLE")
    series_items = (list(machine.get("completed_series") or [])
                    if machine_results_trusted else [])
    if machine_results_trusted and machine.get("series"):
        series_items.append(machine["series"])
    for series in series_items:
        anchor = series.get("trigger_position")
        series_id = series.get("series_id")
        part1 = series.get("part1") or {}
        if series.get("trigger_identity") or part1 or series_id:
            add_batch(anchor, "INITIAL", 1, f"{series_id}:INITIAL",
                      part1.get("rounds"))
        part2 = series.get("part2") or {}
        for index, batch in enumerate(part2.get("batches") or [], start=1):
            add_batch(anchor, "CONTINUATION", index,
                      f"{series_id}:CONTINUATION:{index}", batch.get("rounds"))
        current = part2.get("current_batch")
        if current is not None and not current.get("completed_at"):
            batch_number = int(part2.get("batch_number") or len(part2.get("batches") or []) + 1)
            add_batch(anchor, "CONTINUATION", batch_number,
                      f"{series_id}:CONTINUATION:{batch_number}", current.get("rounds"))

    wins = losses = 0
    for outcome in observed.values():
        result = outcome.get("result") or outcome.get("result_at_2x")
        if result not in {"WIN", "LOSS"}:
            try:
                result = "WIN" if Decimal(str(outcome["multiplier"])) >= Decimal("2.00") else "LOSS"
            except (KeyError, InvalidOperation, TypeError, ValueError):
                continue
        wins += int(result == "WIN")
        losses += int(result == "LOSS")
    wins += sum(item[0] for item in fallback_totals.values())
    losses += sum(item[1] for item in fallback_totals.values())
    count = wins + losses
    return {"batch_count": len(batch_keys), "part2_window_count": len(part2_keys),
            "observed_rounds": count, "wins": wins, "losses": losses,
            "net_units": wins - losses, "win_rate": wins / count if count else None,
            "scope": "UNIQUE_VERIFIED_ROUND_IDENTITIES"}


def _high_dominance_state_test_payload(session: Session, cohort: HybridCohort) -> dict:
    rows = session.scalars(select(HighDominanceStateTest).where(
        HighDominanceStateTest.cohort_id == cohort.cohort_id
    ).order_by(HighDominanceStateTest.decision_position.desc()).limit(20)).all()
    totals = session.execute(select(
        func.count(HighDominanceStateTest.episode_id),
        func.coalesce(func.sum(HighDominanceStateTest.wins), 0),
        func.coalesce(func.sum(HighDominanceStateTest.losses), 0),
        func.coalesce(func.sum(HighDominanceStateTest.exact_count), 0),
    ).where(HighDominanceStateTest.cohort_id == cohort.cohort_id)).one()

    def serialize(row: HighDominanceStateTest) -> dict:
        outcomes = row.outcomes or []
        observed = len(outcomes)
        checkpoints = {}
        saved = row.checkpoint_results or {}
        for size in HIGH_DOMINANCE_STATE_TEST_CHECKPOINTS:
            result = saved.get(str(size))
            if result:
                checkpoints[str(size)] = {"status": "REACHED", **result}
            elif row.status == "COLLECTING":
                checkpoints[str(size)] = {"status": "COLLECTING",
                                          "remaining": max(0, size - observed)}
            else:
                checkpoints[str(size)] = {"status": "NOT_REACHED",
                                          "reason": row.status}
        return {
            "episode_id": row.episode_id,
            "decision_position": row.decision_position,
            "decision_identity_key": row.decision_identity_key,
            "decision_round_id": row.decision_round_id,
            "status": row.status,
            "exit_position": row.exit_position,
            "exit_state": row.exit_state,
            "round_progress": {"observed": observed, "maximum": max(HIGH_DOMINANCE_STATE_TEST_CHECKPOINTS)},
            "wins": row.wins, "losses": row.losses,
            "exact_count": row.exact_count,
            "net_units": int(row.wins or 0) - int(row.losses or 0),
            "win_rate": row.wins / observed if observed else None,
            "checkpoints": checkpoints,
            "outcomes": outcomes,
            "policy_version": row.policy_version,
            "opened_at": _timestamp(row.opened_at),
            "completed_at": _timestamp(row.completed_at),
        }

    episodes = [serialize(row) for row in rows]
    active = next((episode for episode in episodes if episode["status"] == "COLLECTING"), None)
    wins, losses, exact = int(totals[1] or 0), int(totals[2] or 0), int(totals[3] or 0)
    observations = wins + losses
    return {
        "policy_version": HIGH_DOMINANCE_STATE_TEST_POLICY_VERSION,
        "descriptive_only": True,
        "current_confirmed_state": cohort.confirmed_state,
        "status": active["status"] if active else (
            "WAITING_FOR_CONFIRMED_HIGH" if cohort.confirmed_state != "HIGH_DOMINANT"
            else "WAITING_FOR_NEXT_HIGH_EPISODE"),
        "checkpoints": list(HIGH_DOMINANCE_STATE_TEST_CHECKPOINTS),
        "active_episode": active,
        "summary": {"episode_count": int(totals[0] or 0),
                    "observed_rounds": observations, "wins": wins,
                    "losses": losses, "exact_count": exact,
                    "net_units": wins - losses,
                    "win_rate": wins / observations if observations else None},
        "episodes": episodes,
    }


def _get_engine(request: Request) -> Engine:
    engine = getattr(request.app.state, "engine", None)
    if engine is None:
        raise HTTPException(status_code=503, detail={"code": "BEHAVIOUR_DATABASE_UNCONFIGURED",
                                                     "message": "Set an independent BEHAVIOUR_DATABASE_URL."})
    if getattr(request.app.state, "database_error", None):
        raise HTTPException(status_code=503, detail={"code": "BEHAVIOUR_SCHEMA_NOT_READY",
                                                     "message": "The independent Behaviour schema is not ready."})
    return engine


def _get_live_engine(request: Request) -> Engine:
    engine = getattr(request.app.state, "live_engine", None)
    if engine is None or getattr(request.app.state, "live_error", None):
        raise HTTPException(status_code=503, detail={"code": "BEHAVIOUR_LIVE_JOURNAL_UNAVAILABLE",
            "message": "The durable local live-round journal is not ready."})
    return engine


def _session(request: Request):
    engine = _get_engine(request)
    with Session(engine) as session:
        yield session


def _live_session(request: Request):
    engine = _get_live_engine(request)
    with Session(engine, expire_on_commit=False) as session:
        yield session


def _latest_snapshot(session: Session):
    return session.scalar(select(AnalysisSnapshot).order_by(AnalysisSnapshot.id.desc()).limit(1))


def _latest_health(session: Session) -> dict:
    rows = session.scalars(select(SystemHealth).order_by(SystemHealth.component)).all()
    components = {row.component: {"state": row.state, "checked_at": _timestamp(row.checked_at),
                                  "details": row.details} for row in rows}
    if settings.WINNER_PREDICT_DATABASE_URL:
        # Orbit records are retained for audit, but the active stream is the
        # locally persisted Winner Predict round table.
        components.pop("orbit_collector", None)
        if "winner_predict_database" not in components:
            components["winner_predict_database"] = {
                "state": "NOT_REPORTING", "checked_at": None,
                "details": {"reason": "Winner Predict database sync has not reported health"},
            }
    elif "orbit_collector" not in components:
        # The API receives only the endpoint-presence signal. The secret API
        # key is intentionally injected into the collector process alone.
        configured = bool(settings.ORBIT_BASE_URL)
        components["orbit_collector"] = {
            "state": "NOT_REPORTING" if configured else "NOT_CONFIGURED",
            "checked_at": None,
            "details": ({"reason": "collector has not reported health"} if configured
                        else {"reason": "independent Behaviour Orbit credentials are not configured"}),
        }
    if not settings.WINNER_PREDICT_DATABASE_URL and "winner_predict_database" not in components:
        components["winner_predict_database"] = {
            "state": "NOT_CONFIGURED", "checked_at": None,
            "details": {"reason": "Winner Predict DATABASE_URL is not configured"},
        }
    return components


def _latest_round(session: Session):
    return session.scalar(select(CanonicalRound).order_by(CanonicalRound.analysis_position.desc()).limit(1))


def _hybrid_round(cohort_row: HybridCohortRound, row: CanonicalRound) -> dict:
    if row.timestamp_quality == "platform_event_time" and row.event_timestamp:
        role = "platform_event_time"
    elif row.source_observed_at:
        role = "source_observed_at"
    else:
        role = "ingestion_timestamp" if row.ingestion_timestamp else None
    return {"identity_key": row.identity_key, "round_id": row.round_id,
            "analysis_position": row.analysis_position,
            "cohort_position": cohort_row.cohort_position,
            "outcome": row.outcome, "source_name": row.source_name,
            "source_sequence": row.source_sequence, "order_quality": row.order_quality,
            "timestamp_quality": row.timestamp_quality,
            "event_timestamp": row.event_timestamp,
            "source_observed_at": row.source_observed_at,
            "ingestion_timestamp": row.ingestion_timestamp, "timestamp_role": role}


def _high_dominance_2x_payload(session: Session, cohort: HybridCohort,
                               machine_override: dict | None = None) -> dict:
    stored_machine_row = session.get(HighDominanceSeriesState, cohort.cohort_id)
    machine_row_exists = machine_override is not None or stored_machine_row is not None
    machine = deepcopy(machine_override) if machine_override is not None else (
        deepcopy(stored_machine_row.payload) if stored_machine_row else new_high_series_state())
    # The legacy batch table predates the persisted series reducer. It can
    # contain orphaned COLLECTING rows after the reducer has already stopped
    # or advanced to a different series. Once the reducer policy is present,
    # it is the sole authority for the live lifecycle; legacy rows remain
    # available through history and aggregate audit views only.
    machine_is_authoritative = (machine_row_exists
                                and machine.get("policy_version") == HIGH_SERIES_POLICY_VERSION
                                and machine.get("provenance") in {
                                    "LIVE_VERIFIED", "RECOVERY_REPLAY", "CANONICAL_REBASELINE"})
    source = session.get(SystemHealth, "winner_predict_database")
    if settings.WINNER_PREDICT_DATABASE_URL:
        checked = source.checked_at if source else None
        if checked is not None and checked.tzinfo is None:
            checked = checked.replace(tzinfo=timezone.utc)
        age = ((datetime.now(timezone.utc) - checked).total_seconds()
               if checked else float("inf"))
        # A recent database poll says only that the database answered. It does
        # not establish a fresh Orbit round or live strategy boundary.
        source_connected = bool(source and source.state in {"HEALTHY", "CONNECTED", "COLLECTING"}
                                and age <= 120)
        if (machine.get("state") != "UNSAFE_STATE"
                and machine.get("data_health") != "DATA_STALE"
                and (machine.get("provenance") not in {
                    "LIVE_VERIFIED", "RECOVERY_REPLAY", "CANONICAL_REBASELINE"}
                    or not source_connected)):
            machine = {**machine, "data_health": "WAITING_FOR_LIVE"}
    latest_verified = session.scalar(select(CanonicalRound).where(
        CanonicalRound.import_snapshot_id.is_(None),
        CanonicalRound.order_quality.in_(AUTHORITATIVE_ORDER_QUALITIES),
        CanonicalRound.outcome.in_(("H", "L", "E")),
    ).order_by(CanonicalRound.analysis_position.desc()).limit(1))
    cohort_last = session.get(CanonicalRound, cohort.last_identity_key) if cohort.last_identity_key else None
    analysis_health = session.get(SystemHealth, "analysis_worker")
    analysis_details = analysis_health.details if analysis_health and isinstance(analysis_health.details, dict) else {}
    try:
        analysis_position = int(analysis_details.get("last_position"))
    except (TypeError, ValueError, OverflowError):
        analysis_position = None
    latest_identity = latest_verified.identity_key if latest_verified else None
    machine_identity = machine.get("last_processed_identity")
    if machine.get("data_health") == "DATA_STALE":
        round_sync_status = "DATA_STALE"
    elif machine.get("state") == "WAITING_FOR_SHADOW_SETTLEMENT":
        round_sync_status = "WAITING_FOR_SHADOW_SETTLEMENT"
    elif latest_identity is None:
        round_sync_status = "WAITING_FOR_VERIFIED_ROUND"
    elif cohort.last_identity_key != latest_identity:
        round_sync_status = "ANALYSIS_PENDING"
    elif (machine_row_exists and machine.get("provenance") in {
              "LIVE_VERIFIED", "RECOVERY_REPLAY", "CANONICAL_REBASELINE"}
          and machine_identity != cohort.last_identity_key):
        round_sync_status = "SERIES_PENDING"
    else:
        round_sync_status = "SYNCED"
    round_sync = {
        "status": round_sync_status,
        "live_status": ("UNSAFE_STATE" if machine.get("state") == "UNSAFE_STATE" else
                        "LIVE_READY" if machine.get("provenance") == "LIVE_VERIFIED"
                        and machine.get("last_live_round_identity") == latest_identity else
                        "WAITING_FOR_LIVE"),
        "provenance": machine.get("provenance", "HISTORICAL_HYDRATION"),
        "last_live_round_id": machine.get("last_live_round_id"),
        "last_live_round_identity": machine.get("last_live_round_identity"),
        "last_live_round_processed_at": machine.get("last_live_round_processed_at"),
        "latest_verified_round_id": latest_verified.round_id if latest_verified else None,
        "latest_verified_identity": latest_identity,
        "latest_source_sequence": latest_verified.source_sequence if latest_verified else None,
        "latest_analysis_position": latest_verified.analysis_position if latest_verified else None,
        "latest_observed_at": _timestamp(latest_verified.source_observed_at or latest_verified.event_timestamp) if latest_verified else None,
        "last_cohort_round_id": cohort_last.round_id if cohort_last else None,
        "last_cohort_identity": cohort.last_identity_key,
        "last_cohort_position": cohort.last_cohort_position,
        "last_processed_identity": machine_identity,
        "last_processed_position": machine.get("last_processed_position"),
        "analysis_worker_position": analysis_position,
        "analysis_position_backlog": (max(0, latest_verified.analysis_position - analysis_position)
            if latest_verified and analysis_position is not None else None),
    }
    rows = session.scalars(select(HighDominance2xBatch).where(
        HighDominance2xBatch.cohort_id == cohort.cohort_id
    ).order_by(HighDominance2xBatch.series_anchor_position.desc(),
               HighDominance2xBatch.batch_number.desc()).limit(30)).all()
    active = None if machine_is_authoritative else session.scalar(select(HighDominance2xBatch).where(
        HighDominance2xBatch.cohort_id == cohort.cohort_id,
        HighDominance2xBatch.lifecycle_status == "COLLECTING",
    ).order_by(HighDominance2xBatch.series_anchor_position.desc(),
               HighDominance2xBatch.batch_number.desc()).limit(1))
    summary_rows = session.scalars(select(HighDominance2xBatch).where(
        HighDominance2xBatch.cohort_id == cohort.cohort_id
    )).all()
    summary = _reconciled_high_dominance_summary(summary_rows, machine)

    batches = [_serialize_high_dominance_2x_batch(row) for row in rows]
    active_payload = _serialize_high_dominance_2x_batch(active) if active else None
    current_horizon_states = cohort.rolling_horizon_states or {}
    current_window = _current_verified_window(session, cohort, 5)
    latest_anchor = session.scalar(select(func.max(
        HighDominance2xBatch.series_anchor_position
    )).where(HighDominance2xBatch.cohort_id == cohort.cohort_id))
    latest_batch = (session.scalar(select(HighDominance2xBatch).where(
        HighDominance2xBatch.cohort_id == cohort.cohort_id,
        HighDominance2xBatch.series_anchor_position == latest_anchor,
    ).order_by(HighDominance2xBatch.batch_number.desc()).limit(1))
        if latest_anchor is not None else None)
    initial_row = (session.scalar(select(HighDominance2xBatch).where(
        HighDominance2xBatch.cohort_id == cohort.cohort_id,
        HighDominance2xBatch.series_anchor_position == latest_anchor,
        HighDominance2xBatch.phase == "INITIAL",
    ).order_by(HighDominance2xBatch.batch_number.asc()).limit(1))
        if latest_anchor is not None else None)
    latest_continuation_row = (session.scalar(select(HighDominance2xBatch).where(
        HighDominance2xBatch.cohort_id == cohort.cohort_id,
        HighDominance2xBatch.series_anchor_position == latest_anchor,
        HighDominance2xBatch.phase == "CONTINUATION",
    ).order_by(HighDominance2xBatch.batch_number.desc()).limit(1))
        if latest_anchor is not None else None)
    continuation_batch_count = (session.scalar(select(func.count(
        HighDominance2xBatch.batch_id
    )).where(HighDominance2xBatch.cohort_id == cohort.cohort_id,
             HighDominance2xBatch.series_anchor_position == latest_anchor,
             HighDominance2xBatch.phase == "CONTINUATION")) or 0
        if latest_anchor is not None else 0)
    latest_series_rows = [latest_batch] if latest_batch else []
    initial_batch = _serialize_high_dominance_2x_batch(initial_row) if initial_row else None
    continuation_batches = ([_serialize_high_dominance_2x_batch(latest_continuation_row)]
                            if latest_continuation_row else [])
    machine_series = machine.get("series") or {}
    machine["active_config"] = machine_series.get("configuration_snapshot") or machine.get("config")
    machine["configuration_status"] = ("PENDING_NEXT_SERIES" if machine.get("pending_config") else
        "ACTIVE" if machine_series else "SAVED")
    trigger_horizon = (active.trigger_horizon if active else
                       (latest_batch.trigger_horizon if latest_batch else
                        machine_series.get("trigger_horizon") or
                        (machine.get("config") or {}).get("trigger_horizon")))
    trigger_state = (machine.get("last_dominance_state") if machine_is_authoritative else
                     current_horizon_states.get(trigger_horizon) if trigger_horizon else None)
    machine_part2 = (machine.get("series") or {}).get("part2") or {}
    machine_current_batch = machine_part2.get("current_batch") or {}
    continuation_rounds = (len(machine_current_batch.get("rounds") or [])
                           if machine_is_authoritative else
                           (len(latest_continuation_row.outcomes or [])
                            if latest_continuation_row else 0))
    continuation_waiting_for_resume = bool(
        not machine_is_authoritative and active is None and latest_continuation_row and (
            (latest_continuation_row.lifecycle_status == "STOPPED_LOW_DOMINANT"
             and continuation_rounds < HIGH_DOMINANCE_2X_BATCH_SIZE)
            or (latest_continuation_row.lifecycle_status == "COMPLETED"
                and continuation_rounds >= HIGH_DOMINANCE_2X_BATCH_SIZE
                and trigger_state == "LOW_DOMINANT")
        )
    )
    if machine_is_authoritative:
        status = machine.get("state") or "IDLE"
    elif active_payload:
        status = "INITIAL_COLLECTING" if active.phase == "INITIAL" else "CONTINUING_HIGH"
    elif rows and rows[0].lifecycle_status.startswith("INCOMPLETE_SOURCE"):
        status = "PAUSED_AFTER_SOURCE_GAP"
    elif continuation_waiting_for_resume:
        status = "WAITING_FOR_HIGH_RESUMPTION"
    elif latest_series_rows and latest_series_rows[0].lifecycle_status in {
            "STOPPED_LOW_DOMINANT", "STOPPED_STATE_EXIT"}:
        status = latest_series_rows[0].lifecycle_status
    elif trigger_state == "HIGH_DOMINANT":
        status = "WAITING_FOR_NEXT_VERIFIED_ROUND"
    else:
        status = "WAITING_FOR_HIGH_DOMINANT"
    phase_summaries = {}
    for phase in ("INITIAL", "CONTINUATION"):
        criteria = [HighDominance2xBatch.cohort_id == cohort.cohort_id,
                    HighDominance2xBatch.phase == phase]
        if latest_anchor is not None:
            criteria.append(HighDominance2xBatch.series_anchor_position != latest_anchor)
        phase_totals = session.execute(select(
            func.count(HighDominance2xBatch.batch_id),
            func.coalesce(func.sum(HighDominance2xBatch.wins), 0),
            func.coalesce(func.sum(HighDominance2xBatch.losses), 0),
        ).where(*criteria)).one()
        phase_wins, phase_losses = int(phase_totals[1] or 0), int(phase_totals[2] or 0)
        phase_summaries[phase] = {"batch_count": int(phase_totals[0] or 0),
                                  "wins": phase_wins, "losses": phase_losses,
                                  "net_units": phase_wins - phase_losses}
    previous_criteria = [HighDominance2xBatch.cohort_id == cohort.cohort_id]
    if latest_anchor is not None:
        previous_criteria.append(HighDominance2xBatch.series_anchor_position != latest_anchor)
    history_count = session.scalar(select(func.count(HighDominance2xBatch.batch_id)).where(
        *previous_criteria)) or 0
    return {
        "policy_version": HIGH_DOMINANCE_2X_POLICY_VERSION,
        "series_machine": ({**machine,
            "quarantined_series": machine.get("quarantined_series") or machine.get("series"),
            "series": None, "completed_series": []}
            if machine.get("state") == "UNSAFE_STATE"
            or machine.get("data_health") == "DATA_STALE"
            or machine.get("provenance") == "UNVERIFIABLE" else machine),
        "round_sync": round_sync,
        "status": status,
        "current_confirmed_state": cohort.confirmed_state,
        "current_horizon_states": current_horizon_states,
        # Descriptive latest verified horizon remains visible during series
        # quarantine. It never restores eligibility or advances the machine.
        "current_verified_window": current_window,
        "current_high_horizons": sorted(
            [horizon for horizon, state in current_horizon_states.items()
             if state == "HIGH_DOMINANT"], key=int),
        "dominance_scope": "HIGH_DOMINANT_ONLY",
        "batch_size": HIGH_DOMINANCE_2X_BATCH_SIZE,
        "continuation_window_rounds": HIGH_DOMINANCE_2X_BATCH_SIZE,
        "continuation_rounds": int(continuation_rounds),
        "continuation_rounds_remaining": max(0, HIGH_DOMINANCE_2X_BATCH_SIZE - int(continuation_rounds)),
        "continuation_waiting_for_resume": continuation_waiting_for_resume,
        "target_multiplier": "2.00",
        "win_rule": "multiplier >= 2.00; exactly 2.00x counts as WIN",
        "net_rule": "1-unit stake at a fixed 2.00x target: WIN +1 unit, LOSS -1 unit; hypothetical only",
        "descriptive_only": True,
        "trigger_horizon": trigger_horizon,
        "trigger_state": trigger_state,
        "active_batch": active_payload,
        # This legacy projection is only a live series when the reducer policy
        # is absent. Otherwise old batch rows must not masquerade as current.
        "current_series": ({
            "series_anchor_position": latest_anchor,
            "trigger_horizon": latest_batch.trigger_horizon if latest_batch else None,
            "initial_batch": initial_batch,
            "continuation_batches": continuation_batches,
            "continuation_batch_count": int(continuation_batch_count),
            "active_phase": active.phase if active else None,
        } if latest_anchor is not None and not machine_is_authoritative else None),
        "summary": summary,
        "phase_summaries": phase_summaries,
        "history_count": int(history_count),
        "batches": batches,
    }


def _hybrid_payload(session: Session, *, now: datetime | None = None, window_limit: int = 20) -> dict:
    now = now or datetime.now(timezone.utc)
    cohort = session.get(HybridCohort, 1)
    if cohort is None:
        return {"available": False, "policy_version": POLICY_VERSION,
                "reason": "HYBRID_COHORT_NOT_INITIALIZED"}
    joined = session.execute(
        select(HybridCohortRound, CanonicalRound)
        .join(CanonicalRound, CanonicalRound.identity_key == HybridCohortRound.identity_key)
        .where(HybridCohortRound.cohort_id == cohort.cohort_id)
        .order_by(HybridCohortRound.cohort_position.desc()).limit(500)
    ).all()
    rounds = [_hybrid_round(cp, row) for cp, row in reversed(joined)]
    current_number = max(1, (cohort.last_cohort_position - 1) // 100 + 1)
    current_window = session.get(DominanceWindow, f"{cohort.cohort_id}:{current_number:06d}")
    window_rounds = [row for row in rounds if row["cohort_position"] >= (current_number - 1) * 100 + 1]
    rolling = rolling_analysis(rounds)
    rolling["confirmed_state"] = cohort.confirmed_state
    rolling["candidate_state"] = cohort.candidate_state
    rolling["confirmed_momentum"] = cohort.confirmed_momentum
    rolling["candidate_momentum"] = cohort.candidate_momentum
    health = _latest_health(session)
    source_health = health.get("winner_predict_database") or health.get("orbit_collector") or {}
    collector_state = str(source_health.get("state") or "UNKNOWN")
    verified_source_rows = session.scalars(select(CanonicalRound).where(
        CanonicalRound.import_snapshot_id.is_(None),
        CanonicalRound.order_quality.in_((
            "winner_predict_canonical_order_verified",
            "verified_orbit_delta_aligned_to_phase1",
        )),
    ).order_by(CanonicalRound.analysis_position.desc()).limit(500)).all()
    cadence_rows = [{"identity_key": row.identity_key, "round_id": row.round_id,
        "analysis_position": row.analysis_position, "outcome": row.outcome,
        "source_name": row.source_name, "source_sequence": row.source_sequence,
        "order_quality": row.order_quality, "timestamp_quality": row.timestamp_quality,
        "event_timestamp": row.event_timestamp, "source_observed_at": row.source_observed_at,
        "ingestion_timestamp": row.ingestion_timestamp}
        for row in reversed(verified_source_rows)]
    excluded_gap_rows = session.scalars(select(CanonicalRound).where(
        CanonicalRound.import_snapshot_id.is_(None),
        CanonicalRound.source_name == "winner_predict_database",
        CanonicalRound.order_quality == "winner_predict_order_uncertain_source_gap",
    ).order_by(CanonicalRound.analysis_position.desc()).limit(500)).all()
    excluded_gap_context = [{"identity_key": row.identity_key, "round_id": row.round_id,
        "source_name": row.source_name, "source_sequence": row.source_sequence,
        "multiplier_raw": row.multiplier_raw, "order_quality": row.order_quality}
        for row in excluded_gap_rows]
    # Pre-activation verified rounds calibrate cadence and freshness only. They
    # never enter the prospective cohort, rolling outcomes, or its windows.
    coverage = time_coverage(cadence_rows, now=now, collector_state=collector_state,
                             excluded_rounds=excluded_gap_context)
    coverage["cadence_calibration_round_count"] = len(cadence_rows)
    coverage["cadence_calibration_scope"] = "verified canonical rows; may predate cohort activation; outcomes excluded from cohort analysis"
    latest = _latest_round(session)
    if latest:
        last_observed = latest.source_observed_at or latest.ingestion_timestamp or latest.event_timestamp
        if last_observed:
            from .hybrid import as_utc
            age = max(0.0, (as_utc(now) - as_utc(last_observed)).total_seconds())
            coverage["latest_canonical_round"] = {
                "identity_key": latest.identity_key, "round_id": latest.round_id,
                "analysis_position": latest.analysis_position,
                "source_sequence": latest.source_sequence, "source_name": latest.source_name,
                "outcome": latest.outcome, "order_quality": latest.order_quality,
                "observed_at": as_utc(last_observed).isoformat(),
                "age_seconds": age,
            }
            if "source_gap" in latest.order_quality:
                coverage["status"] = "CONFIRMED_SOURCE_GAP"
                coverage["confirmed_source_gaps"].append({
                    "latest_identity_key": latest.identity_key,
                    "source_name": latest.source_name,
                    "source_sequence": latest.source_sequence,
                    "missing_count": None,
                    "evidence": "authoritative upstream order-quality flag; count unavailable here",
                })
    progress = estimated_window_progress(window_rounds, coverage, now=now)
    if window_rounds:
        if coverage.get("status") == "POSSIBLE_MISSING_ROUNDS":
            progress["time_estimate_progress"]["status"] = "COVERAGE_UNCERTAIN"
        elif (progress["time_estimate_progress"]["status"] == "TIME_ESTIMATED_100_ELAPSED"
              and coverage.get("status") == "COVERAGE_OBSERVED"):
            coverage["status"] = "TIME_ESTIMATED_100_ELAPSED"
    completed = session.scalars(select(DominanceWindow).where(
        DominanceWindow.cohort_id == cohort.cohort_id,
        DominanceWindow.lifecycle_status == "REPORTED"
    ).order_by(DominanceWindow.window_number.desc()).limit(window_limit)).all()
    window_list = session.scalars(select(DominanceWindow).where(
        DominanceWindow.cohort_id == cohort.cohort_id
    ).order_by(DominanceWindow.window_number.desc()).limit(window_limit)).all()
    high_dominance_2x = _high_dominance_2x_payload(session, cohort)
    live_notifications = session.scalars(select(CollectorActivity).where(
        CollectorActivity.event_type == "hybrid_live_notification"
    ).order_by(CollectorActivity.id.desc()).limit(50)).all()
    return {
        "available": True, "policy_version": POLICY_VERSION,
        "cohort": {"cohort_id": cohort.cohort_id, "status": "PROSPECTIVE",
                   "activated_at": _timestamp(cohort.activated_at),
                   "anchor": "first eligible verified canonical round inserted after activation",
                   "last_cohort_position": cohort.last_cohort_position,
                   "last_identity_key": cohort.last_identity_key,
                   "provider_global_id_created": False},
        "collector": source_health, "analysis_worker": health.get("analysis_worker") or {},
        "coverage": coverage,
        "rolling": rolling,
        "high_dominance_2x": high_dominance_2x,
        "high_dominance_state_test": _high_dominance_state_test_payload(session, cohort),
        "live_notification_events": [{"id": item.id, **item.details}
                                     for item in reversed(live_notifications)
                                     if str(item.details.get("event_id", "")).startswith(cohort.cohort_id + ":")],
        "current_window": ({"window_id": current_window.window_id,
            "window_number": current_window.window_number,
            "lifecycle_status": current_window.lifecycle_status,
            "summary": current_window.current_summary,
            "report": current_window.report,
            "round_progress": progress["verified_round_progress"],
            "time_progress": progress["time_estimate_progress"]}
            if current_window else {"window_id": None, "window_number": current_number,
                "lifecycle_status": "OPEN", "summary": None,
                "round_progress": {"completed": 0, "required": 100, "fraction": 0},
                "time_progress": progress["time_estimate_progress"]}),
        "completed_windows": [window.report for window in reversed(completed) if window.report],
        "windows": [{"window_id": window.window_id, "window_number": window.window_number,
                     "lifecycle_status": window.lifecycle_status,
                     "previous_window_id": window.previous_window_id,
                     "next_window_id": window.next_window_id,
                     "start_cohort_position": window.start_cohort_position,
                     "end_cohort_position": window.end_cohort_position,
                     "summary": window.current_summary, "report": window.report,
                     "sound_alerts": window.sound_alerts}
                    for window in reversed(window_list)],
        "research_reference": {"high_share_nonexact": 25966 / (25966 + 27398),
                               "rounds": 53503, "inferential_use": "DESCRIPTIVE_ONLY"},
        "limitations": ["Time estimates never create or complete observed rounds.",
                        "Dominance and cluster measures are descriptive, not forecasts."],
    }


def _assessment(row: Next100Assessment | None) -> dict | None:
    if row is None:
        return None
    return {
        "id": row.id, "assessed_at": _timestamp(row.assessed_at),
        "latest_round": row.latest_identity_key, "latest_position": row.latest_position,
        "training_blocks": row.training_blocks,
        "expected_high_rate_nonexact": row.expected_high_rate_nonexact,
        "expected_exact_rate": row.expected_exact_rate,
        "expected_high_count": row.expected_high_count,
        "expected_low_count": row.expected_low_count,
        "expected_exact_count": row.expected_exact_count,
        "historical_count_distribution": row.historical_count_distribution,
        "threshold_probabilities": row.threshold_probabilities,
        "data_sufficiency": row.data_sufficiency, "state": row.state,
        "forecast_method": ((row.explanation or {}).get("forecast_method")
                            or "phase1_expanding_mean_of_disjoint_future100_blocks"),
        "prediction_status": ((row.explanation or {}).get("prediction_status")
                               or "historical_statistical_assessment_not_exact_outcome_prediction"),
        "active_rules": row.active_rules, "explanation": row.explanation,
        "live_context": (row.explanation or {}).get("live_context"),
        "historical_recovery_comparison": (row.explanation or {}).get(
            "historical_recovery_comparison"),
    }


def _phase1_recovery_reference(session: Session, imported: ResearchImport | None) -> tuple[dict | None, float | None]:
    if imported is None:
        return None, None
    cache_key = imported.input_files_sha256 or imported.source_sha256
    reference = cached_recovery_reference(
        cache_key,
        lambda: session.scalars(select(CanonicalRound.outcome).where(
            CanonicalRound.import_snapshot_id == imported.snapshot_id
        ).order_by(CanonicalRound.analysis_position)).all())
    denominator = imported.high_count + imported.low_count
    return reference, imported.high_count / denominator if denominator else None


def _winner_database_context(session: Session, imported: ResearchImport | None) -> dict:
    health = session.get(SystemHealth, "winner_predict_database")
    details = health.details if health and isinstance(health.details, dict) else {}
    newest = session.scalars(select(CanonicalRound).where(
        CanonicalRound.source_name == "winner_predict_database"
    ).order_by(CanonicalRound.analysis_position.desc()).limit(500)).all()
    rows = list(reversed(newest))
    counts_rows = session.execute(select(CanonicalRound.outcome, func.count()).where(
        CanonicalRound.source_name == "winner_predict_database"
    ).group_by(CanonicalRound.outcome)).all()
    session_counts = {symbol: 0 for symbol in ("H", "L", "E")}
    for symbol, count in counts_rows:
        if symbol in session_counts:
            session_counts[symbol] = int(count)
    checked_at = _timestamp(health.checked_at if health else None)
    latest = rows[-1] if rows else None
    sample = rows[-50:]
    errors = []
    if any("uncertain" in row.order_quality for row in sample):
        errors.append("source_order_contains_an_explicit_continuity_gap")
    for previous, current in zip(sample, sample[1:]):
        if (previous.source_sequence is not None and current.source_sequence is not None
                and current.source_sequence != previous.source_sequence + 1):
            errors.append("source_round_indexes_are_not_contiguous")
            break
    rounds = [{"outcome": row.outcome, "multiplier_raw": row.multiplier_raw,
               "event_timestamp": row.event_timestamp,
               "ingestion_timestamp": row.ingestion_timestamp}
              for row in rows]
    windows = rolling_features(rounds)
    rolling50 = windows.get("50", {})
    definitions = session.scalars(select(RuleDefinition).where(
        RuleDefinition.rule_id == "BEHAVIOUR_STATE_DESCRIPTION"
    ).order_by(RuleDefinition.version.desc()).limit(1)).first()
    thresholds = (definitions.thresholds or {}) if definitions and definitions.enabled else {}
    live_state, state_evidence = classify_state(
        rolling50,
        min_rounds=int(thresholds.get("minimum_nonexact", 20)),
        high_dominance=float(thresholds.get("high_dominant", .60)),
        low_dominance=float(thresholds.get("low_dominant", .40)),
        balanced_band=float(thresholds.get("balanced_half_width", .08)),
        alternation_threshold=float(thresholds.get("alternation", .65)),
        high_cluster_run_p90=int(thresholds.get("high_run_p90", 7)),
        low_cluster_run_p90=int(thresholds.get("low_run_p90", 8)))
    reference, phase1_rate = _phase1_recovery_reference(session, imported)
    recovery = compare_historical_recovery([row["outcome"] for row in rounds[-50:]],
                                          reference, phase1_rate)
    round_count = sum(session_counts.values())
    if latest and latest.event_timestamp:
        timestamp_status = "PLATFORM_EVENT_TIME_AVAILABLE_WHEN_PRESENT"
    elif latest and latest.source_observed_at:
        timestamp_status = "PER_ROUND_SOURCE_OBSERVATION_TIME; PLATFORM_EVENT_TIME_UNAVAILABLE"
    else:
        timestamp_status = "SOURCE_TIMESTAMPS_UNAVAILABLE"
    if not rows:
        context_status = "WAITING_FOR_VERIFIED_PHASE1_OVERLAP"
    elif errors:
        context_status = "INVALID"
    else:
        context_status = "VALID" if len(rows) >= 50 else "INSUFFICIENT_ROUNDS"
    return {
        "source": "winner_predict_postgresql_aviator_rounds",
        "source_reading_id": str(latest.source_sequence) if latest else None,
        "latest_source_reading_id": str(details.get("sync_cursor_round_index") or "") or None,
        "source_checked_at": checked_at,
        "context_source_checked_at": checked_at,
        "context_updated_at": _timestamp(latest.ingestion_timestamp if latest else None),
        "source_latest_stored_at": details.get("source_latest_stored_at"),
        "returned_rounds": len(rows), "validated_rounds": len(rows),
        "initial_context_rounds": len(rows), "rolling_window_rounds": min(50, len(rows)),
        "rolling_windows": windows, "rolling50": rolling50,
        "counts": rolling50.get("counts", {symbol: 0 for symbol in ("H", "L", "E")}),
        "high_rate_nonexact": rolling50.get("high_rate_nonexact"),
        "low_rate_nonexact": rolling50.get("low_rate_nonexact"),
        "recent_sequence_hle": "".join(row["outcome"] for row in rounds[-50:]),
        "sequence": sequence_features([row["outcome"] for row in rounds]),
        "current_state": live_state, "state_evidence": state_evidence,
        "latest_round": ({
            "source": "winner_predict_database", "round_id": latest.round_id,
            "round_index": latest.source_sequence,
            "analysis_position": latest.analysis_position,
            "multiplier": latest.multiplier_raw, "outcome": latest.outcome,
            "event_timestamp": _timestamp(latest.event_timestamp),
            "source_observed_at": _timestamp(latest.source_observed_at),
            "ingestion_timestamp": _timestamp(latest.ingestion_timestamp),
            "timestamp_quality": latest.timestamp_quality,
            "order_quality": latest.order_quality,
        } if latest else None),
        "session_history": {
            "source": "Winner Predict PostgreSQL aviator_rounds",
            "counts": session_counts, "round_count": round_count,
            "continuity_status": details.get("continuity_status", "UNKNOWN"),
            "phase1_boundary_continuity": details.get("continuity_status", "UNKNOWN"),
            "seed_context_merged": bool(details.get("overlap_rounds")),
        },
        "validation": {"valid": not errors and bool(rows), "errors": errors,
                        "source_order": "canonical round_index, anchored by exact shared round IDs",
                        "cross_source_continuity": details.get("continuity_status", "UNKNOWN")},
        "timestamp_status": timestamp_status,
        "historical_recovery_comparison": recovery,
        "context_status": context_status,
    }


def _live_context(session: Session, imported: ResearchImport | None) -> dict | None:
    if settings.WINNER_PREDICT_DATABASE_URL:
        return _winner_database_context(session, imported)
    watch = session.get(OrbitWatchState, 1)
    if watch is None:
        return None
    reading = session.scalar(select(OrbitReading).where(
        OrbitReading.watch_id == watch.watch_id,
        OrbitReading.reading_id == watch.cursor).limit(1))
    collector_health = session.get(SystemHealth, "orbit_collector")
    health_details = collector_health.details if collector_health and isinstance(
        collector_health.details, dict) else {}
    latest_source_checked_at = (health_details.get("latest_source_checked_at")
                                or health_details.get("last_checked_at"))
    latest_source_reading_id = (health_details.get("latest_source_cursor")
                                or health_details.get("cursor") or watch.cursor)
    source_items = watch.last_snapshot if isinstance(watch.last_snapshot, list) else []
    errors = []
    provider_visible_count = None
    coalesced_duplicate_count = 0
    if reading:
        try:
            from .collector import _visible_dom
            from .orbit_client import parse_reading
            parsed_reading = parse_reading(reading.raw_payload)
            normalized_source = _visible_dom(parsed_reading)
            provider_visible_count = sum(item.visible for item in parsed_reading.items)
            coalesced_duplicate_count = provider_visible_count - len(normalized_source)
            normalized_stored = [(
                item["item_index"], item["multiplier_normalized"],
                item.get("source_item_indices", [item["item_index"]]))
                for item in source_items if isinstance(item, dict)
            ]
            normalized_expected = [(
                item["item_index"], item["multiplier_normalized"],
                item.get("source_item_indices", [item["item_index"]]))
                for item in normalized_source
            ]
            if normalized_stored != normalized_expected:
                errors.append("stored_context_does_not_match_latest_raw_reading")
            if parsed_reading.truncated:
                errors.append("latest_raw_reading_is_truncated")
        except Exception:
            errors.append("latest_raw_reading_failed_validation")
    parsed_items = []
    seen_indices = set()
    seen_source_indices = set()
    last_index = -1
    for item in source_items:
        if not isinstance(item, dict) or item.get("visible") is not True:
            errors.append("invalid_or_nonvisible_context_item")
            continue
        index = item.get("item_index")
        raw = item.get("multiplier_raw")
        source_indices = item.get("source_item_indices", [index])
        if not isinstance(index, int) or index <= last_index or index in seen_indices:
            errors.append("item_order_or_identity_invalid")
            continue
        if (not isinstance(source_indices, list) or not source_indices
                or any(not isinstance(source_index, int) for source_index in source_indices)
                or len(set(source_indices)) != len(source_indices)
                or seen_source_indices.intersection(source_indices)):
            errors.append("source_item_duplicate_or_invalid")
            continue
        try:
            multiplier = Decimal(str(raw))
            if not multiplier.is_finite() or multiplier < Decimal("1"):
                raise InvalidOperation
            outcome = classify_multiplier(multiplier)
        except (InvalidOperation, TypeError, ValueError):
            errors.append("invalid_multiplier")
            continue
        last_index = index
        seen_indices.add(index)
        seen_source_indices.update(source_indices)
        parsed_items.append({"item_index": index, "multiplier_raw": str(raw),
                             "outcome": outcome, "source_item_indices": source_indices})

    chronological = list(reversed(parsed_items))
    usable = chronological if not errors else []
    rounds = [{"outcome": item["outcome"], "multiplier_raw": item["multiplier_raw"],
               "event_timestamp": None,
               "ingestion_timestamp": reading.checked_at if reading else None}
              for item in usable]
    windows = rolling_features(rounds)
    rolling50 = windows.get("50", {})
    definitions = session.scalars(select(RuleDefinition).where(
        RuleDefinition.rule_id == "BEHAVIOUR_STATE_DESCRIPTION"
    ).order_by(RuleDefinition.version.desc()).limit(1)).first()
    thresholds = (definitions.thresholds or {}) if definitions and definitions.enabled else {}
    live_state, state_evidence = classify_state(
        rolling50,
        min_rounds=int(thresholds.get("minimum_nonexact", 20)),
        high_dominance=float(thresholds.get("high_dominant", .60)),
        low_dominance=float(thresholds.get("low_dominant", .40)),
        balanced_band=float(thresholds.get("balanced_half_width", .08)),
        alternation_threshold=float(thresholds.get("alternation", .65)),
        high_cluster_run_p90=int(thresholds.get("high_run_p90", 7)),
        low_cluster_run_p90=int(thresholds.get("low_run_p90", 8)))
    counts = {symbol: sum(item["outcome"] == symbol for item in usable)
              for symbol in ("H", "L", "E")}
    session_counts_rows = session.execute(
        select(CanonicalRound.outcome, func.count()).where(
            CanonicalRound.source_name == "orbit_data_watch",
            CanonicalRound.import_snapshot_id.is_(None),
        ).group_by(CanonicalRound.outcome)).all()
    session_counts = {symbol: 0 for symbol in ("H", "L", "E")}
    for symbol, count in session_counts_rows:
        if symbol in session_counts:
            session_counts[symbol] = int(count)
    phase1_reference, phase1_rate = _phase1_recovery_reference(session, imported)
    recovery = compare_historical_recovery(
        [item["outcome"] for item in usable], phase1_reference, phase1_rate)
    latest_item = usable[-1] if usable else None
    context_time = _timestamp(reading.checked_at if reading else watch.updated_at)
    return {
        "source": "orbit_live_round_history",
        "source_reading_id": watch.cursor,
        "latest_source_reading_id": str(latest_source_reading_id),
        "source_checked_at": (latest_source_checked_at
                              or _timestamp(reading.checked_at if reading else None)),
        "context_source_checked_at": _timestamp(reading.checked_at if reading else None),
        "context_updated_at": _timestamp(watch.updated_at),
        "returned_rounds": len(source_items),
        "validated_rounds": len(parsed_items),
        "initial_context_rounds": len(source_items),
        "rolling_window_rounds": min(50, len(usable)),
        "rolling_windows": windows,
        "rolling50": rolling50,
        "counts": rolling50.get("counts", counts),
        "high_rate_nonexact": rolling50.get("high_rate_nonexact"),
        "low_rate_nonexact": rolling50.get("low_rate_nonexact"),
        "recent_sequence_hle": "".join(item["outcome"] for item in usable[-50:]),
        "sequence": sequence_features([item["outcome"] for item in usable]),
        "current_state": live_state,
        "state_evidence": state_evidence,
        "latest_round": ({"source": "orbit_data_watch",
                          "reading_id": watch.cursor,
                          "source_item_indices": latest_item["source_item_indices"],
                          "multiplier": latest_item["multiplier_raw"],
                          "outcome": latest_item["outcome"],
                          "event_timestamp": None,
                          "ingestion_timestamp": context_time,
                          "order_quality": "source_history_newest_first_reversed_to_chronological",
                          "timestamp_quality": "reading_checked_at_only"}
                         if latest_item else None),
        "session_history": {"source": "durable Orbit accepted deltas",
                            "counts": session_counts,
                            "round_count": sum(session_counts.values()),
                            "continuity_status": watch.continuity_status,
                            "phase1_boundary_continuity": watch.continuity_status,
                            "seed_context_merged": False},
        "validation": {"valid": not errors, "errors": errors,
                       "item_order": "validated by strictly increasing source item indexes; reversed from Orbit newest-first order",
                       "duplicates": {"provider_visible_items": provider_visible_count,
                                      "coalesced_duplicate_items": coalesced_duplicate_count,
                                      "exact_duplicate_halves_coalesced": coalesced_duplicate_count > 0,
                                      "source_item_identities_checked": True},
                       "per_round_timestamp_gaps": "unavailable: Orbit returns one checkedAt per reading, not event timestamps",
                       "cross_source_continuity": watch.continuity_status},
        "timestamp_status": "PER_ROUND_EVENT_TIMESTAMPS_UNAVAILABLE",
        "historical_recovery_comparison": recovery,
        "context_status": "VALID" if not errors and len(usable) >= 50 else (
            "INVALID" if errors else "INSUFFICIENT_ROUNDS"),
    }


def create_app(engine: Engine | None = None) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        owns_engine = False
        active_engine = engine
        if active_engine is None and settings.DATABASE_URL:
            active_engine = make_engine(settings.DATABASE_URL)
            owns_engine = True
        app.state.engine = active_engine
        live_engine = (active_engine if engine is not None else
                       make_live_journal_engine(settings.LIVE_PROCESSOR_DATABASE_URL))
        owns_live_engine = engine is None
        app.state.live_engine = live_engine
        app.state.live_error = None
        try:
            migrate(live_engine)
            baseline = hydrate_live_journal_baseline(live_engine)
            log.info("[BEHAVIOUR_LIVE_STARTUP] journal=%s baseline=%s",
                     settings.LIVE_PROCESSOR_DATABASE_URL if owns_live_engine else "injected-test-engine",
                     baseline)
            app.state.live_strategy_processor = LiveStrategyProcessor(live_engine)
            app.state.live_eligibility_publisher_task = asyncio.create_task(
                _run_live_eligibility_publisher(live_engine, app.state.live_strategy_processor),
                name="behaviour-live-eligibility-publisher")
        except Exception as exc:
            app.state.live_error = type(exc).__name__
            log.exception("behaviour live journal startup failed")
        if active_engine is not None and active_engine is not live_engine:
            try:
                migrate(active_engine)
                app.state.database_error = None
            except Exception as exc:
                app.state.database_error = type(exc).__name__
                log.exception("behaviour database startup failed")
        elif active_engine is live_engine:
            # An injected engine serves both the analysis routes and the local
            # processor in tests/embedded deployments. If its migration failed,
            # do not mark the analysis API healthy simply because the live
            # startup exception was caught above.
            app.state.database_error = app.state.live_error
        else:
            app.state.database_error = "BEHAVIOUR_DATABASE_URL_MISSING"
        try:
            yield
        finally:
            publisher_task = getattr(app.state, "live_eligibility_publisher_task", None)
            if publisher_task:
                publisher_task.cancel()
                try:
                    await publisher_task
                except asyncio.CancelledError:
                    pass
            if owns_engine and active_engine is not None:
                active_engine.dispose()
            if owns_live_engine and live_engine is not None:
                live_engine.dispose()

    app = FastAPI(title="Advanced Crash Game Behaviour Detection System",
                  description="Independent, explainable, deterministic behaviour research API.",
                  version=__version__, lifespan=lifespan)
    origins_raw = os.getenv("BEHAVIOUR_CORS_ORIGINS", "[]")
    try:
        origins = json.loads(origins_raw)
        if not isinstance(origins, list) or not all(isinstance(x, str) for x in origins):
            origins = []
    except ValueError:
        origins = []
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins,
                           allow_credentials=False, allow_methods=["GET", "DELETE", "PUT"],
                           allow_headers=["Accept", "Content-Type"])

    @app.get("/health/live", tags=["health"])
    def live():
        return {"ok": True, "service": "advanced-crash-behaviour", "version": __version__}

    @app.get("/health/ready", tags=["health"])
    def ready(request: Request):
        live = getattr(request.app.state, "live_engine", None)
        if live is None or getattr(request.app.state, "live_error", None):
            raise HTTPException(status_code=503, detail={
                "ok": False, "state": "NOT_READY", "reason": "BEHAVIOUR_LIVE_JOURNAL_NOT_READY"})
        try:
            with live.connect() as connection:
                connection.execute(text("SELECT 1"))
            database = "connected"
            active = getattr(request.app.state, "engine", None)
            if active is None or getattr(request.app.state, "database_error", None):
                database = "offline"
            else:
                try:
                    with active.connect() as connection:
                        connection.execute(text("SELECT 1"))
                except Exception:
                    database = "offline"
            return {"ok": True,
                    "state": "READY" if database == "connected" else "READY_DATABASE_OFFLINE",
                    "database": database, "live_processor": "ready"}
        except Exception as exc:
            raise HTTPException(status_code=503, detail={
                "ok": False, "state": "NOT_READY", "reason": type(exc).__name__}) from None

    @app.get("/internal/round-checkpoint", tags=["internal"])
    def round_checkpoint(request: Request, stream_id: str = Query(..., min_length=1, max_length=120)):
        """Expose the last contiguous hub round already staged by Behaviour for restart reconciliation."""
        import ipaddress
        peer = getattr(getattr(request, "client", None), "host", "")
        try:
            trusted_peer = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            trusted_peer = False
        if not trusted_peer:
            raise HTTPException(status_code=403, detail={"error": "round_checkpoint_source_not_trusted"})
        active = _get_live_engine(request)
        with Session(active) as session:
            row = session.scalars(select(BehaviourApiRound).where(
                BehaviourApiRound.stream_id == stream_id,
                BehaviourApiRound.status.in_({"PROMOTED", "RECONCILED"})).order_by(
                    BehaviourApiRound.round_index.desc()).limit(1)).first()
        return {"ok": True, "stream_id": stream_id,
                "round_id": row.round_id if row else None,
                "round_index": row.round_index if row else None}

    @app.post("/internal/round-observed", tags=["internal"])
    def round_observed(request: Request, body: dict = Body(...)):
        """Analyze a verified live API observation before PostgreSQL handoff."""
        import ipaddress
        peer = getattr(getattr(request, "client", None), "host", "")
        try:
            trusted_peer = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            trusted_peer = False
        if not trusted_peer:
            raise HTTPException(status_code=403, detail={"error": "round_observation_source_not_trusted"})
        active = _get_live_engine(request)
        try:
            staged = insert_verified_api_observation(active, body)
        except ValueError as exc:
            code = str(exc)
            log.warning("[BEHAVIOUR_API_ROUND] rejected round_id=%s index=%s error=%s",
                        body.get("round_id"), body.get("round_index"), code)
            raise HTTPException(status_code=409, detail={"error": code}) from exc
        if staged == "STAGED_GAP":
            raise HTTPException(status_code=409, detail={"error": "behaviour_round_sequence_gap"})
        decision = None
        processor = getattr(request.app.state, "live_strategy_processor", None)
        if processor is not None and staged in {"PROMOTED", "DUPLICATE", "RECONCILED"}:
            provenance = body.get("behaviour_provenance")
            if provenance == "LIVE_VERIFIED":
                decision = processor.process(body)
            elif provenance == "RECOVERY_REPLAY":
                decision = processor.recover(body)
            if decision and decision.get("status") in {"ORDER_GAP", "STREAM_BOUNDARY_UNVERIFIED",
                                                        "CANONICAL_ROUND_UNAVAILABLE",
                                                        "API_ROUND_UNAVAILABLE", "COHORT_UNAVAILABLE"}:
                raise HTTPException(status_code=409, detail={"error": "behaviour_live_reconciliation_required",
                    "decision": decision})
        return {"ok": True, "status": "STAGED",
                "inbox_status": staged, "promoted": staged == "PROMOTED",
                "decision": decision,
                "round_id": body.get("round_id"), "analysis_current": False}

    @app.get("/api/v1/behaviour/overview", tags=["behaviour"])
    def overview(session: Session = Depends(_session)):
        snapshot = _latest_snapshot(session)
        stats = session.get(CumulativeStats, "global")
        imported = session.scalar(select(ResearchImport).order_by(ResearchImport.snapshot_utc.desc()).limit(1))
        latest = _latest_round(session)
        current_forecast = session.scalar(select(Next100Assessment).order_by(Next100Assessment.id.desc()).limit(1))
        state_history = session.scalars(select(BehaviourStateHistory).order_by(
            BehaviourStateHistory.id.desc()).limit(1000)).all()
        confirmed_state = state_history[0].confirmed_state if state_history else "UNCERTAIN"
        state_duration_rounds = 0
        for entry in state_history:
            if entry.confirmed_state != confirmed_state:
                break
            state_duration_rounds += 1
        orbit_quarantine = session.scalar(select(func.count()).select_from(OrbitReading).where(
            OrbitReading.status.like("quarantined%"))) or 0
        if stats is None:
            raise HTTPException(status_code=503, detail={"code": "PHASE1_IMPORT_REQUIRED"})
        snapshot_features = (snapshot.features or {}) if snapshot else {}
        state = snapshot_features.get("state", {})
        live = _live_context(session, imported)
        next_assessment = _assessment(current_forecast)
        if live and next_assessment:
            forecast_explanation = dict(next_assessment.get("explanation") or {})
            forecast_explanation["live_context"] = live
            forecast_explanation["historical_recovery_comparison"] = live["historical_recovery_comparison"]
            next_assessment["explanation"] = forecast_explanation
            next_assessment["live_context"] = live
            next_assessment["historical_recovery_comparison"] = live["historical_recovery_comparison"]
        latest_payload = (live.get("latest_round") if live else None)
        if latest_payload is None and latest:
            latest_payload = {"identity_key": latest.identity_key, "round_id": latest.round_id,
                              "analysis_position": latest.analysis_position,
                              "multiplier": latest.multiplier_raw, "outcome": latest.outcome,
                              "event_timestamp": _timestamp(latest.event_timestamp),
                              "ingestion_timestamp": _timestamp(latest.ingestion_timestamp),
                              "timestamp_quality": latest.timestamp_quality,
                              "order_quality": latest.order_quality}
        return {
            "system": {"name": "Advanced Crash Game Behaviour Detection System",
                       "version": __version__, "independent_backend": True,
                       "independent_database": True, "rule_based_only": True},
            "population": {"rounds": stats.round_count,
                           "high": stats.high_count, "low": stats.low_count, "exact": stats.exact_count,
                           "high_rate_all": stats.high_count / max(1, stats.round_count),
                           "low_rate_all": stats.low_count / max(1, stats.round_count),
                           "exact_rate": stats.exact_count / max(1, stats.round_count),
                           "high_rate_nonexact": stats.high_count / max(1, stats.high_count + stats.low_count),
                           "imported_source_records": imported.source_records if imported else 0,
                           "imported_canonical_rounds": imported.canonical_rounds if imported else 0,
                           "live_rounds": max(0, stats.round_count - (imported.canonical_rounds if imported else 0))},
            "latest_round": latest_payload,
            "live_context": live,
            "live_outlook": ({"historical_baseline": {
                                  "expected_high_rate_nonexact": next_assessment.get("expected_high_rate_nonexact"),
                                  "expected_high_count": next_assessment.get("expected_high_count"),
                                  "expected_low_count": next_assessment.get("expected_low_count"),
                                  "expected_exact_count": next_assessment.get("expected_exact_count"),
                                  "high_count_distribution": (next_assessment.get("historical_count_distribution") or {}).get("high_count"),
                                  "method": next_assessment.get("forecast_method"),
                                  "evidence_status": next_assessment.get("data_sufficiency")},
                              "current_context": (live.get("historical_recovery_comparison") if live else None),
                              "assessed_at": _timestamp(current_forecast.assessed_at) if current_forecast else None,
                              "live_context_checked_at": live.get("context_updated_at") if live else None,
                              "exact_outcome_prediction": False} if next_assessment else None),
            "behaviour": {"observed_state": live.get("current_state") if live else state.get("candidate", "UNCERTAIN"),
                          "confirmed_state": confirmed_state,
                          "state_duration_rounds": state_duration_rounds,
                          "state_evidence": live.get("state_evidence", state) if live else state,
                          "sequence": live.get("sequence", snapshot_features.get("sequence", {})) if live else snapshot_features.get("sequence", {}),
                          "rolling_windows": live.get("rolling_windows", snapshot_features.get("rolling_windows", {})) if live else snapshot_features.get("rolling_windows", {}),
                          "global_distribution": snapshot_features.get("global_distribution", {})},
            "next100": next_assessment,
            "data_quality": {"event_timestamp_count": stats.event_timestamp_count,
                             "event_timestamp_missing_count": stats.event_timestamp_missing_count,
                             "timestamp_tie_round_count": stats.timestamp_tie_count,
                             "order_uncertain_count": stats.order_uncertain_count,
                             "cross_source_overlap_unknown_rounds": stats.cross_source_overlap_unknown_count,
                             "quarantined_orbit_readings": orbit_quarantine},
            "health": _latest_health(session),
            "research_snapshot": ({"snapshot_id": imported.snapshot_id,
                                   "snapshot_utc": _timestamp(imported.snapshot_utc),
                                   "source_records": imported.source_records,
                                   "canonical_rounds": imported.canonical_rounds,
                                   "source_sha256": imported.source_sha256,
                                   "input_files_sha256": imported.input_files_sha256} if imported else None),
            "updated_at": _timestamp(snapshot.generated_at) if snapshot else None,
        }

    @app.get("/api/v1/behaviour/hybrid", tags=["behaviour"])
    def hybrid_overview(request: Request, session: Session = Depends(_session)):
        """Prospective rolling/window analysis; read-only and separate from forecasts."""
        payload = _hybrid_payload(session)
        cohort = session.get(HybridCohort, 1)
        live_machine = None
        live_current_window = None
        try:
            live_engine = _get_live_engine(request)
            with Session(live_engine) as live_session:
                live_cohort = live_session.get(HybridCohort, 1)
                if live_cohort is not None:
                    live_current_window = _current_verified_window(live_session, live_cohort, 5)
                live_row = (live_session.get(HighDominanceSeriesState, live_cohort.cohort_id)
                            if live_cohort else None)
                if (live_row is not None and isinstance(live_row.payload, dict)
                        and live_row.payload.get("policy_version") == HIGH_SERIES_POLICY_VERSION):
                    live_machine = deepcopy(live_row.payload)
        except HTTPException:
            # Keep the overview available when the low-latency journal is down;
            # its main-store projection will remain visibly stale/quarantined.
            live_machine = None
        if cohort is not None and live_machine is not None:
            payload["high_dominance_2x"] = _high_dominance_2x_payload(
                session, cohort, machine_override=live_machine)
        if live_current_window:
            payload["high_dominance_2x"]["current_verified_window"] = live_current_window
            payload["high_dominance_2x"]["current_verified_window_source"] = "LIVE_VERIFIED_JOURNAL"
        elif live_machine is not None:
            audit = payload["high_dominance_2x"]
            round_sync = audit.get("round_sync") or {}
            live_identity = (live_machine.get("last_live_round_identity")
                             or live_machine.get("last_processed_round_identity"))
            cohort_identity = round_sync.get("last_cohort_identity")
            if live_identity and live_identity == cohort_identity:
                audit["current_verified_window_source"] = "SYNCED_ANALYSIS_COHORT"
            else:
                audit["current_verified_window"] = []
                audit["current_verified_window_source"] = "WAITING_FOR_LIVE_SYNC"
            audit["current_verified_window_latest_live_identity"] = live_identity
        processor = getattr(request.app.state, "live_strategy_processor", None)
        payload["high_dominance_2x"]["live_processor"] = (
            processor.metrics() if processor else {"status": "WAITING_FOR_LIVE", "samples": 0})
        return payload

    @app.get("/api/v1/behaviour/live-status", tags=["health"])
    def live_status(request: Request):
        """Expose the local decision path even when the analysis database is down."""
        active = _get_live_engine(request)
        processor = getattr(request.app.state, "live_strategy_processor", None)
        if processor is None:
            raise HTTPException(status_code=503, detail={"code": "BEHAVIOUR_PROCESSOR_NOT_READY"})
        metrics = processor.metrics()
        with Session(active) as session:
            cohort = session.get(HybridCohort, 1)
            machine_row = session.get(HighDominanceSeriesState, cohort.cohort_id) if cohort else None
            machine = dict(machine_row.payload or {}) if machine_row else {}
        latest = machine.get("latest_strategy_eligibility") or {}
        pending_intents = machine.get("pending_strategy_eligibilities") or []
        journal = {"state": "UNAVAILABLE", "pending_records": None}
        try:
            if active.dialect.name == "sqlite" and inspect(active).has_table("round_observation_inbox_counters"):
                with active.connect() as connection:
                    counter = connection.execute(text(
                        "SELECT pending_records,pending_bytes FROM round_observation_inbox_counters WHERE singleton=1"
                    )).first()
                journal = {"state": "HEALTHY" if counter is not None else "EMPTY",
                           "pending_records": int(counter[0]) if counter else 0,
                           "pending_bytes": int(counter[1]) if counter else 0}
        except Exception as exc:
            journal = {"state": "ERROR", "error": type(exc).__name__, "pending_records": None}
        processor_state = metrics.get("status") or "WAITING_FOR_LIVE"
        if pending_intents:
            eligibility_status = "WAITING_FOR_RULES_ENGINE"
        elif latest.get("eligibility") == "INELIGIBLE":
            eligibility_status = f"NOT_ELIGIBLE · {latest.get('eligibility_reason') or 'POLICY_GATE'}"
        elif latest.get("eligibility") == "ELIGIBLE":
            eligibility_status = ("ELIGIBLE" if latest.get("status") == "PUBLISHED"
                                  else f"ELIGIBLE · {latest.get('status') or 'PUBLISHING'}")
        else:
            eligibility_status = str(latest.get("status") or "WAITING_FOR_LIVE")
        active_database = getattr(request.app.state, "engine", None)
        database_status = "OFFLINE"
        if active_database is not None and not getattr(request.app.state, "database_error", None):
            try:
                with active_database.connect() as connection:
                    connection.execute(text("SELECT 1"))
                database_status = "CONNECTED"
            except Exception:
                database_status = "OFFLINE"
        signal_lock = ({key: latest.get(key) for key in (
            "signal_id", "session_id", "target_round_id", "signal_status",
            "signal_persistence", "signal_accepted_at", "signal_locked_at",
            "lock_acknowledged_at", "rules_engine_acceptance_to_durable_lock_ms")}
            if latest.get("signal_id") else {"status": "NO_SIGNAL_ACCEPTED"})
        orbit_status = _read_orbit_live_status()
        return {"orbit_source": orbit_status["stream_status"],
                "orbit": orbit_status, "processor": metrics,
                "processor_state": processor_state,
                "strategy_state": machine.get("state") or "IDLE",
                "strategy_provenance": machine.get("provenance") or "HISTORICAL_HYDRATION",
                "signal_eligibility": eligibility_status,
                "latest_decision_round_uid": latest.get("source_round_id"),
                "latest_signal_lock": signal_lock,
                "sqlite_journal": journal,
                "database_status": database_status,
                "pre_start_timing": "UNVERIFIED"}

    @app.put("/api/v1/behaviour/hybrid/high-series/config", tags=["behaviour"])
    def update_high_series_config(request: Request, payload: dict = Body(...),
                                  session: Session = Depends(_live_session)):
        origin = request.headers.get("origin")
        peer = str(getattr(getattr(request, "client", None), "host", "") or "")
        try:
            local_peer = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            local_peer = peer in {"testclient", "localhost"}
        if not local_peer or (origin and origin not in origins):
            raise HTTPException(status_code=403, detail={
                "code": "HIGH_SERIES_CONFIG_ORIGIN_FORBIDDEN",
                "message": "Strategy settings can only be changed from the configured Behaviour application origin or loopback.",
            })
        cohort = session.get(HybridCohort, 1)
        if cohort is None:
            raise HTTPException(status_code=409, detail={"code": "BEHAVIOUR_COHORT_NOT_READY"})
        machine_row = session.scalar(select(HighDominanceSeriesState).where(
            HighDominanceSeriesState.cohort_id == cohort.cohort_id).with_for_update())
        machine = deepcopy(machine_row.payload) if machine_row else new_high_series_state()
        try:
            current_config = validate_high_series_config(
                machine.get("pending_config") or machine.get("config") or {})
            candidate_payload = deepcopy(current_config)
            nested_part1 = payload.get("part1")
            nested_part2 = payload.get("part2")
            candidate_payload.update({key: value for key, value in payload.items()
                                      if key not in {"part1", "part2", "configuration_version"}})
            if isinstance(nested_part1, dict):
                candidate_payload["part1"] = {**candidate_payload.get("part1", {}), **nested_part1}
            if isinstance(nested_part2, dict):
                candidate_payload["part2"] = {**candidate_payload.get("part2", {}), **nested_part2}
                if ("pause_limit_verified_rounds" in nested_part2
                        and "pause_limit_verified_rounds" not in payload
                        and "part2_pause_limit_verified_rounds" not in payload):
                    candidate_payload["pause_limit_verified_rounds"] = nested_part2[
                        "pause_limit_verified_rounds"]
            next_version = max(int((machine.get("config") or {}).get("configuration_version") or 1),
                               int((machine.get("pending_config") or {}).get("configuration_version") or 0)) + 1
            candidate_payload["configuration_version"] = next_version
            candidate = validate_high_series_config(candidate_payload)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail={"code": "HIGH_SERIES_CONFIG_INVALID",
                                                         "message": str(exc)}) from exc
        has_active_series = bool(machine.get("series"))
        if has_active_series:
            machine["pending_config"] = candidate
            machine["pending_configuration_version"] = next_version
            configuration_status = "PENDING_NEXT_SERIES"
        else:
            machine["config"] = candidate
            machine["pending_config"] = None
            machine["pending_configuration_version"] = None
            configuration_status = "SAVED"
        saved_at = datetime.now(timezone.utc).isoformat()
        machine.setdefault("configuration_audit", []).append({
            "configuration_version": next_version, "saved_at": saved_at,
            "status": configuration_status,
            "series_id": (machine.get("series") or {}).get("series_id"),
            "configuration": deepcopy(candidate),
        })
        machine["configuration_audit"] = machine["configuration_audit"][-100:]
        machine.setdefault("events", []).append({
            "type": "CONFIGURATION_SAVED", "round_identity": machine.get("last_processed_identity"),
            "round_id": None, "position": machine.get("last_processed_position"),
            "timestamp": saved_at, "configuration_version": next_version,
            "configuration_status": configuration_status,
        })
        machine["events"] = machine["events"][-500:]
        if machine_row:
            machine_row.payload = machine
            machine_row.updated_at = datetime.now(timezone.utc)
        else:
            session.add(HighDominanceSeriesState(cohort_id=cohort.cohort_id, payload=machine,
                                                  updated_at=datetime.now(timezone.utc)))
        session.commit()
        active_config = ((machine.get("series") or {}).get("configuration_snapshot")
                         or machine.get("config"))
        return {"config": machine["config"], "active_config": active_config,
                "pending_config": machine.get("pending_config"),
                "configuration_status": configuration_status,
                "configuration_version": next_version}

    @app.post("/api/v1/behaviour/internal/shadow-settlement", tags=["internal"])
    def receive_shadow_settlement(request: Request, payload: dict = Body(...),
                                  session: Session = Depends(_live_session)):
        peer = str(getattr(getattr(request, "client", None), "host", "") or "")
        try:
            local_peer = ipaddress.ip_address(peer).is_loopback
        except ValueError:
            local_peer = peer in {"testclient", "localhost"}
        supplied_secret = request.headers.get("x-behaviour-shadow-secret", "")
        expected_secret = settings.SHADOW_SHARED_SECRET
        if not local_peer or (expected_secret and not hmac.compare_digest(
                expected_secret, supplied_secret)):
            raise HTTPException(status_code=403, detail={"code": "SHADOW_SETTLEMENT_SOURCE_NOT_TRUSTED"})

        required_text = ("signal_id", "session_id", "series_id", "part", "batch_id",
                         "target_round_id", "target_round_identity", "target_stream_id")
        if any(not isinstance(payload.get(key), str) or not payload[key].strip()
               for key in required_text):
            raise HTTPException(status_code=422, detail={"code": "SHADOW_SETTLEMENT_FIELDS_MISSING"})
        try:
            target_index = int(payload.get("target_round_index"))
            configuration_version = int(payload.get("configuration_version"))
            actual_multiplier = Decimal(str(payload.get("actual_multiplier")))
        except (TypeError, ValueError, OverflowError):
            raise HTTPException(status_code=422, detail={"code": "SHADOW_SETTLEMENT_IDENTITY_INVALID"}) from None
        result = str(payload.get("result") or "").upper()
        evidence = payload.get("evidence") if isinstance(payload.get("evidence"), dict) else {}
        try:
            evidence_index = int(evidence.get("target_round_index") or 0)
        except (TypeError, ValueError, OverflowError):
            evidence_index = 0
        if (target_index < 1 or configuration_version < 0
                or payload.get("settlement_status") != "COMMITTED"
                or payload.get("signal_lock_status") != "DURABLY_LOCKED"
                or result not in {"WIN", "LOSS"}
                or not actual_multiplier.is_finite() or actual_multiplier < 1
                or payload.get("target_round_identity") != f"winner-predict:{payload['target_round_id']}"
                or evidence.get("event_type") != "round.persisted"
                or evidence.get("durability") != "COMMITTED"
                or evidence.get("continuity_status") != "VERIFIED"
                or evidence.get("target_round_id") != payload.get("target_round_id")
                or evidence.get("target_stream_id") != payload.get("target_stream_id")
                or evidence_index != target_index):
            raise HTTPException(status_code=422, detail={"code": "SHADOW_SETTLEMENT_NOT_VERIFIED"})

        cohort = session.get(HybridCohort, 1)
        if cohort is None:
            raise HTTPException(status_code=503, detail={"code": "BEHAVIOUR_COHORT_NOT_READY"})
        machine_row = session.scalar(select(HighDominanceSeriesState).where(
            HighDominanceSeriesState.cohort_id == cohort.cohort_id).with_for_update())
        machine = deepcopy(machine_row.payload) if machine_row else None
        locked = ((machine or {}).get("locked_signals") or {}).get(payload["signal_id"])
        if not isinstance(locked, dict):
            raise HTTPException(status_code=409, detail={"code": "SHADOW_SIGNAL_LOCK_NOT_RECONCILED"})
        lock_matches = (
            locked.get("session_id") == payload["session_id"]
            and locked.get("series_id") == payload["series_id"]
            and locked.get("part") == payload["part"]
            and locked.get("batch_id") == payload["batch_id"]
            and int(locked.get("configuration_version") or 0) == configuration_version
            and locked.get("target_round_id") == payload.get("signal_target_round_id")
            and locked.get("target_stream_id") == payload["target_stream_id"]
            and int(locked.get("target_round_index") or 0) == target_index
            and locked.get("lock_status") in {"DURABLY_LOCKED", "EXISTING_DURABLE_SIGNAL"}
        )
        if not lock_matches:
            raise HTTPException(status_code=409, detail={"code": "SHADOW_SETTLEMENT_SIGNAL_MISMATCH"})

        existing = session.get(BehaviourStrategySettlement, payload["signal_id"])
        if existing is not None:
            if (existing.target_round_id != payload["target_round_id"]
                    or existing.target_stream_id != payload["target_stream_id"]
                    or existing.target_round_index != target_index
                    or existing.result != result):
                raise HTTPException(status_code=409, detail={"code": "SHADOW_SETTLEMENT_IDENTITY_CONFLICT"})
            state = "ALREADY_RECORDED"
        else:
            received_at = datetime.now(timezone.utc)
            session.add(BehaviourStrategySettlement(
                signal_id=payload["signal_id"], cohort_id=cohort.cohort_id,
                session_id=payload["session_id"], series_id=payload["series_id"],
                part=payload["part"], batch_id=payload["batch_id"],
                configuration_version=configuration_version,
                target_round_id=payload["target_round_id"],
                target_round_identity=payload["target_round_identity"],
                target_stream_id=payload["target_stream_id"],
                target_round_index=target_index, result=result,
                payload=deepcopy(payload), received_at=received_at))
            session.commit()
            state = "RECORDED"
        if existing is not None:
            session.commit()
        processor = getattr(request.app.state, "live_strategy_processor", None)
        reconciliation = (processor.reconcile_settlement(payload) if processor is not None
                          else {"status": "PROCESSOR_NOT_READY"})
        reconciliation_status = str(reconciliation.get("status") or "")
        if reconciliation_status not in {"RECONCILED", "ALREADY_COUNTED"}:
            raise HTTPException(status_code=503, detail={
                "code": "SHADOW_SETTLEMENT_RECONCILIATION_PENDING",
                "message": "The committed settlement was stored but the Behaviour counter has not acknowledged it yet.",
                "reconciliation": reconciliation,
            })
        return {"status": state, "signal_id": payload["signal_id"],
                "reconciliation": reconciliation}

    @app.delete("/api/v1/behaviour/hybrid/high-series/performance-history", tags=["behaviour"])
    def clear_high_series_performance_history(
        request: Request,
        payload: dict = Body(...),
        session: Session = Depends(_session),
    ):
        """Clear archived/quarantined results while preserving active live work."""
        origin = request.headers.get("origin")
        if not origin or origin not in origins:
            raise HTTPException(status_code=403, detail={
                "code": "HIGH_SERIES_RESET_ORIGIN_FORBIDDEN",
                "message": "Performance history reset is accepted only from a configured Behaviour Detection application origin.",
            })
        if payload.get("confirm") != "RESET_HIGH_SERIES_PERFORMANCE_HISTORY":
            raise HTTPException(status_code=422, detail={
                "code": "HIGH_SERIES_RESET_CONFIRMATION_REQUIRED",
                "message": "Explicit performance-history reset confirmation is required.",
            })
        cohort = session.get(HybridCohort, 1)
        if cohort is None:
            raise HTTPException(status_code=404, detail={
                "code": "HIGH_DOMINANCE_COHORT_NOT_FOUND",
                "message": "The prospective Behaviour cohort is not available.",
            })

        primary_row = session.scalar(select(HighDominanceSeriesState).where(
            HighDominanceSeriesState.cohort_id == cohort.cohort_id).with_for_update())
        primary_machine = deepcopy(primary_row.payload) if primary_row else new_high_series_state()
        live_engine = _get_live_engine(request)
        processor = getattr(request.app.state, "live_strategy_processor", None)
        lock = processor._lock if processor is not None else nullcontext()
        same_engine = live_engine is session.get_bind()
        live_session_context = nullcontext(session) if same_engine else Session(
            live_engine, expire_on_commit=False)

        def series_round_ids(series: dict | None) -> set[str]:
            found: set[str] = set()
            if not isinstance(series, dict):
                return found
            part2 = series.get("part2") or {}
            containers = [series.get("part1")]
            if isinstance(part2, dict):
                containers.extend(part2.get("batches") or [])
                containers.append(part2.get("current_batch"))
            for container in containers:
                if not isinstance(container, dict):
                    continue
                for item in container.get("rounds") or []:
                    if not isinstance(item, dict):
                        continue
                    identity = item.get("identity_key") or item.get("round_id")
                    if identity:
                        found.add(str(identity))
            return found

        now = datetime.now(timezone.utc)
        backup_dir = FilePath(settings.PROJECT_ROOT) / "data" / "behaviour" / "backups"
        backup_name = f"high-series-performance-before-reset-{now.strftime('%Y%m%dT%H%M%S%fZ')}.json"
        backup_path = backup_dir / backup_name
        legacy_rows = session.scalars(select(HighDominance2xBatch).where(
            HighDominance2xBatch.cohort_id == cohort.cohort_id).with_for_update()).all()
        legacy_snapshot = [{column.name: getattr(row, column.name)
                            for column in HighDominance2xBatch.__table__.columns}
                           for row in legacy_rows]

        with lock:
            with live_session_context as live_session:
                live_cohort = live_session.get(HybridCohort, 1)
                live_row = (live_session.scalar(select(HighDominanceSeriesState).where(
                    HighDominanceSeriesState.cohort_id == live_cohort.cohort_id).with_for_update())
                    if live_cohort else None)
                live_machine = (deepcopy(live_row.payload) if live_row
                                and isinstance(live_row.payload, dict) else None)
                machine = (live_machine if live_machine
                           and live_machine.get("policy_version") == HIGH_SERIES_POLICY_VERSION
                           else deepcopy(primary_machine))
                active_states = {"PART1_COLLECTING", "PART1_COMPLETED", "PART2_ACTIVE",
                                 "PART2_PAUSED_LOW_DOMINANT", "PART2_PAUSED_NON_HIGH"}
                active_series_preserved = (machine.get("state") in active_states
                                           and isinstance(machine.get("series"), dict))
                current_series = machine.get("series")
                archived_series = list(machine.get("completed_series") or [])
                series_rounds_cleared: set[str] = set()
                series_rounds_preserved = (series_round_ids(current_series)
                                           if active_series_preserved else set())
                for saved in archived_series:
                    series_rounds_cleared.update(series_round_ids(saved))
                if not active_series_preserved:
                    series_rounds_cleared.update(series_round_ids(current_series))
                    series_rounds_cleared.update(series_round_ids(machine.get("quarantined_series")))
                for saved in primary_machine.get("completed_series") or []:
                    series_rounds_cleared.update(series_round_ids(saved))
                if primary_machine.get("state") == "UNSAFE_STATE":
                    series_rounds_cleared.update(series_round_ids(primary_machine.get("series")))
                    series_rounds_cleared.update(series_round_ids(primary_machine.get("quarantined_series")))
                for batch in legacy_rows:
                    for item in batch.outcomes or []:
                        if isinstance(item, dict):
                            identity = item.get("identity_key") or item.get("round_id")
                            if identity:
                                series_rounds_cleared.add(str(identity))
                series_rounds_cleared.difference_update(series_rounds_preserved)

                backup = {
                    "created_at": now.isoformat(),
                    "scope": "HIGH-series SHADOW history only; active series, pending eligibility, canonical verified rounds, source cursors, and Rules Engine signal history are preserved",
                    "cohort_id": cohort.cohort_id,
                    "live_cohort_id": live_cohort.cohort_id if live_cohort else None,
                    "primary_machine_state": primary_machine,
                    "live_machine_state": live_machine,
                    "legacy_batches": legacy_snapshot,
                }
                try:
                    backup_dir.mkdir(parents=True, exist_ok=True)
                    descriptor = os.open(str(backup_path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                    with os.fdopen(descriptor, "w", encoding="utf-8") as backup_file:
                        json.dump(backup, backup_file, ensure_ascii=False, default=str,
                                  separators=(",", ":"))
                        backup_file.flush()
                        os.fsync(backup_file.fileno())
                except OSError as exc:
                    log.exception("could not back up HIGH-series performance history before reset")
                    raise HTTPException(status_code=500, detail={
                        "code": "HIGH_SERIES_RESET_BACKUP_FAILED",
                        "message": "Performance history was not reset because its backup could not be written.",
                    }) from exc

                machine["completed_series"] = []
                if not active_series_preserved:
                    machine["series"] = None
                    machine.pop("quarantined_series", None)
                    if machine.get("state") == "UNSAFE_STATE":
                        machine["data_health"] = "DATA_STALE"
                        machine["provenance"] = "UNVERIFIABLE"
                        machine["quarantine_reason"] = "PERFORMANCE_HISTORY_RESET_AWAITING_VERIFIED_LIVE_ROUND"
                    elif machine.get("state") == "COOLDOWN":
                        # The cooldown is lifecycle state, not performance history.
                        pass
                eligibility = machine.get("latest_strategy_eligibility")
                if (not active_series_preserved and isinstance(eligibility, dict)
                        and str(eligibility.get("status") or "").upper() in {
                            "REJECTED", "CANCELLED", "UNSAFE_QUARANTINED"}):
                    machine.pop("latest_strategy_eligibility", None)

                if active_series_preserved:
                    trigger_position = (current_series or {}).get("trigger_position")
                    active_ids = set(series_rounds_preserved)
                    trigger_identity = (current_series or {}).get("trigger_round_identity")
                    if trigger_identity:
                        active_ids.add(str(trigger_identity))
                    retained_events = []
                    for event in machine.get("events") or []:
                        if not isinstance(event, dict):
                            continue
                        event_identity = event.get("round_identity")
                        try:
                            event_position = int(event.get("position"))
                        except (TypeError, ValueError, OverflowError):
                            event_position = None
                        if (event_identity and str(event_identity) in active_ids
                                or trigger_position is None and event_position is not None
                                or trigger_position is not None and event_position is not None
                                and event_position >= int(trigger_position)):
                            retained_events.append(event)
                    machine["events"] = retained_events[-499:]
                else:
                    machine["events"] = []
                reset_event = {
                    "type": "PERFORMANCE_HISTORY_CLEARED",
                    "round_identity": None, "round_id": None,
                    "position": machine.get("last_processed_position"),
                    "timestamp": now.isoformat(),
                    "legacy_batches_cleared": len(legacy_rows),
                    "archived_series_cleared": len(archived_series),
                    "series_rounds_cleared": len(series_rounds_cleared),
                    "active_series_preserved": active_series_preserved,
                }
                machine["events"].append(reset_event)

                if live_row is not None and live_machine is not None:
                    live_row.payload = machine
                    live_row.updated_at = now
                if same_engine:
                    if primary_row is not None:
                        primary_row.payload = machine
                        primary_row.updated_at = now
                    else:
                        session.add(HighDominanceSeriesState(
                            cohort_id=cohort.cohort_id, payload=machine, updated_at=now))
                if primary_row is not None and not same_engine and live_row is not None:
                    primary_machine["completed_series"] = []
                    primary_machine["series"] = None
                    primary_machine.pop("quarantined_series", None)
                    primary_machine["events"] = [reset_event]
                    if primary_machine.get("state") == "UNSAFE_STATE":
                        primary_machine["data_health"] = "DATA_STALE"
                        primary_machine["provenance"] = "UNVERIFIABLE"
                    primary_row.payload = primary_machine
                    primary_row.updated_at = now
                elif not same_engine and live_row is None:
                    if primary_row is not None:
                        primary_row.payload = machine
                        primary_row.updated_at = now
                    else:
                        session.add(HighDominanceSeriesState(
                            cohort_id=cohort.cohort_id, payload=machine, updated_at=now))

                session.execute(sql_delete(HighDominance2xBatch).where(
                    HighDominance2xBatch.cohort_id == cohort.cohort_id))
                if same_engine:
                    session.commit()
                else:
                    live_session.commit()
                    session.commit()
                session.expire_all()

        baseline_status = ("ACTIVE_SERIES_PRESERVED" if active_series_preserved else
                           "DATA_STALE_PRESERVED" if machine.get("state") == "UNSAFE_STATE" else
                           "HISTORY_CLEARED")
        summary = _high_dominance_2x_payload(
            session, cohort, machine_override=machine if live_row is not None else None)["summary"]
        return {
            "legacy_batches_cleared": len(legacy_rows),
            "archived_series_cleared": len(archived_series),
            "series_cleared": int(bool(current_series) and not active_series_preserved),
            "series_rounds_cleared": len(series_rounds_cleared),
            "active_series_preserved": active_series_preserved,
            "active_rounds_preserved": len(series_rounds_preserved),
            "pending_eligibilities_preserved": len(machine.get("pending_strategy_eligibilities") or []),
            "backup_file": backup_name,
            "baseline_status": baseline_status,
            "verified_round_history_preserved": True,
            "accepted_signal_history_preserved": True,
            "machine_state": machine.get("state"),
            "summary": summary,
        }

    @app.get("/api/v1/behaviour/hybrid/high-dominance-2x/history", tags=["behaviour"])
    def high_dominance_2x_history(
        phase: str = Query(..., pattern="^(INITIAL|CONTINUATION)$"),
        offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100),
        session: Session = Depends(_session),
    ):
        cohort = session.get(HybridCohort, 1)
        if cohort is None:
            return {"items": [], "total": 0, "offset": offset, "limit": limit}
        latest_anchor = session.scalar(select(func.max(
            HighDominance2xBatch.series_anchor_position
        )).where(HighDominance2xBatch.cohort_id == cohort.cohort_id))
        criteria = [HighDominance2xBatch.cohort_id == cohort.cohort_id,
                    HighDominance2xBatch.phase == phase]
        if latest_anchor is not None:
            criteria.append(HighDominance2xBatch.series_anchor_position != latest_anchor)
        total = session.scalar(select(func.count(HighDominance2xBatch.batch_id)).where(
            *criteria)) or 0
        rows = session.scalars(select(HighDominance2xBatch).where(*criteria).order_by(
            HighDominance2xBatch.series_anchor_position.desc(),
            HighDominance2xBatch.batch_number.desc()).offset(offset).limit(limit)).all()
        return {"items": [_serialize_high_dominance_2x_batch(row) for row in rows],
                "total": int(total), "offset": offset, "limit": limit,
                "phase": phase}

    @app.delete("/api/v1/behaviour/hybrid/high-dominance-state-test/earlier", tags=["behaviour"])
    def delete_earlier_high_episodes(
        request: Request,
        payload: dict = Body(...),
        session: Session = Depends(_session),
    ):
        origin = request.headers.get("origin")
        if not origin or origin not in origins:
            raise HTTPException(status_code=403, detail={
                "code": "HIGH_EPISODE_DELETE_ORIGIN_FORBIDDEN",
                "message": "Episode deletion is accepted only from a configured Behaviour Detection application origin.",
            })
        delete_all = payload.get("delete_all") is True
        episode_ids = payload.get("episode_ids")
        if (delete_all and episode_ids not in (None, [])) or (not delete_all and (
            not isinstance(episode_ids, list) or not 1 <= len(episode_ids) <= 500
            or not all(isinstance(value, str) and 3 <= len(value) <= 180 for value in episode_ids)
            or len(set(episode_ids)) != len(episode_ids)
        )):
            raise HTTPException(status_code=422, detail={
                "code": "HIGH_EPISODE_DELETE_SELECTION_INVALID",
                "message": "Choose valid episode IDs, or delete all earlier episodes.",
            })
        cohort = session.get(HybridCohort, 1)
        if cohort is None:
            raise HTTPException(status_code=404, detail={
                "code": "HIGH_EPISODE_COHORT_NOT_FOUND",
                "message": "The prospective Behaviour cohort is not available.",
            })
        latest_position = session.scalar(select(func.max(HighDominanceStateTest.decision_position)).where(
            HighDominanceStateTest.cohort_id == cohort.cohort_id))
        if latest_position is None:
            return {"deleted_count": 0,
                    "remaining_summary": _high_dominance_state_test_payload(session, cohort)["summary"]}
        criteria = [HighDominanceStateTest.cohort_id == cohort.cohort_id]
        if delete_all:
            criteria.append(HighDominanceStateTest.decision_position < latest_position)
        else:
            criteria.append(HighDominanceStateTest.episode_id.in_(episode_ids))
        selected = session.scalars(select(HighDominanceStateTest).where(*criteria)).all()
        if not delete_all and len(selected) != len(episode_ids):
            raise HTTPException(status_code=404, detail={
                "code": "HIGH_EPISODE_NOT_FOUND",
                "message": "One or more episodes are not in the current Behaviour cohort.",
            })
        if any(row.decision_position == latest_position or row.status == "COLLECTING" for row in selected):
            raise HTTPException(status_code=409, detail={
                "code": "HIGH_EPISODE_PROTECTED",
                "message": "The latest or collecting HIGH episode cannot be deleted.",
            })
        selected_ids = [row.episode_id for row in selected]
        if selected_ids:
            session.execute(sql_delete(HighDominanceStateTest).where(
                HighDominanceStateTest.episode_id.in_(selected_ids)))
            session.commit()
        return {"deleted_count": len(selected_ids),
                "remaining_summary": _high_dominance_state_test_payload(session, cohort)["summary"]}

    @app.delete("/api/v1/behaviour/hybrid/high-dominance-2x/bulk-delete", tags=["behaviour"])
    def bulk_delete_high_dominance_2x_batches(
        request: Request,
        payload: dict = Body(...),
        session: Session = Depends(_session),
    ):
        origin = request.headers.get("origin")
        if not origin or origin not in origins:
            raise HTTPException(status_code=403, detail={
                "code": "HIGH_DOMINANCE_DELETE_ORIGIN_FORBIDDEN",
                "message": "Batch deletion is accepted only from a configured Behaviour Detection application origin.",
            })
        phase = payload.get("phase")
        delete_all = payload.get("delete_all") is True
        batch_ids = payload.get("batch_ids")
        if phase not in {"INITIAL", "CONTINUATION"}:
            raise HTTPException(status_code=422, detail={
                "code": "HIGH_DOMINANCE_DELETE_PHASE_INVALID",
                "message": "Choose Part 1 INITIAL or Part 2 CONTINUATION.",
            })
        if delete_all:
            if batch_ids not in (None, []):
                raise HTTPException(status_code=422, detail={
                    "code": "HIGH_DOMINANCE_DELETE_SELECTION_INVALID",
                    "message": "Delete all cannot be combined with selected batch IDs.",
                })
        elif (not isinstance(batch_ids, list) or not batch_ids
              or len(batch_ids) > 500
              or not all(isinstance(batch_id, str) and 3 <= len(batch_id) <= 180
                         for batch_id in batch_ids)):
            raise HTTPException(status_code=422, detail={
                "code": "HIGH_DOMINANCE_DELETE_SELECTION_INVALID",
                "message": "Select between 1 and 500 valid batch IDs, or use delete_all.",
            })
        elif len(set(batch_ids)) != len(batch_ids):
            raise HTTPException(status_code=422, detail={
                "code": "HIGH_DOMINANCE_DELETE_SELECTION_DUPLICATE",
                "message": "A batch was selected more than once.",
            })

        cohort = session.get(HybridCohort, 1)
        if cohort is None:
            raise HTTPException(status_code=404, detail={
                "code": "HIGH_DOMINANCE_COHORT_NOT_FOUND",
                "message": "The prospective Behaviour cohort is not available.",
            })
        latest_anchor = session.scalar(select(func.max(
            HighDominance2xBatch.series_anchor_position
        )).where(HighDominance2xBatch.cohort_id == cohort.cohort_id))
        if delete_all:
            criteria = [HighDominance2xBatch.cohort_id == cohort.cohort_id,
                        HighDominance2xBatch.phase == phase]
            if latest_anchor is not None:
                criteria.append(HighDominance2xBatch.series_anchor_position != latest_anchor)
            selected = session.scalars(select(HighDominance2xBatch).where(*criteria)).all()
        else:
            selected = session.scalars(select(HighDominance2xBatch).where(
                HighDominance2xBatch.cohort_id == cohort.cohort_id,
                HighDominance2xBatch.batch_id.in_(batch_ids),
            )).all()
            if len(selected) != len(batch_ids):
                raise HTTPException(status_code=404, detail={
                    "code": "HIGH_DOMINANCE_BATCH_NOT_FOUND",
                    "message": "One or more selected batches are not in the current Behaviour cohort.",
                })
            if any(batch.phase != phase for batch in selected):
                raise HTTPException(status_code=422, detail={
                    "code": "HIGH_DOMINANCE_DELETE_PHASE_MISMATCH",
                    "message": "Selected batches must all belong to the chosen Part.",
                })

        if not selected:
            return {"deleted_count": 0, "phase": phase,
                    "remaining_summary": _high_dominance_2x_payload(session, cohort)["summary"]}
        if latest_anchor is not None and any(
                batch.series_anchor_position == latest_anchor for batch in selected):
            raise HTTPException(status_code=409, detail={
                "code": "HIGH_DOMINANCE_CURRENT_SERIES",
                "message": "The newest audit series is protected because it tracks live HIGH transitions.",
            })
        if any(batch.lifecycle_status == "COLLECTING" for batch in selected):
            raise HTTPException(status_code=409, detail={
                "code": "HIGH_DOMINANCE_SERIES_ACTIVE",
                "message": "Live batches cannot be deleted.",
            })
        if phase == "INITIAL":
            selected_anchors = {batch.series_anchor_position for batch in selected}
            dependent_ids = session.scalars(select(HighDominance2xBatch.batch_id).where(
                HighDominance2xBatch.cohort_id == cohort.cohort_id,
                HighDominance2xBatch.series_anchor_position.in_(selected_anchors),
                HighDominance2xBatch.phase == "CONTINUATION",
            )).all()
            if dependent_ids:
                raise HTTPException(status_code=409, detail={
                    "code": "HIGH_DOMINANCE_CONTINUATION_EXISTS",
                    "message": "Delete Part 2 continuation batches in these series first, then delete Part 1.",
                    "dependent_continuation_count": len(dependent_ids),
                })

        selected_ids = [batch.batch_id for batch in selected]
        deleted = session.execute(sql_delete(HighDominance2xBatch).where(
            HighDominance2xBatch.cohort_id == cohort.cohort_id,
            HighDominance2xBatch.batch_id.in_(selected_ids),
        )).rowcount or 0
        session.commit()
        session.expire_all()
        return {"deleted_count": int(deleted), "phase": phase,
                "remaining_summary": _high_dominance_2x_payload(session, cohort)["summary"],
                "remaining_phase_summaries": _high_dominance_2x_payload(session, cohort)["phase_summaries"]}

    @app.delete("/api/v1/behaviour/hybrid/high-dominance-2x/{batch_id}", tags=["behaviour"])
    def delete_high_dominance_2x_batch(
        request: Request,
        batch_id: str = Path(..., min_length=3, max_length=180),
        session: Session = Depends(_session),
    ):
        origin = request.headers.get("origin")
        if not origin or origin not in origins:
            raise HTTPException(status_code=403, detail={
                "code": "HIGH_DOMINANCE_DELETE_ORIGIN_FORBIDDEN",
                "message": "Batch deletion is accepted only from a configured Behaviour Detection application origin.",
            })
        cohort = session.get(HybridCohort, 1)
        batch = session.get(HighDominance2xBatch, batch_id)
        if cohort is None or batch is None or batch.cohort_id != cohort.cohort_id:
            raise HTTPException(status_code=404, detail={
                "code": "HIGH_DOMINANCE_BATCH_NOT_FOUND",
                "message": "This saved 2x batch does not exist in the current Behaviour cohort.",
            })
        active_in_series = session.scalar(select(func.count()).select_from(
            HighDominance2xBatch
        ).where(
            HighDominance2xBatch.cohort_id == cohort.cohort_id,
            HighDominance2xBatch.series_anchor_position == batch.series_anchor_position,
            HighDominance2xBatch.lifecycle_status == "COLLECTING",
        )) or 0
        if active_in_series:
            raise HTTPException(status_code=409, detail={
                "code": "HIGH_DOMINANCE_SERIES_ACTIVE",
                "message": "Batches in a live series cannot be deleted. Wait for the series to finish.",
            })
        latest_series_anchor = session.scalar(
            select(func.max(HighDominance2xBatch.series_anchor_position)).where(
                HighDominance2xBatch.cohort_id == cohort.cohort_id
            )
        )
        if batch.series_anchor_position == latest_series_anchor:
            raise HTTPException(status_code=409, detail={
                "code": "HIGH_DOMINANCE_CURRENT_SERIES",
                "message": "The newest audit series is still used to track live HIGH transitions and cannot be deleted. It will move into history when a later series opens.",
            })
        if batch.phase == "INITIAL":
            continuation_count = session.scalar(select(func.count()).select_from(
                HighDominance2xBatch
            ).where(
                HighDominance2xBatch.cohort_id == cohort.cohort_id,
                HighDominance2xBatch.series_anchor_position == batch.series_anchor_position,
                HighDominance2xBatch.phase == "CONTINUATION",
            )) or 0
            if continuation_count:
                raise HTTPException(status_code=409, detail={
                    "code": "HIGH_DOMINANCE_CONTINUATION_EXISTS",
                    "message": "Delete Part 2 continuation batches before deleting this Part 1 batch.",
                })
        session.delete(batch)
        session.commit()
        return {"deleted": True, "batch_id": batch_id,
                "remaining_summary": _high_dominance_2x_payload(session, cohort)["summary"]}

    @app.get("/api/v1/behaviour/hybrid/windows", tags=["behaviour"])
    def hybrid_windows(limit: int = Query(default=20, ge=1, le=100),
                       session: Session = Depends(_session)):
        payload = _hybrid_payload(session, window_limit=limit)
        return {"cohort": payload.get("cohort"), "windows": payload.get("windows", []),
                "policy_version": payload.get("policy_version"),
                "available": payload.get("available", False)}

    @app.get("/api/v1/behaviour/rounds", tags=["history"])
    def rounds(limit: int = Query(100, ge=1, le=1000), before_position: int | None = Query(None, ge=1),
               session: Session = Depends(_session)):
        query = select(CanonicalRound)
        if before_position is not None:
            query = query.where(CanonicalRound.analysis_position < before_position)
        rows = session.scalars(query.order_by(CanonicalRound.analysis_position.desc()).limit(limit)).all()
        return {"items": [{"identity_key": r.identity_key, "round_id": r.round_id,
                           "analysis_position": r.analysis_position,
                           "source_round_index": r.source_round_index,
                           "multiplier": r.multiplier_raw, "outcome": r.outcome,
                           "event_timestamp": _timestamp(r.event_timestamp),
                           "ingestion_timestamp": _timestamp(r.ingestion_timestamp),
                           "source": r.source_name, "timestamp_quality": r.timestamp_quality,
                           "order_quality": r.order_quality} for r in rows],
                "next_before_position": rows[-1].analysis_position if rows else None}

    @app.get("/api/v1/behaviour/distribution", tags=["behaviour"])
    def distribution(session: Session = Depends(_session)):
        stats = session.get(CumulativeStats, "global")
        snapshot = _latest_snapshot(session)
        if stats is None:
            raise HTTPException(status_code=503, detail={"code": "PHASE1_IMPORT_REQUIRED"})
        global_snapshot = session.scalar(select(DistributionSnapshot).where(
            DistributionSnapshot.horizon == "global").order_by(DistributionSnapshot.id).limit(1))
        n = max(1, stats.round_count)
        log_mean = float(stats.log_multiplier_sum / n)
        log_variance = max(0.0, float(stats.log_multiplier_square_sum / n) - log_mean ** 2)
        if stats.round_count > 1:
            log_variance *= stats.round_count / (stats.round_count - 1)
        tails = stats.tail_counts or {}
        cumulative_summary = {
            "round_count": stats.round_count,
            "mean_multiplier": float(stats.multiplier_sum / n),
            "minimum_multiplier": str(stats.minimum_multiplier),
            "maximum_multiplier": str(stats.maximum_multiplier),
            "log_mean": log_mean, "log_volatility_sample": log_variance ** 0.5,
            "tail_counts": tails,
            "tail_rates": {key: value / n for key, value in tails.items()},
            "quantiles_scope": "Frozen Phase 1 quantiles are shown separately; current quantiles are in rolling windows.",
        }
        return {"counts": {"H": stats.high_count, "L": stats.low_count, "E": stats.exact_count},
                "rates_all": {"H": stats.high_count / max(1, stats.round_count),
                              "L": stats.low_count / max(1, stats.round_count),
                              "E": stats.exact_count / max(1, stats.round_count)},
                "high_rate_nonexact": stats.high_count / max(1, stats.high_count + stats.low_count),
                "round_count": stats.round_count,
                "phase1_snapshot_distribution": global_snapshot.statistics if global_snapshot else None,
                "cumulative_summary": cumulative_summary,
                "rolling_windows": (snapshot.features or {}).get("rolling_windows", {}) if snapshot else {}}

    @app.get("/api/v1/behaviour/time-groups", tags=["time"])
    def time_groups(limit: int = Query(200, ge=1, le=2000), offset: int = Query(0, ge=0),
                    session: Session = Depends(_session)):
        total = session.scalar(select(func.count()).select_from(TimeGroup)) or 0
        rows = session.scalars(select(TimeGroup).order_by(TimeGroup.first_position)
                               .offset(offset).limit(limit)).all()
        return {"total": total, "items": [{"id": r.id, "first_position": r.first_position,
                                             "last_position": r.last_position,
                                             "start_at": _timestamp(r.start_at), "end_at": _timestamp(r.end_at),
                                             "duration_seconds": r.duration_seconds,
                                             "gap_threshold_seconds": float(r.threshold_seconds),
                                             "round_count": r.round_count, "statistics": r.stats,
                                             "timestamp_quality": r.timestamp_quality} for r in rows]}

    @app.get("/api/v1/behaviour/state", tags=["behaviour"])
    def state(session: Session = Depends(_session)):
        history = session.scalars(select(BehaviourStateHistory).order_by(
            BehaviourStateHistory.id.desc()).limit(1000)).all()
        latest = history[0] if history else None
        duration_rounds = 0
        if latest:
            for entry in history:
                if entry.confirmed_state != latest.confirmed_state:
                    break
                duration_rounds += 1
        snapshot = _latest_snapshot(session)
        imported = session.scalar(select(ResearchImport).order_by(ResearchImport.snapshot_utc.desc()).limit(1))
        live = _live_context(session, imported)
        return {"observed_state": live.get("current_state") if live else (latest.observed_state if latest else "UNCERTAIN"),
                "confirmed_state": latest.confirmed_state if latest else (live.get("current_state") if live else "UNCERTAIN"),
                "duration_rounds": duration_rounds,
                "generated_at": _timestamp(latest.generated_at) if latest else None,
                "evidence": live.get("state_evidence") if live else (latest.evidence if latest else {}),
                "rolling_windows": live.get("rolling_windows", {}) if live else ((snapshot.features or {}).get("rolling_windows", {}) if snapshot else {}),
                "live_context": live}

    @app.get("/api/v1/behaviour/next100", tags=["forecast"])
    def next100(session: Session = Depends(_session)):
        row = session.scalar(select(Next100Assessment).order_by(Next100Assessment.id.desc()).limit(1))
        assessment = _assessment(row) or {"data_sufficiency": "INSUFFICIENT_HISTORY"}
        imported = session.scalar(select(ResearchImport).order_by(ResearchImport.snapshot_utc.desc()).limit(1))
        live = _live_context(session, imported)
        if live:
            explanation = dict(assessment.get("explanation") or {})
            explanation["live_context"] = live
            explanation["historical_recovery_comparison"] = live["historical_recovery_comparison"]
            assessment.update({"live_context": live,
                               "historical_recovery_comparison": live["historical_recovery_comparison"],
                               "explanation": explanation})
        return assessment

    @app.get("/api/v1/behaviour/sequences", tags=["sequence"])
    def sequences(session: Session = Depends(_session)):
        snapshot = _latest_snapshot(session)
        outlook = session.scalar(select(SequenceOutlookResult).order_by(SequenceOutlookResult.id.desc()).limit(1))
        motifs = session.scalars(select(DiscoveredMotif).order_by(DiscoveredMotif.length, DiscoveredMotif.motif)).all()
        imported = session.scalar(select(ResearchImport).order_by(ResearchImport.snapshot_utc.desc()).limit(1))
        live = _live_context(session, imported)
        return {"current": live.get("sequence", {}) if live else ((snapshot.features or {}).get("sequence", {}) if snapshot else {}),
                "rolling_windows": live.get("rolling_windows", {}) if live else ((snapshot.features or {}).get("rolling_windows", {}) if snapshot else {}),
                "recent_sequence_hle": live.get("recent_sequence_hle", "") if live else "",
                "live_context": live,
                "motif_counts_last_500": (snapshot.features or {}).get("motifs", {}) if snapshot else {},
                "historical_motifs": [{"motif": m.motif, "length": m.length,
                                       "count": m.historical_count, "relative_frequency": m.relative_frequency,
                                       "shuffle_p_value": m.shuffle_p_value, "bh_q_value": m.bh_q_value,
                                       "evidence_status": m.evidence_status,
                                       "predictive": False} for m in motifs],
                "outlook": outlook.outlook if outlook else {},
                "outlook_evidence_status": outlook.evidence_status if outlook else "INSUFFICIENT_HISTORY"}

    @app.get("/api/v1/behaviour/motifs/{motif}", tags=["sequence"])
    def motif_detail(motif: str = Path(..., min_length=2, max_length=8, pattern="^[HL]{2,8}$"),
                     session: Session = Depends(_session)):
        row = session.get(DiscoveredMotif, motif)
        if row is None:
            raise HTTPException(status_code=404, detail={"code": "MOTIF_NOT_FOUND"})
        details = session.get(MotifResearchEvidence, (motif, "phase1-2026-11-13T083013Z"))
        return {"motif": row.motif, "length": row.length, "count": row.historical_count,
                "relative_frequency": row.relative_frequency,
                "shuffle_p_value": row.shuffle_p_value, "bh_q_value": row.bh_q_value,
                "evidence_status": row.evidence_status,
                "research_details": details.details if details else {}}

    @app.get("/api/v1/behaviour/rules", tags=["rules"])
    def rules(session: Session = Depends(_session)):
        definitions = session.scalars(select(RuleDefinition).order_by(RuleDefinition.category,
                                                                     RuleDefinition.rule_id,
                                                                     RuleDefinition.version)).all()
        latest_rule_evaluation = (
            select(RuleEvaluation.rule_id, RuleEvaluation.rule_version,
                   func.max(RuleEvaluation.id).label("latest_id"))
            .group_by(RuleEvaluation.rule_id, RuleEvaluation.rule_version)
            .subquery()
        )
        evaluations = session.scalars(
            select(RuleEvaluation).join(
                latest_rule_evaluation, RuleEvaluation.id == latest_rule_evaluation.c.latest_id
            )
        ).all()
        evaluations_by_rule = {(row.rule_id, row.rule_version): row for row in evaluations}
        output = []
        for rule in definitions:
            evaluation = evaluations_by_rule.get((rule.rule_id, rule.version))
            output.append({"rule_id": rule.rule_id, "version": rule.version,
                           "name": rule.name, "category": rule.category,
                           "description": rule.description, "formula": rule.formula,
                           "input_features": rule.input_features, "thresholds": rule.thresholds,
                           "minimum_evidence": rule.minimum_evidence,
                           "evaluation_frequency": rule.evaluation_frequency,
                           "validation_status": rule.validation_status, "enabled": rule.enabled,
                           "latest_evaluation": ({"evaluated_at": _timestamp(evaluation.evaluated_at),
                                                 "result": evaluation.result,
                                                 "evidence_class": evaluation.evidence_class,
                                                 "explanation": evaluation.explanation} if evaluation else None)})
        return {"items": output}

    @app.get("/api/v1/behaviour/forecasts", tags=["forecast"])
    def forecasts(limit: int = Query(200, ge=1, le=2000), session: Session = Depends(_session)):
        rows = session.scalars(select(ForecastHistory).order_by(ForecastHistory.id.desc()).limit(limit)).all()
        return {"items": [{"id": r.id, "assessed_at": _timestamp(r.assessed_at),
                           "latest_round": r.latest_identity_key, "latest_position": r.latest_position,
                           "type": r.forecast_type, "forecast": r.forecast,
                           "change_from_previous": r.change_from_previous} for r in rows]}

    @app.get("/api/v1/behaviour/evaluations", tags=["forecast"])
    def evaluations(limit: int = Query(200, ge=1, le=2000), session: Session = Depends(_session)):
        rows = session.scalars(select(ForecastEvaluationResult)
                               .order_by(ForecastEvaluationResult.id.desc()).limit(limit)).all()
        return {"items": [{"id": r.id, "forecast_id": r.forecast_id,
                           "evaluated_at": _timestamp(r.evaluated_at),
                           "horizon_end_position": r.horizon_end_position,
                           "outcome": r.outcome, "metrics": r.metrics} for r in rows]}

    @app.get("/api/v1/behaviour/research", tags=["research"])
    def research(session: Session = Depends(_session)):
        items = session.scalars(select(ResearchFinding).where(
            ResearchFinding.snapshot_id == "phase1-2026-11-13T083013Z").order_by(ResearchFinding.finding_key)).all()
        return {"snapshot_id": "phase1-2026-11-13T083013Z",
                "items": {item.finding_key: item.payload for item in items}}

    @app.get("/api/v1/behaviour/collector", tags=["operations"])
    def collector_status(session: Session = Depends(_session)):
        return {"health": _latest_health(session),
                "latest_readings": [{"watch_id": r.watch_id, "reading_id": r.reading_id,
                                     "checked_at": _timestamp(r.checked_at),
                                     "received_at": _timestamp(r.received_at),
                                     "status": r.status, "validation_error": r.validation_error}
                                    for r in session.scalars(select(OrbitReading)
                                                             .order_by(OrbitReading.id.desc()).limit(20)).all()],
                "recent_errors": [{"component": e.component, "error_code": e.error_code,
                                   "occurred_at": _timestamp(e.occurred_at), "details": e.details}
                                  for e in session.scalars(select(ApiError)
                                                           .order_by(ApiError.id.desc()).limit(50)).all()],
                "recent_activity": [{"event_type": e.event_type, "occurred_at": _timestamp(e.occurred_at),
                                     "details": e.details}
                                    for e in session.scalars(select(CollectorActivity)
                                                             .order_by(CollectorActivity.id.desc()).limit(50)).all()]}

    @app.get("/api/v1/behaviour/health", tags=["health"])
    def health(session: Session = Depends(_session)):
        counts = {
            "canonical_rounds": session.scalar(select(func.count()).select_from(CanonicalRound)) or 0,
            "source_records": session.scalar(select(func.count()).select_from(SourceRecord)) or 0,
            "orbit_readings": session.scalar(select(func.count()).select_from(OrbitReading)) or 0,
            "quarantined_readings": session.scalar(select(func.count()).select_from(OrbitReading).where(OrbitReading.status.like("quarantined%"))) or 0,
        }
        return {"service": "advanced-crash-behaviour", "version": __version__,
                "database": "connected", "counts": counts, "components": _latest_health(session)}

    return app


app = create_app()
