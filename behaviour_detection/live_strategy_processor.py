"""Bounded, ordered live strategy reducer for verified Orbit rounds.

The source API has already validated and durably staged each event before this
processor runs. This reducer does no PostgreSQL work or historical scan per
event; its small state snapshot is committed to the Behaviour local store.
"""
from __future__ import annotations

from collections import deque
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
import threading
import time
import logging

from sqlalchemy import inspect, select, text
from sqlalchemy.orm import Session

from .high_series import _settle, new_state, prepare_strategy_eligibility, process_round
from .models import (BehaviourApiRound, CanonicalRound, HighDominanceSeriesState,
                     HybridCohort, HybridCohortRound)

log = logging.getLogger("behaviour.live_strategy")


def _dominance(outcomes: list[str]) -> str:
    high = outcomes.count("H")
    low = outcomes.count("L")
    exact = outcomes.count("E")
    nonexact = high + low
    if len(outcomes) < 5 or nonexact < 4 or not nonexact:
        return "INSUFFICIENT_DATA"
    share = high / nonexact
    if share >= .60:
        return "HIGH_DOMINANT"
    if share <= .40:
        return "LOW_DOMINANT"
    return "BALANCED"


def _classification(multiplier: Decimal | str | float) -> str:
    value = Decimal(str(multiplier))
    return "H" if value > Decimal("2.00") else "L" if value < Decimal("2.00") else "E"


def hydrate_live_journal_baseline(engine) -> dict:
    """Seed descriptive context from committed rows already in the shared journal.

    This runs only at startup. The seeded snapshot is historical baseline data;
    it never advances a strategy series or creates eligibility.
    """
    if engine.dialect.name != "sqlite" or not inspect(engine).has_table("round_observation_inbox"):
        return {"status": "NO_SHARED_INGRESS_JOURNAL"}
    with engine.connect() as connection:
        rows = connection.execute(text("""
            SELECT event_json FROM round_observation_inbox
            WHERE status='COMMITTED'
            ORDER BY inbox_sequence DESC LIMIT 100
        """)).fetchall()
    events = []
    for row in rows:
        try:
            event = json.loads(row[0])
            sequence = int(event.get("round_index"))
            multiplier = Decimal(str(event.get("multiplier")))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        round_id = str(event.get("round_uid") or event.get("round_id") or "")
        if (event.get("event_type") != "round.observed"
                or event.get("continuity_status") != "VERIFIED"
                or str(event.get("delivery_mode") or "").upper() != "LIVE"
                or round_id != str(event.get("round_id") or "")
                or sequence < 1 or not multiplier.is_finite() or multiplier < 1):
            continue
        events.append({**event, "round_uid": round_id, "round_index": sequence,
                       "multiplier": multiplier})
    if not events:
        return {"status": "NO_COMMITTED_VERIFIED_ROUND"}

    latest = events[0]
    contiguous = [latest]
    for event in events[1:]:
        if (event.get("stream_id") != latest.get("stream_id")
                or int(event["round_index"]) != int(contiguous[-1]["round_index"]) - 1):
            break
        contiguous.append(event)
        if len(contiguous) == 5:
            break
    recent = list(reversed(contiguous))
    outcomes = ["H" if item["multiplier"] > 2 else
                "L" if item["multiplier"] < 2 else "E" for item in recent]
    round_id = str(latest["round_uid"])
    identity = f"winner-predict:{round_id}"
    now = datetime.now(timezone.utc)

    def timestamp(value):
        if not value:
            return None
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)

    event_time = timestamp(latest.get("platform_timestamp"))
    observed_time = timestamp(latest.get("observed_at"))
    with Session(engine, expire_on_commit=False) as session, session.begin():
        cohort = session.get(HybridCohort, 1)
        if cohort is None:
            return {"status": "COHORT_UNAVAILABLE"}
        canonical = session.get(CanonicalRound, identity)
        if canonical is None:
            position = int(session.scalar(select(CanonicalRound.analysis_position)
                .order_by(CanonicalRound.analysis_position.desc()).limit(1)) or 0) + 1
            session.add(CanonicalRound(
                identity_key=identity, round_id=round_id,
                source_round_index=str(latest["round_index"]), source_sequence=int(latest["round_index"]),
                analysis_position=position, multiplier_raw=str(latest["multiplier"]),
                multiplier_value=latest["multiplier"],
                outcome="H" if latest["multiplier"] > 2 else
                        "L" if latest["multiplier"] < 2 else "E",
                event_timestamp=event_time, source_observed_at=observed_time,
                ingestion_timestamp=timestamp(latest.get("stored_at")),
                source_name="winner_predict_database",
                timestamp_quality=("platform_event_time" if event_time else
                                   "observation_time_only" if observed_time else "source_time_unavailable"),
                order_quality="winner_predict_canonical_order_verified",
                import_snapshot_id=None, inserted_at=now))
        state_row = session.get(HighDominanceSeriesState, cohort.cohort_id)
        if state_row is None:
            machine = new_state()
            machine.update({
                "state": "ARMED_FOR_HIGH", "provenance": "HISTORICAL_HYDRATION",
                "data_health": "HEALTHY", "live_rolling_outcomes": outcomes,
                "last_dominance_state": _dominance(outcomes),
                "historical_baseline_round_id": round_id,
                "historical_baseline_round_identity": identity,
                "historical_baseline_round_index": int(latest["round_index"]),
                "historical_baseline_stream_id": str(latest.get("stream_id") or "") or None,
                "last_processed_position": int(cohort.last_cohort_position or 0),
            })
            session.add(HighDominanceSeriesState(cohort_id=cohort.cohort_id,
                payload=machine, updated_at=now))
    return {"status": "SEEDED_HISTORICAL_BASELINE", "round_id": round_id,
            "round_index": int(latest["round_index"]), "rolling_samples": len(outcomes)}


class LiveStrategyProcessor:
    """Serialize live rounds and persist an idempotent strategy checkpoint."""

    def __init__(self, engine):
        self.engine = engine
        self._lock = threading.RLock()
        self._outcomes: deque[str] = deque(maxlen=5)
        self._last_sequence = 0
        self._position = 0
        self._last_identity: str | None = None
        self._stream_id: str | None = None
        self._state = "WAITING_FOR_LIVE"
        self._latest_live_round_id: str | None = None
        self._latest_live_identity: str | None = None
        self._latest_live_processed_at: str | None = None
        self._series_state: str | None = None
        self._provenance: str | None = None
        self._latest_eligibility: dict | None = None
        self._latencies_ms: deque[float] = deque(maxlen=1000)
        self._stage_latencies_ms: dict[str, deque[float]] = {}
        self._hydrate()

    def record_stage_latency(self, stage: str, elapsed_ms: float) -> None:
        """Retain bounded same-process stage measurements for live status."""
        try:
            value = float(elapsed_ms)
        except (TypeError, ValueError, OverflowError):
            return
        if not value >= 0 or not value < float("inf"):
            return
        with self._lock:
            samples = self._stage_latencies_ms.setdefault(stage, deque(maxlen=1000))
            samples.append(value)

    def _hydrate(self) -> None:
        with Session(self.engine) as session:
            cohort = session.get(HybridCohort, 1)
            if cohort is None:
                return
            machine_row = session.get(HighDominanceSeriesState, cohort.cohort_id)
            machine = dict(machine_row.payload or {}) if machine_row else new_state()
            self._position = max(int(cohort.last_cohort_position or 0),
                                 int(machine.get("last_processed_position") or 0))
            self._last_identity = (machine.get("last_processed_round_identity")
                                   or machine.get("last_live_round_identity")
                                   or machine.get("historical_baseline_round_identity"))
            self._stream_id = (machine.get("last_processed_stream_id")
                               or machine.get("last_live_stream_id")
                               or machine.get("historical_baseline_stream_id"))
            self._last_sequence = int(machine.get("last_processed_round_index")
                                      or machine.get("last_live_round_index")
                                      or machine.get("historical_baseline_round_index") or 0)
            # The async reporting mirror may be ahead of the locally verified
            # event log, or contain holes. Its maximum sequence is not a live
            # decision cursor and must never suppress an Orbit event.
            values = list(machine.get("live_rolling_outcomes") or [])[-5:]
            if not values:
                rows = session.execute(
                    select(CanonicalRound.outcome)
                    .join(HybridCohortRound,
                          HybridCohortRound.identity_key == CanonicalRound.identity_key)
                    .where(HybridCohortRound.cohort_id == cohort.cohort_id)
                    .order_by(HybridCohortRound.cohort_position.desc()).limit(5)
                ).all()
                values = [row[0] for row in reversed(rows)]
            self._outcomes.extend(value for value in values if value in {"H", "L", "E"})
            self._latest_live_round_id = machine.get("last_live_round_id")
            self._latest_live_identity = machine.get("last_live_round_identity")
            self._latest_live_processed_at = machine.get("last_live_round_processed_at")
            self._series_state = machine.get("state")
            self._provenance = machine.get("provenance")
            latest = machine.get("latest_strategy_eligibility")
            self._latest_eligibility = dict(latest) if isinstance(latest, dict) else None
            self._state = ("UNSAFE_STATE" if machine.get("state") == "UNSAFE_STATE"
                           else "LIVE_READY" if machine.get("provenance") == "LIVE_VERIFIED"
                           else "WAITING_FOR_LIVE")

    @staticmethod
    def _trusted_live_trigger(session: Session, machine: dict, stream_id: str) -> bool:
        """Prove a restored active series began on an authenticated live event."""
        series = machine.get("series") or {}
        trigger_id = str(series.get("trigger_round_id") or "")
        trigger_identity = str(series.get("trigger_round_identity") or "")
        if (not trigger_id or trigger_identity != f"winner-predict:{trigger_id}"
                or not any(event.get("type") in {"PART1_STARTED", "HIGH_EMERGENCE_DETECTED"}
                           and event.get("round_identity") == trigger_identity
                           for event in machine.get("events", []) if isinstance(event, dict))):
            return False
        source_round = session.get(BehaviourApiRound, f"{stream_id}:{trigger_id}")
        if source_round is None or source_round.stream_id != stream_id:
            return False
        payload = source_round.payload if isinstance(source_round.payload, dict) else {}
        metadata = payload.get("source_metadata") if isinstance(payload.get("source_metadata"), dict) else {}
        return bool(source_round.status in {"PROMOTED", "RECONCILED"}
            and source_round.round_id == trigger_id
            and int(series.get("trigger_position") or 0) > 0
            and payload.get("event_type") == "round.observed"
            and payload.get("durability") == "PENDING"
            and str(payload.get("round_uid") or payload.get("round_id") or "") == trigger_id
            and int(payload.get("round_index") or 0) == int(source_round.round_index)
            and Decimal(str(payload.get("multiplier"))) == Decimal(source_round.multiplier_raw)
            and payload.get("behaviour_provenance") == "LIVE_VERIFIED"
            and str(payload.get("delivery_mode") or "").upper() == "LIVE"
            and payload.get("continuity_status") == "VERIFIED"
            and metadata.get("source") == "ORBIT_DATA_WATCH_API"
            and metadata.get("reading_id") and metadata.get("reading_checked_at")
            and metadata.get("collector_received_at"))

    @staticmethod
    def _durable_signal_for_target(session: Session, stream_id: str,
                                   sequence: int) -> dict | None:
        """Read the accepted target lock and its optional committed result."""
        if session.bind.dialect.name != "sqlite":
            return None
        target_id = f"shadow-index:{stream_id}:{int(sequence)}"
        try:
            row = session.execute(text("""SELECT signal_json FROM fast_signal_outbox
                WHERE target_id=:target_id ORDER BY rowid DESC LIMIT 1"""),
                {"target_id": target_id}).first()
        except Exception:
            return None
        if row is None:
            return None
        try:
            signal = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        accepted = bool(signal.get("signal_id") and signal.get("session_id")
            and str(signal.get("target_round_id") or "") == target_id
            and str(signal.get("source_id") or "") == "BEHAVIOUR_DETECTION"
            and str(signal.get("mode") or "").upper() == "SHADOW"
            and str(signal.get("distribution_status") or "").upper() == "ACCEPTED")
        if not accepted:
            return None
        settlement = signal.get("settlement")
        evidence = settlement.get("evidence") if isinstance(settlement, dict) else None
        try:
            settlement_index = int(settlement.get("target_round_index") or 0) if isinstance(settlement, dict) else 0
        except (TypeError, ValueError, OverflowError):
            settlement_index = 0
        confirmed = bool(signal.get("settlement_status") == "COMMITTED"
            and isinstance(settlement, dict) and settlement.get("status") == "COMMITTED"
            and str(settlement.get("result") or "").upper() in {"WIN", "LOSS"}
            and isinstance(evidence, dict) and evidence.get("event_type") == "round.persisted"
            and evidence.get("durability") == "COMMITTED"
            and evidence.get("continuity_status") == "VERIFIED"
            and (evidence.get("target_stream_id") or evidence.get("stream_id")) == stream_id
            and settlement_index == int(sequence))
        signal["confirmed_settlement"] = confirmed
        if confirmed:
            signal["confirmed_settlement_data"] = {
                "signal_id": str(signal.get("signal_id") or ""),
                "signal_lock_status": "DURABLY_LOCKED",
                "settlement_status": "COMMITTED",
                "target_round_identity": f"winner-predict:{settlement.get('target_round_id')}",
                "result": str(settlement.get("result") or "").upper(),
                "net_profit": settlement.get("profit_loss_rwf"),
            }
        return signal

    def reconcile_settlement(self, settlement: dict) -> dict:
        """Apply one durable SHADOW result and release only future eligibility."""
        signal_id = str(settlement.get("signal_id") or "")
        if not signal_id:
            return {"status": "REJECTED_IDENTITY"}
        with self._lock:
            with Session(self.engine, expire_on_commit=False) as session, session.begin():
                cohort = session.get(HybridCohort, 1)
                if cohort is None:
                    return {"status": "COHORT_UNAVAILABLE"}
                machine_row = session.get(HighDominanceSeriesState, cohort.cohort_id)
                if machine_row is None:
                    return {"status": "STATE_UNAVAILABLE"}
                machine = deepcopy(machine_row.payload or {})
                if signal_id in (machine.get("settlement_receipts") or {}):
                    return {"status": "ALREADY_COUNTED", "signal_id": signal_id}
                target_identity = str(settlement.get("target_round_identity") or "")
                wait = machine.get("settlement_wait") or {}
                context = machine.get("last_live_round_context") or {}
                deferred = list(machine.get("deferred_verified_rounds") or [])
                target_stream = str(settlement.get("target_stream_id") or "")
                try:
                    target_index = int(settlement.get("target_round_index") or 0)
                except (TypeError, ValueError, OverflowError):
                    target_index = 0
                waiting_match = (machine.get("state") == "WAITING_FOR_SHADOW_SETTLEMENT"
                    and str(wait.get("signal_id") or "") == signal_id
                    and str(wait.get("identity_key") or "") == target_identity)
                latest_match = (str(context.get("identity_key") or "") == target_identity
                                and str(context.get("series_id") or "") == str(settlement.get("series_id") or ""))
                if not waiting_match and not latest_match:
                    # Settlement feedback may beat the Behaviour round reducer
                    # because SHADOW commits from the persisted-round event.
                    # Keep the receipt retryable until the verified target has
                    # reached this checkpoint; do not quarantine a future target.
                    if (target_stream and target_stream == self._stream_id
                            and target_index > self._last_sequence):
                        return {"status": "WAITING_FOR_TARGET_ROUND", "signal_id": signal_id,
                                "target_round_index": target_index}

                    # Recover a receipt that arrived after a verified target
                    # but before its causal checkpoint was retained. Count only
                    # the durably locked settlement, then terminate this series
                    # into cooldown; skipped decisions are never replayed.
                    series = machine.get("series") or {}
                    locked = (machine.get("locked_signals") or {}).get(signal_id) or {}
                    part = str(settlement.get("part") or "")
                    batch_id = str(settlement.get("batch_id") or "")
                    source_row = session.get(BehaviourApiRound,
                        f"{target_stream}:{settlement.get('target_round_id')}")
                    source_payload = source_row.payload if source_row and isinstance(source_row.payload, dict) else {}
                    source_trusted = bool(source_row
                        and source_row.stream_id == target_stream
                        and int(source_row.round_index) == target_index
                        and source_payload.get("event_type") == "round.observed"
                        and source_payload.get("continuity_status") == "VERIFIED"
                        and source_payload.get("durability") in {"PENDING", "COMMITTED"}
                        and str(source_payload.get("delivery_mode") or "").upper() == "LIVE")
                    current_counter = None
                    if part == "PART1" and batch_id == f"PART1-001":
                        current_counter = (series.get("part1") if series.get("current_part") == "PART1" else None)
                    elif part == "PART2":
                        part2 = series.get("part2") or {}
                        current_counter = part2.get("current_batch") if part2.get("current_batch_id") == batch_id else None
                    expected_result = None
                    if source_row is not None:
                        try:
                            expected_result = ("WIN" if Decimal(str(source_row.multiplier_raw)) >= Decimal("2.00")
                                               else "LOSS")
                        except Exception:
                            expected_result = None
                    existing_target = any(
                        str(item.get("identity_key") or "") == target_identity
                        or str(item.get("signal_id") or "") == signal_id
                        for container in (series.get("part1"),
                                          (series.get("part2") or {}).get("current_batch"))
                        if isinstance(container, dict)
                        for item in (container.get("rounds") or []))
                    receipt_result = str(settlement.get("result") or "").upper()
                    recoverable = (machine.get("state") == "UNSAFE_STATE"
                        and machine.get("recovery_quarantine_reason") == "SHADOW_SETTLEMENT_OUT_OF_ORDER"
                        and series.get("series_id") == settlement.get("series_id")
                        and locked.get("lock_status") in {"DURABLY_LOCKED", "EXISTING_DURABLE_SIGNAL"}
                        and locked.get("series_id") == settlement.get("series_id")
                        and locked.get("part") == part and locked.get("batch_id") == batch_id
                        and locked.get("target_stream_id") == target_stream
                        and int(locked.get("target_round_index") or 0) == target_index
                        and source_trusted and current_counter is not None
                        and not existing_target and expected_result == receipt_result)
                    if recoverable:
                        row = {"identity_key": target_identity,
                               "round_id": source_row.round_id,
                               "position": target_index,
                               "multiplier": str(source_row.multiplier_raw),
                               "outcome": _classification(Decimal(str(source_row.multiplier_raw))),
                               "timestamp": (source_row.observed_at.isoformat() if source_row.observed_at else "")}
                        counted = _settle(current_counter, row, signal_executed=True,
                            signal_settlement={**settlement,
                                "signal_lock_status": str(settlement.get("signal_lock_status") or "DURABLY_LOCKED"),
                                "settlement_status": "COMMITTED",
                                "target_round_identity": target_identity})
                        if counted:
                            at = datetime.now(timezone.utc)
                            machine.setdefault("settlement_receipts", {})[signal_id] = {
                                "result": receipt_result, "series_id": series.get("series_id"),
                                "part": part, "target_round_identity": target_identity,
                                "received_at": at.isoformat(),
                                "reconciled_after_ordering_gap": True}
                            locked["settlement_status"] = "COMMITTED"
                            machine.setdefault("completed_series", []).append({**series,
                                "stop_reason": "SETTLEMENT_RECONCILED_AFTER_ORDERING_GAP",
                                "terminal_state": "COOLDOWN", "terminal_at": at.isoformat(),
                                "terminal_round_identity": target_identity})
                            machine["completed_series"] = machine["completed_series"][-100:]
                            machine["series"] = None
                            machine["state"] = "COOLDOWN"
                            machine["cooldown_progress"] = 0
                            machine["cooldown_started_at"] = at.isoformat()
                            machine["fresh_high_exit_seen"] = False
                            machine["provenance"] = "LIVE_VERIFIED"
                            machine["data_health"] = "HEALTHY"
                            machine.pop("recovery_quarantine_reason", None)
                            machine.pop("settlement_wait", None)
                            machine.pop("deferred_verified_rounds", None)
                            machine.setdefault("events", []).append({
                                "type": "SETTLEMENT_RECONCILED_AFTER_ORDERING_GAP",
                                "signal_id": signal_id, "round_identity": target_identity,
                                "result": receipt_result, "at": at.isoformat()})
                            machine_row.payload = machine
                            machine_row.updated_at = at
                            self._series_state = "COOLDOWN"
                            self._provenance = "LIVE_VERIFIED"
                            self._state = "LIVE_READY"
                            self._latest_eligibility = dict(machine.get("latest_strategy_eligibility") or {})
                            return {"status": "RECONCILED_AFTER_ORDERING_GAP",
                                    "signal_id": signal_id, "state": "COOLDOWN"}
                    machine["state"] = "UNSAFE_STATE"
                    machine["data_health"] = "DATA_STALE"
                    machine["recovery_quarantine_reason"] = "SHADOW_SETTLEMENT_OUT_OF_ORDER"
                    machine.setdefault("events", []).append({
                        "type": "SHADOW_SETTLEMENT_REQUIRES_RECONCILIATION",
                        "round_identity": target_identity,
                        "round_id": settlement.get("target_round_id"),
                        "position": settlement.get("target_round_index"),
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    })
                    machine["events"] = machine["events"][-500:]
                    machine_row.payload = machine
                    machine_row.updated_at = datetime.now(timezone.utc)
                    return {"status": "QUARANTINED_OUT_OF_ORDER", "signal_id": signal_id}

                if waiting_match:
                    round_context = wait
                    machine["state"] = str(machine.pop("settlement_resume_state", "PART1_COLLECTING"))
                    machine["data_health"] = "HEALTHY"
                    machine["last_processed_identity"] = wait.get("previous_identity")
                    machine["last_processed_position"] = int(wait.get("previous_position") or 0)
                    row = deepcopy(wait.get("row") or {})
                    current_state = str(wait.get("current_state") or "INSUFFICIENT_DATA")
                    previous_state = str(wait.get("previous_state") or "INSUFFICIENT_DATA")
                    emergence = bool(wait.get("emergence"))
                    deferred_rows = deferred
                else:
                    round_context = context
                    machine["state"] = str(context.get("state_before") or machine.get("state"))
                    machine["data_health"] = "HEALTHY"
                    machine["last_processed_identity"] = context.get("previous_identity")
                    machine["last_processed_position"] = int(context.get("previous_position") or 0)
                    series = machine.get("series") or {}
                    for key, value in (context.get("series_terminal_snapshot") or {}).items():
                        if value is None:
                            series.pop(key, None)
                        else:
                            series[key] = deepcopy(value)
                    part2 = series.get("part2") or {}
                    for key, value in (context.get("part2_pause_snapshot") or {}).items():
                        part2[key] = deepcopy(value)
                    machine["cooldown_progress"] = int(context.get("cooldown_progress_before") or 0)
                    machine["cooldown_started_at"] = context.get("cooldown_started_at_before")
                    machine["fresh_high_exit_seen"] = bool(context.get("fresh_high_exit_seen_before"))
                    events = machine.setdefault("events", [])
                    event_count = max(0, int(context.get("events_length_before") or 0))
                    del events[event_count:]
                    row = {key: context[key] for key in (
                        "identity_key", "round_id", "position", "multiplier", "outcome", "timestamp")}
                    current_state = str(context.get("current_state") or "INSUFFICIENT_DATA")
                    previous_state = str(context.get("previous_state") or "INSUFFICIENT_DATA")
                    emergence = bool(context.get("emergence"))
                    deferred_rows = []

                if not row:
                    return {"status": "TARGET_CONTEXT_UNAVAILABLE", "signal_id": signal_id}
                settled_at = datetime.now(timezone.utc)
                machine = process_round(machine, row=row,
                    previous_state=previous_state, current_state=current_state,
                    emergence=emergence, now=settled_at, data_fresh=True,
                    signal_executed=True, signal_settlement=settlement,
                    allow_trigger=True)
                latest_row = row
                for item in deferred_rows:
                    latest_row = deepcopy(item.get("row") or latest_row)
                    machine = process_round(machine, row=latest_row,
                        previous_state=str(item.get("previous_state") or "INSUFFICIENT_DATA"),
                        current_state=str(item.get("current_state") or "INSUFFICIENT_DATA"),
                        emergence=bool(item.get("emergence")), now=settled_at,
                        data_fresh=True, signal_executed=False, allow_trigger=False,
                        count_cooldown=True)
                machine.pop("settlement_wait", None)
                machine.pop("settlement_resume_state", None)
                machine.pop("deferred_verified_rounds", None)
                decision = prepare_strategy_eligibility(machine, latest_row,
                    decision_timestamp=settled_at.isoformat())
                if decision and decision.get("eligibility") == "ELIGIBLE":
                    pending = machine.setdefault("pending_strategy_eligibilities", [])
                    if all(item.get("eligibility_id") != decision.get("eligibility_id")
                           for item in pending):
                        pending.append(dict(decision))
                machine["data_health"] = ("WAITING_FOR_SHADOW_SETTLEMENT"
                    if machine.get("state") == "WAITING_FOR_SHADOW_SETTLEMENT" else
                    "DATA_STALE" if machine.get("state") == "UNSAFE_STATE" else "HEALTHY")
                machine_row.payload = machine
                machine_row.updated_at = settled_at
            latest = machine.get("latest_strategy_eligibility")
            self._latest_eligibility = dict(latest) if isinstance(latest, dict) else None
            self._series_state = machine.get("state")
            self._provenance = machine.get("provenance")
            self._state = "UNSAFE_STATE" if machine.get("state") == "UNSAFE_STATE" else "LIVE_READY"
            return {"status": "RECONCILED", "signal_id": signal_id,
                    "state": machine.get("state"),
                    "eligibility_id": (decision or {}).get("eligibility_id")}

    @staticmethod
    def _recovery_rows(session: Session, stream_id: str, start: int,
                       end: int) -> list[BehaviourApiRound] | None:
        """Load a bounded, contiguous replay slice from Behaviour's durable local inbox."""
        if end < start:
            return []
        if end - start + 1 > 20_000:
            return None
        rounds = session.scalars(select(BehaviourApiRound).where(
            BehaviourApiRound.stream_id == stream_id,
            BehaviourApiRound.round_index >= start,
            BehaviourApiRound.round_index <= end).order_by(BehaviourApiRound.round_index)).all()
        if len(rounds) != end - start + 1:
            return None
        output: list[BehaviourApiRound] = []
        for expected, observed in enumerate(rounds, start):
            payload = observed.payload if isinstance(observed.payload, dict) else {}
            if (int(observed.round_index) != expected
                    or observed.status not in {"PROMOTED", "RECONCILED", "STAGED"}
                    or payload.get("event_type") != "round.observed"
                    or payload.get("durability") != "PENDING"
                    or payload.get("continuity_status") != "VERIFIED"
                    or str(payload.get("delivery_mode") or "").upper() not in {"LIVE", "RECOVERED"}
                    or str(payload.get("round_uid") or payload.get("round_id") or "") != observed.round_id
                    or int(payload.get("round_index") or 0) != expected
                    or Decimal(str(payload.get("multiplier"))) != Decimal(observed.multiplier_raw)
                    or payload.get("behaviour_provenance") not in {"LIVE_VERIFIED", "RECOVERY_REPLAY"}):
                return None
            output.append(observed)
        return output

    @staticmethod
    def _rolling_outcomes_at(session: Session, stream_id: str, sequence: int) -> list[str]:
        """Rebuild only the five-round context at a durable live checkpoint."""
        start = max(1, sequence - 4)
        rows = session.scalars(select(BehaviourApiRound).where(
            BehaviourApiRound.stream_id == stream_id,
            BehaviourApiRound.round_index >= start,
            BehaviourApiRound.round_index <= sequence).order_by(BehaviourApiRound.round_index)).all()
        outcomes: list[str] = []
        previous = None
        for row in rows:
            payload = row.payload if isinstance(row.payload, dict) else {}
            if (payload.get("continuity_status") == "GAP_BEFORE"
                    or (previous is not None and int(row.round_index) != previous + 1)):
                outcomes = []
            try:
                outcome = _classification(row.multiplier_raw)
                outcomes.append(outcome)
                outcomes = outcomes[-5:]
            except (TypeError, ValueError, ArithmeticError):
                outcomes = []
            previous = int(row.round_index)
        return outcomes

    @staticmethod
    def _strategy_state_at_checkpoint(machine: dict) -> str:
        if machine.get("state") != "UNSAFE_STATE":
            return str(machine.get("state") or "IDLE")
        series = machine.get("series") or {}
        part1 = series.get("part1") or {}
        if part1.get("completion_pending"):
            return "PART1_COMPLETED"
        part2 = series.get("part2")
        if not isinstance(part2, dict):
            return "PART1_COLLECTING"
        if part2.get("pause_started_at"):
            return ("PART2_PAUSED_NON_HIGH" if part2.get("pause_reason") in {"LOW_RESULT", "LOW_DOMINANT"}
                    else "PART2_PAUSED_NON_HIGH")
        return "PART2_ACTIVE"

    def _replay_series_after_live_checkpoint(self, session: Session, machine: dict,
                                             stream_id: str,
                                             through_sequence: int) -> tuple[dict, list[str], int] | None:
        """Rebuild missed strategy progress without producing any past eligibility."""
        last_live = int(machine.get("last_live_round_index") or 0)
        last_live_id = str(machine.get("last_live_round_id") or "")
        series = machine.get("series") or {}
        if (not last_live or not last_live_id or not series
                or not self._trusted_live_trigger(session, machine, stream_id)):
            return None
        trigger_sequence = int(series.get("trigger_sequence") or 0)
        trigger_round = session.get(BehaviourApiRound, f"{stream_id}:{series.get('trigger_round_id')}")
        if trigger_round is None:
            return None
        trigger_sequence = int(trigger_round.round_index)
        if last_live < trigger_sequence or through_sequence < last_live:
            return None
        replay_rows = self._recovery_rows(session, stream_id, last_live + 1, through_sequence)
        if replay_rows is None:
            return None

        live_checkpoint_row = session.get(BehaviourApiRound, f"{stream_id}:{last_live_id}")
        if live_checkpoint_row is None or int(live_checkpoint_row.round_index) != last_live:
            return None
        live_position = machine.get("last_live_round_position")
        if live_position is None:
            live_position = int(series.get("trigger_position") or 0) + last_live - trigger_sequence
        live_position = int(live_position)
        outcomes = self._rolling_outcomes_at(session, stream_id, last_live)
        rebuilt = deepcopy(machine)
        rebuilt["state"] = self._strategy_state_at_checkpoint(machine)
        rebuilt["provenance"] = "RECOVERY_REPLAY"
        rebuilt["last_processed_round_id"] = last_live_id
        rebuilt["last_processed_round_identity"] = f"winner-predict:{last_live_id}"
        rebuilt["last_processed_round_index"] = last_live
        rebuilt["last_processed_stream_id"] = stream_id
        rebuilt["last_processed_position"] = live_position
        rebuilt["live_rolling_outcomes"] = outcomes
        rebuilt["last_dominance_state"] = _dominance(outcomes)
        rebuilt["data_health"] = "HEALTHY"
        rebuilt.pop("recovery_quarantine_reason", None)

        for observed in replay_rows:
            row_payload = observed.payload if isinstance(observed.payload, dict) else {}
            round_identity = f"winner-predict:{observed.round_id}"
            multiplier = Decimal(observed.multiplier_raw)
            outcome = _classification(multiplier)
            previous = _dominance(outcomes)
            outcomes = (outcomes + [outcome])[-5:]
            current = _dominance(outcomes)
            live_position += 1
            event_time = row_payload.get("observed_at")
            try:
                replayed_at = datetime.fromisoformat(str(event_time).replace("Z", "+00:00"))
                if replayed_at.tzinfo is None:
                    replayed_at = replayed_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                replayed_at = datetime.now(timezone.utc)
            rebuilt = process_round(rebuilt,
                row={"identity_key": round_identity, "round_id": observed.round_id,
                     "position": live_position, "multiplier": str(multiplier),
                     "outcome": outcome, "timestamp": event_time},
                previous_state=previous, current_state=current, emergence=False,
                now=replayed_at, data_fresh=True,
                signal_executed=bool((target_signal := self._durable_signal_for_target(
                    session, stream_id, int(observed.round_index)))
                    and target_signal.get("confirmed_settlement")),
                signal_settlement=(target_signal.get("confirmed_settlement_data") if target_signal
                                   and target_signal.get("confirmed_settlement") else None),
                settlement_pending=bool(target_signal and not target_signal.get("confirmed_settlement")),
                pending_signal=(target_signal if target_signal
                                and not target_signal.get("confirmed_settlement") else None),
                allow_trigger=False)
            rebuilt["provenance"] = "RECOVERY_REPLAY"
            rebuilt["live_rolling_outcomes"] = outcomes
            rebuilt["last_processed_round_id"] = observed.round_id
            rebuilt["last_processed_round_identity"] = round_identity
            rebuilt["last_processed_round_index"] = int(observed.round_index)
            rebuilt["last_processed_stream_id"] = stream_id
            rebuilt["last_processed_position"] = live_position
            rebuilt["last_recovery_round_processed_at"] = datetime.now(timezone.utc).isoformat()
        rebuilt["recovery_reconciliation"] = {
            "status": "VALID_RESTORED", "series_id": series.get("series_id"),
            "trigger_round_id": series.get("trigger_round_id"),
            "live_checkpoint_round_id": last_live_id,
            "replayed_from_sequence": last_live + 1,
            "replayed_through_sequence": through_sequence,
            "eligibility_created": False,
            "reconciled_at": datetime.now(timezone.utc).isoformat(),
        }
        return rebuilt, outcomes, live_position

    def recover(self, event: dict) -> dict:
        """Replay contiguous verified events without creating retroactive eligibility."""
        continuity = event.get("continuity_status")
        delivery_mode = str(event.get("delivery_mode") or "").upper()
        if (event.get("event_type") != "round.observed"
                or event.get("durability") != "PENDING"
                or continuity not in {"VERIFIED", "GAP_BEFORE"}
                or delivery_mode not in {"LIVE", "RECOVERED"}
                or event.get("behaviour_provenance") != "RECOVERY_REPLAY"):
            return {"status": "REJECTED_PROVENANCE"}
        round_id = str(event.get("round_uid") or event.get("round_id") or "")
        try:
            sequence = int(event["round_index"])
            multiplier = Decimal(str(event["multiplier"]))
            stream_id = str(event.get("stream_id") or "")
        except (KeyError, TypeError, ValueError):
            return {"status": "REJECTED_IDENTITY"}
        if not round_id or round_id != str(event.get("round_id") or "") or not stream_id or not multiplier.is_finite():
            return {"status": "REJECTED_IDENTITY"}
        with self._lock:
            if sequence <= self._last_sequence:
                return {"status": "DUPLICATE", "round_id": round_id}
            if (self._last_sequence and sequence != self._last_sequence + 1
                    and continuity != "GAP_BEFORE"):
                self._state = "RECONCILING"
                return {"status": "ORDER_GAP", "expected_sequence": self._last_sequence + 1,
                        "received_sequence": sequence}
            if self._stream_id and self._stream_id != stream_id:
                self._state = "RECONCILING"
                return {"status": "STREAM_BOUNDARY_UNVERIFIED"}
            identity = f"winner-predict:{round_id}"
            with Session(self.engine, expire_on_commit=False) as session, session.begin():
                cohort = session.get(HybridCohort, 1)
                source_round = session.get(BehaviourApiRound, f"{stream_id}:{round_id}")
                if (cohort is None or source_round is None
                        or source_round.status not in {"PROMOTED", "RECONCILED"}
                        or source_round.round_id != round_id
                        or int(source_round.round_index) != sequence
                        or Decimal(str(source_round.multiplier_raw)) != multiplier):
                    return {"status": "API_ROUND_UNAVAILABLE"}
                outcome = _classification(multiplier)
                machine_row = session.get(HighDominanceSeriesState, cohort.cohort_id)
                machine = dict(machine_row.payload or {}) if machine_row else new_state()
                series = machine.get("series") or {}
                has_series = bool(series)
                trusted_series = (has_series and self._trusted_live_trigger(session, machine, stream_id))
                unresolved_quarantine = machine.get("state") == "UNSAFE_STATE" and (
                    machine.get("recovery_quarantine_reason") != "MISSED_LIVE_ROUNDS_DURING_RECOVERY"
                    or not trusted_series)
                replayed_checkpoint = None

                if (continuity == "VERIFIED" and has_series and trusted_series
                        and machine.get("state") == "UNSAFE_STATE"
                        and not unresolved_quarantine):
                    # Older recovery code advanced the round cursor but left the
                    # active series untouched. Rebuild from its last durable LIVE
                    # checkpoint and the verified local event log, with signal
                    # creation disabled for the entire replay.
                    replayed_checkpoint = self._replay_series_after_live_checkpoint(
                        session, machine, stream_id, sequence)

                if replayed_checkpoint is not None:
                    machine, next_outcomes, position = replayed_checkpoint
                    status = "RESTORED_FROM_RECOVERY"
                elif has_series and continuity == "GAP_BEFORE":
                    next_outcomes = []
                    position = max(self._position,
                                   int(machine.get("last_processed_position") or 0)) + 1
                    machine["state"] = "UNSAFE_STATE"
                    machine["provenance"] = "UNVERIFIABLE"
                    machine["recovery_quarantine_reason"] = "GAP_BEFORE_DURING_ACTIVE_SERIES"
                    machine["data_health"] = "DATA_STALE"
                    machine["live_rolling_outcomes"] = next_outcomes
                    machine["last_dominance_state"] = _dominance(next_outcomes)
                    status = "UNSAFE_STATE"
                elif (unresolved_quarantine or (has_series and not trusted_series)
                      or machine.get("state") == "UNSAFE_STATE"):
                    baseline = [] if continuity == "GAP_BEFORE" else list(self._outcomes)
                    next_outcomes = (baseline + [outcome])[-5:]
                    position = max(self._position,
                                   int(machine.get("last_processed_position") or 0)) + 1
                    machine["state"] = "UNSAFE_STATE"
                    machine["provenance"] = "UNVERIFIABLE"
                    machine["recovery_quarantine_reason"] = (
                        "MISSED_LIVE_ROUNDS_DURING_RECOVERY" if trusted_series
                        else machine.get("recovery_quarantine_reason") or "ACTIVE_SERIES_TRIGGER_UNVERIFIED")
                    machine["live_rolling_outcomes"] = next_outcomes
                    machine["last_dominance_state"] = _dominance(next_outcomes)
                    status = "UNSAFE_STATE"
                elif continuity == "GAP_BEFORE":
                    # A gap starts a fresh descriptive segment. It cannot count
                    # toward an active series, cooldown or a new trigger.
                    next_outcomes = [outcome]
                    position = max(self._position,
                                   int(machine.get("last_processed_position") or 0)) + 1
                    machine["provenance"] = "RECOVERY_REPLAY"
                    machine["data_health"] = "DATA_STALE"
                    machine["live_rolling_outcomes"] = next_outcomes
                    machine["last_dominance_state"] = _dominance(next_outcomes)
                    status = "WAITING_FOR_LIVE"
                else:
                    previous = _dominance(list(self._outcomes))
                    next_outcomes = (list(self._outcomes) + [outcome])[-5:]
                    current = _dominance(next_outcomes)
                    position = max(self._position,
                                   int(machine.get("last_processed_position") or 0)) + 1
                    event_time = event.get("observed_at") or source_round.observed_at
                    try:
                        replayed_at = datetime.fromisoformat(str(event_time).replace("Z", "+00:00"))
                        if replayed_at.tzinfo is None:
                            replayed_at = replayed_at.replace(tzinfo=timezone.utc)
                    except (TypeError, ValueError):
                        replayed_at = datetime.now(timezone.utc)
                    machine = process_round(machine,
                        row={"identity_key": identity, "round_id": round_id,
                             "position": position, "multiplier": str(multiplier),
                             "outcome": outcome, "timestamp": str(event_time or "")},
                        previous_state=previous, current_state=current, emergence=False,
                        now=replayed_at, data_fresh=True,
                        signal_executed=bool((target_signal := self._durable_signal_for_target(
                            session, stream_id, sequence)) and target_signal.get("confirmed_settlement")),
                        signal_settlement=(target_signal.get("confirmed_settlement_data") if target_signal
                                           and target_signal.get("confirmed_settlement") else None),
                        settlement_pending=bool(target_signal and not target_signal.get("confirmed_settlement")),
                        pending_signal=(target_signal if target_signal
                                        and not target_signal.get("confirmed_settlement") else None),
                        allow_trigger=False)
                    machine["provenance"] = "RECOVERY_REPLAY"
                    machine["data_health"] = "HEALTHY"
                    machine["live_rolling_outcomes"] = next_outcomes
                    machine["last_dominance_state"] = current
                    status = "WAITING_FOR_LIVE"

                now = datetime.now(timezone.utc)
                machine["last_processed_round_id"] = round_id
                machine["last_processed_round_identity"] = identity
                machine["last_processed_round_index"] = sequence
                machine["last_processed_stream_id"] = stream_id
                machine["last_processed_position"] = position
                machine["last_recovery_round_processed_at"] = now.isoformat()
                if replayed_checkpoint is not None:
                    machine["recovery_reconciliation"]["replayed_through_sequence"] = sequence
                if machine_row:
                    machine_row.payload = machine
                    machine_row.updated_at = now
                else:
                    session.add(HighDominanceSeriesState(cohort_id=cohort.cohort_id,
                        payload=machine, updated_at=now))
            self._outcomes.clear()
            self._outcomes.extend(next_outcomes)
            self._last_identity = identity
            self._last_sequence = sequence
            self._position = position
            self._stream_id = stream_id
            self._state = "UNSAFE_STATE" if machine.get("state") == "UNSAFE_STATE" else "WAITING_FOR_LIVE"
            self._series_state = machine.get("state")
            self._provenance = machine.get("provenance")
            return {"status": status, "round_id": round_id,
                    "recovery_replayed": True, "gap_boundary": continuity == "GAP_BEFORE",
                    "eligibility_created": False,
                    "strategy_state": machine.get("state"),
                    "series_reconciled": replayed_checkpoint is not None}

    def process(self, event: dict) -> dict:
        received_ns = time.perf_counter_ns()
        if (event.get("event_type") != "round.observed"
                or event.get("durability") != "PENDING"
                or event.get("continuity_status") != "VERIFIED"
                or str(event.get("delivery_mode") or "").upper() != "LIVE"
                or event.get("behaviour_provenance") != "LIVE_VERIFIED"):
            return {"status": "REJECTED_PROVENANCE"}
        round_id = str(event.get("round_uid") or event.get("round_id") or "")
        if not round_id or round_id != str(event.get("round_id") or ""):
            return {"status": "REJECTED_IDENTITY"}
        try:
            sequence = int(event["round_index"])
            multiplier = Decimal(str(event["multiplier"]))
            stream_id = str(event.get("stream_id") or "")
        except (KeyError, TypeError, ValueError):
            return {"status": "REJECTED_IDENTITY"}
        if not stream_id or not multiplier.is_finite():
            return {"status": "REJECTED_IDENTITY"}

        with self._lock:
            identity = f"winner-predict:{round_id}"
            if identity == self._last_identity or sequence <= self._last_sequence:
                return {"status": "DUPLICATE", "round_id": round_id}
            if self._last_sequence and sequence != self._last_sequence + 1:
                self._state = "RECONCILING"
                return {"status": "ORDER_GAP", "expected_sequence": self._last_sequence + 1,
                        "received_sequence": sequence}
            if self._stream_id and self._stream_id != stream_id:
                self._state = "RECONCILING"
                return {"status": "STREAM_BOUNDARY_UNVERIFIED"}
            unsafe_state = False
            with Session(self.engine, expire_on_commit=False) as session, session.begin():
                cohort = session.get(HybridCohort, 1)
                if cohort is None:
                    return {"status": "COHORT_UNAVAILABLE"}
                identity = f"winner-predict:{round_id}"
                source_round = session.get(BehaviourApiRound, f"{stream_id}:{round_id}")
                if (source_round is None or source_round.status not in {"PROMOTED", "RECONCILED"}
                        or source_round.round_id != round_id
                        or int(source_round.round_index) != sequence
                        or Decimal(str(source_round.multiplier_raw)) != multiplier):
                    return {"status": "API_ROUND_UNAVAILABLE"}
                outcome = _classification(multiplier)
                machine_row = session.get(HighDominanceSeriesState, cohort.cohort_id)
                machine = dict(machine_row.payload or {}) if machine_row else new_state()
                # The strategy checkpoint has its own position. The analysis
                # worker owns HybridCohortRound/window persistence; sharing that
                # insert here caused the worker to skip descriptive updates and
                # made worker/HTTP scheduling determine strategy order.
                position = max(self._position, int(machine.get("last_processed_position") or 0)) + 1
                outcomes_before = list(self._outcomes)
                previous = _dominance(outcomes_before)
                next_outcomes = (outcomes_before + [outcome])[-5:]
                current = _dominance(next_outcomes)
                now = datetime.now(timezone.utc)
                unsafe_state = (machine.get("state") == "UNSAFE_STATE"
                                or machine.get("provenance") == "UNVERIFIABLE")
                target_signal = self._durable_signal_for_target(session, stream_id, sequence)
                state_before = str(machine.get("state") or "IDLE")
                series_before = machine.get("series") or {}
                part2_before = series_before.get("part2") or {}
                part2_pause_snapshot = {key: deepcopy(part2_before.get(key)) for key in (
                    "pause_reason", "pause_started_round_identity", "pause_started_at",
                    "pause_count", "total_pause_rounds", "resume_count", "pause_intervals")}
                terminal_keys = ("stop_reason", "terminal_state", "terminal_round_identity", "terminal_at")
                series_terminal_snapshot = {key: deepcopy(series_before.get(key)) for key in terminal_keys}
                previous_identity = machine.get("last_processed_identity")
                previous_position = int(machine.get("last_processed_position") or 0)
                cooldown_progress_before = int(machine.get("cooldown_progress") or 0)
                cooldown_started_at_before = machine.get("cooldown_started_at")
                fresh_high_exit_seen_before = bool(machine.get("fresh_high_exit_seen"))
                event_count_before = len(machine.get("events") or [])
                emergence = current == "HIGH_DOMINANT" and previous != "HIGH_DOMINANT"
                if (unsafe_state
                        and machine.get("recovery_quarantine_reason") == "ACTIVE_SERIES_TRIGGER_UNVERIFIED"):
                    # The old series cannot be resumed because its trigger was
                    # not proven live. Retire it once on a verified live event,
                    # then arm from the rolling state observed at this event.
                    # This event itself is never reused as a fresh trigger.
                    quarantined = machine.get("series")
                    quarantined_series_id = (quarantined or {}).get("series_id")
                    if quarantined:
                        quarantined = deepcopy(quarantined)
                        quarantined["terminal_reason"] = "ACTIVE_SERIES_TRIGGER_UNVERIFIED"
                        quarantined["quarantined_at"] = now.isoformat()
                        items = machine.setdefault("quarantined_series", [])
                        if all(item.get("series_id") != quarantined.get("series_id") for item in items):
                            items.append(quarantined)
                            del items[:-20]
                    pending = machine.get("pending_strategy_eligibilities") or []
                    cancelled = [item for item in pending
                                 if quarantined_series_id
                                 and item.get("series_id") == quarantined_series_id]
                    if cancelled:
                        archived = machine.setdefault("quarantined_eligibilities", [])
                        for item in cancelled:
                            archived.append({**item,
                                "status": "CANCELLED",
                                "cancellation_reason": "ACTIVE_SERIES_TRIGGER_UNVERIFIED",
                                "cancelled_at": now.isoformat()})
                        del archived[:-100]
                        machine["pending_strategy_eligibilities"] = [item for item in pending
                            if item not in cancelled]
                    latest_eligibility = machine.get("latest_strategy_eligibility") or {}
                    if (quarantined_series_id
                            and latest_eligibility.get("series_id") == quarantined_series_id):
                        machine["latest_strategy_eligibility"] = {
                            **latest_eligibility,
                            "eligibility": "INELIGIBLE",
                            "status": "QUARANTINED",
                            "eligibility_reason": "ACTIVE_SERIES_TRIGGER_UNVERIFIED",
                            "cancelled_at": now.isoformat(),
                        }
                    machine["series"] = None
                    machine["state"] = "ARMED_FOR_HIGH"
                    machine["provenance"] = "LIVE_VERIFIED"
                    machine["data_health"] = "HEALTHY"
                    machine["last_dominance_state"] = current
                    machine["recovery_quarantine_reason"] = None
                    machine.setdefault("events", []).append({
                        "type": "SERIES_QUARANTINED",
                        "reason": "ACTIVE_SERIES_TRIGGER_UNVERIFIED",
                        "round_identity": identity,
                        "at": now.isoformat(),
                    })
                if not unsafe_state:
                    machine = process_round(
                        machine,
                        row={"identity_key": identity, "round_id": round_id,
                             "position": position,
                             "multiplier": str(multiplier), "outcome": outcome,
                             "timestamp": now.isoformat()},
                        previous_state=previous, current_state=current,
                        emergence=emergence,
                        now=now, data_fresh=True,
                        signal_executed=bool(target_signal and target_signal.get("confirmed_settlement")),
                        signal_settlement=(target_signal.get("confirmed_settlement_data") if target_signal
                                           and target_signal.get("confirmed_settlement") else None),
                        settlement_pending=bool(target_signal and not target_signal.get("confirmed_settlement")),
                        pending_signal=(target_signal if target_signal
                                        and not target_signal.get("confirmed_settlement") else None))
                    machine["last_live_round_context"] = {
                        "identity_key": identity, "round_id": round_id,
                        "position": position, "multiplier": str(multiplier),
                        "outcome": outcome, "timestamp": now.isoformat(),
                        "previous_state": previous, "current_state": current,
                        "emergence": emergence, "state_before": state_before,
                        "series_id": (series_before.get("series_id") or ""),
                        "part2_pause_snapshot": part2_pause_snapshot,
                        "series_terminal_snapshot": series_terminal_snapshot,
                        "previous_identity": previous_identity,
                        "previous_position": previous_position,
                        "events_length_before": event_count_before,
                        "cooldown_progress_before": cooldown_progress_before,
                        "cooldown_started_at_before": cooldown_started_at_before,
                        "fresh_high_exit_seen_before": fresh_high_exit_seen_before,
                    }
                    machine["provenance"] = "LIVE_VERIFIED"
                if machine.get("state") != "WAITING_FOR_SHADOW_SETTLEMENT":
                    machine["data_health"] = "HEALTHY"
                machine["last_live_round_id"] = round_id
                machine["last_live_round_identity"] = identity
                machine["last_live_round_index"] = sequence
                machine["last_live_stream_id"] = stream_id
                machine["last_live_round_position"] = position
                machine["last_processed_round_id"] = round_id
                machine["last_processed_round_identity"] = identity
                machine["last_processed_round_index"] = sequence
                machine["last_processed_stream_id"] = stream_id
                machine["last_processed_position"] = position
                machine["last_live_round_processed_at"] = now.isoformat()
                machine["live_rolling_outcomes"] = next_outcomes
                # Keep the descriptive rolling state current during quarantine.
                # Quarantine still blocks strategy transitions and eligibility;
                # leaving this value stale made the UI report LOW while the
                # same verified rolling window was already HIGH.
                machine["last_dominance_state"] = current
                if not unsafe_state:
                    decision = prepare_strategy_eligibility(machine,
                        {"identity_key": identity, "round_id": round_id,
                         "position": position}, decision_timestamp=now.isoformat())
                    if decision and decision.get("eligibility") == "ELIGIBLE":
                        pending = machine.setdefault("pending_strategy_eligibilities", [])
                        if all(item.get("eligibility_id") != decision.get("eligibility_id")
                               for item in pending):
                            pending.append(dict(decision))
                else:
                    decision = None
                machine["last_live_decision_latency_ms"] = (
                    time.perf_counter_ns() - received_ns) / 1_000_000
                if machine_row:
                    machine_row.payload = machine
                    machine_row.updated_at = now
                else:
                    session.add(HighDominanceSeriesState(cohort_id=cohort.cohort_id,
                        payload=machine, updated_at=now))
            # Only publish the in-memory checkpoint after the durable commit.
            self._outcomes.clear()
            self._outcomes.extend(next_outcomes)
            self._last_identity = identity
            self._last_sequence = sequence
            self._position = position
            self._stream_id = stream_id
            self._state = "UNSAFE_STATE" if machine.get("state") == "UNSAFE_STATE" else "LIVE_READY"
            self._latest_live_round_id = round_id
            self._latest_live_identity = identity
            self._latest_live_processed_at = now.isoformat()
            self._series_state = machine.get("state")
            self._provenance = machine.get("provenance")
            latest = machine.get("latest_strategy_eligibility")
            self._latest_eligibility = dict(latest) if isinstance(latest, dict) else None
            elapsed_ms = (time.perf_counter_ns() - received_ns) / 1_000_000
            self._latencies_ms.append(elapsed_ms)
            status = ("UNSAFE_STATE" if machine.get("state") == "UNSAFE_STATE"
                      else "QUARANTINED_SERIES_RETIRED" if unsafe_state else "PROCESSED")
            log.info("[BEHAVIOUR_LIVE_DECISION] round_uid=%s status=%s decision_ms=%.3f",
                     round_id, status, elapsed_ms)
            return {"status": status, "round_id": round_id,
                    "identity": identity, "decision": decision,
                    "decision_latency_ms": elapsed_ms, "dominance_state": current}

    def metrics(self) -> dict:
        with self._lock:
            values = sorted(self._latencies_ms)
            def percentile(q: float):
                if not values:
                    return None
                return round(values[min(len(values) - 1, int((len(values) - 1) * q))], 3)
            stages = {}
            for stage, samples in self._stage_latencies_ms.items():
                ordered = sorted(samples)
                stages[stage] = {"samples": len(ordered),
                    "p50_ms": round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * .50))], 3),
                    "p95_ms": round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * .95))], 3),
                    "p99_ms": round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * .99))], 3),
                    "max_ms": round(max(ordered), 3)} if ordered else {"samples": 0}
            return {"status": self._state, "samples": len(values),
                    "p50_ms": percentile(.50), "p95_ms": percentile(.95),
                    "p99_ms": percentile(.99), "max_ms": round(max(values), 3) if values else None,
                    "stages": stages,
                    "latest_live_round_uid": self._latest_live_round_id,
                    "latest_live_round_identity": self._latest_live_identity,
                    "latest_live_processed_at": self._latest_live_processed_at,
                    "last_processed_round_identity": self._last_identity,
                    "series_state": self._series_state,
                    "provenance": self._provenance,
                    "latest_eligibility": self._latest_eligibility}
