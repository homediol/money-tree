from __future__ import annotations

import logging
import math
import time
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .analysis import (
    baseline_assessment, cached_recovery_reference, classify_state, compare_historical_recovery,
    confirm_state, disjoint_reference_blocks,
    motif_evidence, rolling_features, sequence_features,
)
from .models import (
    AnalysisSnapshot, BehaviourStateHistory, CanonicalRound, CollectorActivity,
    CumulativeStats, DistributionSnapshot, ForecastEvaluationResult,
    DominanceWindow, ForecastHistory, ForecastReferenceBlock, HybridCohort,
    HighDominanceSeriesState, HighDominanceStateTest,
    HybridCohortRound, MotifObservation,
    Next100Assessment, OrbitReading, OrbitRoundMapping, ResearchImport, RuleDefinition,
    RuleEvaluation, SequenceObservation,
    SequenceOutlookResult, SystemHealth, WorkerCheckpoint,
)
from .rules import RULE_VERSION, active_rule_registry, evaluate_descriptive_rules
from .hybrid import (AUTHORITATIVE_ORDER_QUALITIES, COHORT_WINDOW_SIZE,
                     HIGH_DOMINANCE_STATE_TEST_CHECKPOINTS,
                     HIGH_DOMINANCE_STATE_TEST_POLICY_VERSION,
                     POLICY_VERSION, analyse_window,
                     rolling_analysis, update_hysteresis)

log = logging.getLogger("behaviour.worker")


def _round_dict(row: CanonicalRound) -> dict:
    return {"identity_key": row.identity_key, "round_id": row.round_id,
            "analysis_position": row.analysis_position, "outcome": row.outcome,
            "multiplier_raw": row.multiplier_raw, "event_timestamp": row.event_timestamp,
            "ingestion_timestamp": row.ingestion_timestamp, "order_quality": row.order_quality,
            "timestamp_quality": row.timestamp_quality}


def _forecast_change(current: dict, previous: dict | None) -> dict:
    if previous is None:
        return {"initial_assessment": True}
    old = previous.get("forecast") or {}
    change = {}
    for key in ("expected_high_count", "expected_low_count", "expected_exact_count"):
        before, after = old.get(key), current.get(key)
        change[key] = None if before is None or after is None else round(float(after) - float(before), 8)
    change["state_changed"] = old.get("state") != current.get("state")
    change["baseline_block_count_changed"] = old.get("historical_block_count") != current.get("historical_block_count")
    change["contextual_adjustment_active"] = False
    return change


def _maybe_add_completed_block(session: Session, position: int) -> dict | None:
    # Anchor the first live reference window immediately after the immutable
    # Phase 1 snapshot. Do not let its 23-round tail create an overlapping block.
    frozen_position = 50123
    if position <= frozen_position or (position - frozen_position) % 100:
        return None
    exists = session.scalar(select(ForecastReferenceBlock.block_number).order_by(ForecastReferenceBlock.block_number.desc()).limit(1))
    block_number = int(exists or 0) + 1
    rows = session.scalars(select(CanonicalRound).where(
        CanonicalRound.analysis_position.between(position - 99, position)
    ).order_by(CanonicalRound.analysis_position)).all()
    if len(rows) != 100:
        raise RuntimeError(f"forecast block at position {position} has {len(rows)} rounds; expected 100")
    if any("cross_source_overlap_unknown" in r.order_quality for r in rows):
        # Do not let an uncertain source boundary enter the validated forecast
        # reference distribution. The Phase 1 baseline remains available.
        return None
    symbols = [r.outcome for r in rows]
    high, low, exact = symbols.count("H"), symbols.count("L"), symbols.count("E")
    rate = high / (high + low) if high + low else 0.5
    sequence = sequence_features(symbols)
    block = ForecastReferenceBlock(block_number=block_number,
                                   first_position=position - 99, last_position=position,
                                   high_count=high, low_count=low, exact_count=exact,
                                   high_rate_nonexact=rate, sequence_stats=sequence,
                                   source="live_verified_sequence")
    session.add(block)
    session.flush()
    return {"block_number": block_number, "first_position": position - 99,
            "last_position": position, "high_count": high, "low_count": low,
            "exact_count": exact, "high_rate_nonexact": rate, "sequence": sequence}


def _evaluate_matured_disjoint_forecast(session: Session, current_position: int, frozen_position: int = 50123) -> None:
    origin = current_position - 100
    if origin < frozen_position or (origin - frozen_position) % 100:
        return
    forecast = session.scalar(select(ForecastHistory).where(
        ForecastHistory.latest_position == origin,
        ForecastHistory.forecast_type == "next100_expanding_baseline"
    ).order_by(ForecastHistory.id.desc()).limit(1))
    if forecast is None:
        return
    future = session.scalars(select(CanonicalRound).where(
        CanonicalRound.analysis_position.between(origin + 1, current_position)
    ).order_by(CanonicalRound.analysis_position)).all()
    if len(future) != 100:
        return
    if any("cross_source_overlap_unknown" in row.order_quality for row in future):
        # Keep the forecast history honest when the realized target block crosses
        # an unverified handoff from the frozen research sequence.
        return
    h, l, e = (sum(r.outcome == s for r in future) for s in ("H", "L", "E"))
    actual = h / (h + l) if h + l else 0.5
    predicted = forecast.forecast.get("expected_high_rate_nonexact")
    if predicted is None:
        return
    error = float(predicted) - actual
    session.add(ForecastEvaluationResult(
        forecast_id=forecast.id, evaluated_at=datetime.now(timezone.utc),
        horizon_end_position=current_position,
        outcome={"high_count": h, "low_count": l, "exact_count": e,
                 "high_rate_nonexact": actual, "horizon_rounds": 100},
        metrics={"high_rate_squared_error": error ** 2,
                 "high_rate_absolute_error": abs(error),
                 "chronological_disjoint_anchor": True,
                 "overlapping_horizon": False}))


def process_round(session: Session, row: CanonicalRound, reference_blocks: list[dict], *,
                  state_confirm_rounds: int = 3, min_state_rounds: int = 20,
                  gap_seconds: float = 180.0452,
                  recovery_reference: dict | None = None,
                  phase1_high_rate: float | None = None,
                  phase1_counts: dict | None = None) -> dict:
    """Commit all analysis for one durable new round as one transaction."""
    position = row.analysis_position
    context_rows = session.scalars(select(CanonicalRound).where(
        CanonicalRound.analysis_position <= position
    ).order_by(CanonicalRound.analysis_position.desc()).limit(500)).all()
    context_rows.reverse()
    context = [_round_dict(r) for r in context_rows]
    context_source = "canonical_analysis_order"
    if row.source_name == "winner_predict_database":
        last_source_gap = max((index for index, item in enumerate(context_rows)
                               if item.source_name == row.source_name
                               and "uncertain" in item.order_quality), default=-1)
        if last_source_gap >= 0:
            context_rows = context_rows[last_source_gap:]
            context = [_round_dict(item) for item in context_rows]
        context_source = "winner_predict_database_recent_source_segment"
    cross_source_unknown = "cross_source_overlap_unknown" in row.order_quality
    if cross_source_unknown:
        # Use the page snapshot associated with this exact reading/item, clipped
        # at that item in the newest-first DOM list, so a later item from the
        # same reading cannot leak into this round's context.
        mapping = session.scalar(select(OrbitRoundMapping).where(
            OrbitRoundMapping.identity_key == row.identity_key
        ).order_by(OrbitRoundMapping.item_index.asc()).limit(1))
        reading = session.get(OrbitReading, mapping.orbit_reading_id) if mapping else None
        visible = []
        from .orbit_client import parse_reading
        parsed_snapshot = parse_reading(reading.raw_payload) if reading else None
        if parsed_snapshot:
            # Use the same coalesced logical list as ingestion. A raw duplicated
            # DOM half must not cut the per-round context from 60 to 30 items.
            from .collector import _visible_dom
            for item in _visible_dom(parsed_snapshot):
                if item["item_index"] >= (mapping.item_index if mapping else 0):
                    visible.append({"identity_key": f"orbit-context:{reading.reading_id}:{item['item_index']}",
                                "analysis_position": 0,
                                "outcome": "H" if Decimal(item["multiplier_raw"]) > 2 else "L" if Decimal(item["multiplier_raw"]) < 2 else "E",
                                "multiplier_raw": item["multiplier_raw"], "event_timestamp": None,
                                "ingestion_timestamp": reading.checked_at,
                                "order_quality": "visible_page_context_overlap_unknown",
                                "timestamp_quality": "per_round_event_time_unavailable"})
        # Orbit's payout list is newest first; put only this round and its prior
        # visible rounds into chronological order.
        context = list(reversed(visible))
        for index, item in enumerate(context):
            item["analysis_position"] = position - len(context) + index + 1
        context_source = "orbit_reading_snapshot_overlap_unknown"
    symbols = [r["outcome"] for r in context]
    seq = sequence_features(symbols)
    rolling = rolling_features(context)

    stats = session.get(CumulativeStats, "global")
    if stats is None:
        raise RuntimeError("cumulative analysis statistics have not been initialized by the Phase 1 import")
    stats.round_count += 1
    stats.high_count += row.outcome == "H"
    stats.low_count += row.outcome == "L"
    stats.exact_count += row.outcome == "E"
    if row.event_timestamp is None:
        stats.event_timestamp_missing_count += 1
    else:
        stats.event_timestamp_count += 1
    if row.timestamp_quality == "shared_timestamp":
        stats.timestamp_tie_count += 1
    if "uncertain" in row.order_quality:
        stats.order_uncertain_count += 1
    if cross_source_unknown:
        stats.cross_source_overlap_unknown_count += 1
    multiplier = Decimal(row.multiplier_raw)
    stats.minimum_multiplier = min(stats.minimum_multiplier, multiplier)
    stats.maximum_multiplier = max(stats.maximum_multiplier, multiplier)
    stats.multiplier_sum += multiplier
    log_value = Decimal(str(math.log(float(multiplier))))
    stats.log_multiplier_sum = Decimal(str(stats.log_multiplier_sum)) + log_value
    stats.log_multiplier_square_sum = (Decimal(str(stats.log_multiplier_square_sum))
                                       + log_value * log_value)
    tails = dict(stats.tail_counts or {})
    for threshold in ("5", "10", "100", "1000"):
        key = f"ge_{threshold}"
        tails[key] = int(tails.get(key, 0)) + int(multiplier >= Decimal(threshold))
    stats.tail_counts = tails
    stats.updated_at = datetime.now(timezone.utc)

    completed = _maybe_add_completed_block(session, position)
    if completed:
        reference_blocks.append(completed)
    if not reference_blocks:
        # The imported warm-up is expected to provide this, but fail explicitly.
        raise RuntimeError("no validated Phase 1 expanding reference blocks are available")
    previous_snapshot = session.scalar(select(AnalysisSnapshot).order_by(AnalysisSnapshot.id.desc()).limit(1))
    old_state = (previous_snapshot.features or {}).get("state", {}) if previous_snapshot else {}
    definitions = session.scalars(select(RuleDefinition)).all()
    rule_registry = active_rule_registry(definitions)
    state_rule = rule_registry.get("BEHAVIOUR_STATE_DESCRIPTION")
    state_thresholds = (state_rule or {}).get("thresholds", {})
    if state_rule is None:
        candidate = "UNCERTAIN"
        state_evidence = {"explanation": "behaviour state rule is disabled or unavailable",
                          "evidence_class": "rule_disabled"}
    else:
        candidate, state_evidence = classify_state(
            rolling["50"],
            min_rounds=int(state_thresholds.get("minimum_nonexact", min_state_rounds)),
            high_dominance=float(state_thresholds.get("high_dominant", 0.60)),
            low_dominance=float(state_thresholds.get("low_dominant", 0.40)),
            balanced_band=float(state_thresholds.get("balanced_half_width", 0.08)),
            alternation_threshold=float(state_thresholds.get("alternation", 0.65)),
            high_cluster_run_p90=int(state_thresholds.get("high_run_p90", 7)),
            low_cluster_run_p90=int(state_thresholds.get("low_run_p90", 8)))
        state_confirm_rounds = int(state_thresholds.get("confirmation_rounds", state_confirm_rounds))
    hysteresis = confirm_state(candidate, old_state, confirm_rounds=state_confirm_rounds)
    confirmed_state = hysteresis["confirmed_state"]
    assessment = baseline_assessment(reference_blocks, latest_position=position,
                                     latest_identity_key=row.identity_key, state=confirmed_state)
    phase1_import = None
    if phase1_counts is None or phase1_high_rate is None:
        phase1_import = session.scalar(select(ResearchImport).order_by(
            ResearchImport.snapshot_utc.desc()).limit(1))
    if phase1_counts is None and phase1_import is not None:
        phase1_counts = {"H": phase1_import.high_count, "L": phase1_import.low_count,
                         "E": phase1_import.exact_count,
                         "rounds": phase1_import.canonical_rounds}
    if phase1_high_rate is None and phase1_counts:
        phase1_high_rate = phase1_counts["H"] / max(1, phase1_counts["H"] + phase1_counts["L"])
    recovery = compare_historical_recovery(symbols[-50:], recovery_reference, phase1_high_rate)
    if row.source_name == "winner_predict_database":
        source_counts = session.execute(select(CanonicalRound.outcome, func.count()).where(
            CanonicalRound.source_name == row.source_name
        ).group_by(CanonicalRound.outcome)).all()
        session_counts = {symbol: 0 for symbol in ("H", "L", "E")}
        for symbol, count in source_counts:
            if symbol in session_counts:
                session_counts[symbol] = int(count)
        session_history_source = "Winner Predict PostgreSQL aviator_rounds"
        phase1_boundary_continuity = "verified_by_shared_round_id_overlap"
        timestamp_status = ("PLATFORM_EVENT_TIME_AVAILABLE_WHEN_PRESENT"
                            if row.event_timestamp else "PER_ROUND_SOURCE_OBSERVATION_TIME_ONLY")
        timestamp_gaps = "platform_event_time_unavailable_when_source_timestamp_is_observation_only"
    else:
        session_counts = ({symbol: max(0, int(getattr(stats, attr)) - int(phase1_counts[symbol]))
                           for symbol, attr in (("H", "high_count"), ("L", "low_count"), ("E", "exact_count"))}
                          if phase1_counts else None)
        session_history_source = "durable Orbit accepted deltas"
        phase1_boundary_continuity = "unknown" if cross_source_unknown else "verified"
        timestamp_status = "PER_ROUND_EVENT_TIMESTAMPS_UNAVAILABLE"
        timestamp_gaps = "NOT_MEASURABLE_FROM_ORBIT_CHECKED_AT"
    live_context = {
        "source": context_source,
        "analysis_position": position,
        "rolling50": rolling.get("50", {}),
        "recent_sequence_hle": "".join(symbols[-50:]),
        "observed_state": candidate,
        "state_evidence": state_evidence,
        "timestamp_status": timestamp_status,
        "timestamp_gaps": timestamp_gaps,
        "session_history": {
            "source": session_history_source,
            "counts": session_counts,
            "round_count": sum(session_counts.values()) if session_counts else None,
            "phase1_boundary_continuity": phase1_boundary_continuity,
            "seed_context_merged": row.source_name == "winner_predict_database",
        },
        "context_updated_at": (row.ingestion_timestamp.isoformat()
                               if row.ingestion_timestamp else None),
    }
    assessment["explanation"]["live_context"] = live_context
    assessment["explanation"]["historical_recovery_comparison"] = recovery
    # The motif list itself is Phase 1 evidence imported into this database.
    from .models import DiscoveredMotif
    motif_patterns = [m.motif for m in session.scalars(select(DiscoveredMotif).order_by(DiscoveredMotif.length, DiscoveredMotif.motif)).all()]
    motifs = motif_evidence(symbols, motif_patterns)
    current_time_group = {"status": "EVENT_TIME_UNAVAILABLE",
                          "explanation": ("Winner Predict source event timestamps are unavailable for this round; its per-round observation and stored times are retained."
                                          if row.source_name == "winner_predict_database" and not row.event_timestamp
                                          else "Orbit exposes reading checkedAt, not per-round event timestamps."),
                          "source_observed_at": row.source_observed_at.isoformat() if row.source_observed_at else None,
                          "checkedAt_is_ingestion_observation_time": row.source_name != "winner_predict_database"}
    quality = {"event_timestamp_count": stats.event_timestamp_count,
               "event_timestamp_missing_count": stats.event_timestamp_missing_count,
               "timestamp_tie_count": stats.timestamp_tie_count,
               "order_uncertain_count": stats.order_uncertain_count,
               "cross_source_overlap_unknown_count": stats.cross_source_overlap_unknown_count,
               "latest_timestamp_quality": row.timestamp_quality,
               "latest_order_quality": row.order_quality,
               "latest_event_timestamp": row.event_timestamp.isoformat() if row.event_timestamp else None,
               "latest_ingestion_timestamp": row.ingestion_timestamp.isoformat() if row.ingestion_timestamp else None}
    log_mean = float(stats.log_multiplier_sum / stats.round_count)
    log_variance = max(0.0, float(stats.log_multiplier_square_sum / stats.round_count) - log_mean ** 2)
    if stats.round_count > 1:
        log_variance *= stats.round_count / (stats.round_count - 1)
    global_dist = {"round_count": stats.round_count,
                   "counts": {"H": stats.high_count, "L": stats.low_count, "E": stats.exact_count},
                   "high_rate_all": stats.high_count / stats.round_count,
                   "low_rate_all": stats.low_count / stats.round_count,
                   "exact_rate": stats.exact_count / stats.round_count,
                   "high_rate_nonexact": stats.high_count / max(1, stats.high_count + stats.low_count),
                   "mean_multiplier": float(stats.multiplier_sum / stats.round_count),
                   "min_multiplier": str(stats.minimum_multiplier), "max_multiplier": str(stats.maximum_multiplier),
                   "log_mean": log_mean,
                   "log_volatility_sample": log_variance ** 0.5,
                   "tail_counts": tails,
                   "tail_rates": {key: value / stats.round_count for key, value in tails.items()},
                   "source_context": ("Phase 1 snapshot plus Winner Predict aviator_rounds, anchored by shared round identities"
                                      if row.source_name == "winner_predict_database"
                                      else "Phase 1 snapshot plus independently verified Orbit deltas; cross-source boundary status is explicit")}
    state_payload = {"candidate": candidate, **hysteresis, **state_evidence}
    quality["current_context_source"] = context_source
    quality["cross_source_overlap_unknown"] = cross_source_unknown
    features = {"sequence": seq, "rolling_windows": rolling, "motifs": motifs,
                "current_time_group": current_time_group, "state": state_payload,
                "next100": assessment, "live_context": live_context,
                "historical_recovery_comparison": recovery,
                "global_distribution": global_dist,
                "source_quality": quality}
    rule_eval = evaluate_descriptive_rules(features, latest_identity_key=row.identity_key,
                                           registry=rule_registry)
    previous_forecast = session.scalar(select(ForecastHistory).order_by(ForecastHistory.id.desc()).limit(1))
    previous_payload = {"forecast": previous_forecast.forecast} if previous_forecast else None
    changed = _forecast_change(assessment, previous_payload)
    now = datetime.now(timezone.utc)
    session.add(AnalysisSnapshot(generated_at=now, latest_identity_key=row.identity_key,
                                 latest_position=position, round_count=stats.round_count,
                                 features=features, source_quality=quality))
    session.add(SequenceObservation(identity_key=row.identity_key,
                                    analysis_position=position, symbol=row.outcome,
                                    observed_at=row.ingestion_timestamp or now,
                                    order_quality=row.order_quality))
    session.add(DistributionSnapshot(generated_at=now, horizon="global_cumulative_summary",
                                     counts=global_dist["counts"], statistics=global_dist))
    for width, data in rolling.items():
        if data.get("complete"):
            session.add(DistributionSnapshot(generated_at=now, horizon=f"rolling_{width}",
                                             counts=data["counts"], statistics=data))
    session.add(Next100Assessment(
        assessed_at=now, latest_identity_key=row.identity_key, latest_position=position,
        training_blocks=assessment["historical_block_count"],
        expected_high_rate_nonexact=assessment["expected_high_rate_nonexact"],
        expected_exact_rate=assessment["expected_exact_rate"],
        expected_high_count=assessment["expected_high_count"],
        expected_low_count=assessment["expected_low_count"],
        expected_exact_count=assessment["expected_exact_count"],
        historical_count_distribution=assessment["historical_count_distribution"],
        threshold_probabilities=assessment["threshold_probabilities"],
        data_sufficiency=assessment["data_sufficiency"], state=confirmed_state,
        active_rules=[e["rule_id"] for e in rule_eval], explanation=assessment["explanation"]))
    session.add(ForecastHistory(assessed_at=now, latest_identity_key=row.identity_key,
                                latest_position=position, forecast_type="next100_expanding_baseline",
                                forecast=assessment, change_from_previous=changed))
    session.add(SequenceOutlookResult(assessed_at=now, latest_identity_key=row.identity_key,
                                      historical_windows=len(reference_blocks),
                                      outlook=assessment["historical_count_distribution"],
                                      evidence_status="historical_empirical_distribution_descriptive"))
    session.add(BehaviourStateHistory(generated_at=now, latest_identity_key=row.identity_key,
                                      observed_state=candidate, confirmed_state=confirmed_state,
                                      evidence={**state_evidence, "hysteresis": hysteresis},
                                      rule_version=(state_rule or {}).get("version", RULE_VERSION)))
    for evaluation in rule_eval:
        session.add(RuleEvaluation(rule_id=evaluation["rule_id"],
                                   rule_version=evaluation["rule_version"], evaluated_at=now,
                                   latest_identity_key=row.identity_key,
                                   result=evaluation["result"],
                                   evidence_class=evaluation["evidence_class"],
                                   explanation=evaluation["explanation"]))
    previous_motifs = ((previous_snapshot.features or {}).get("motifs", {})
                       if previous_snapshot else {})
    for motif, count_data in motifs.items():
        current_count = count_data["count_last_500"]
        previous_count = previous_motifs.get(motif, {}).get("count_last_500")
        # The full current motif vector is already stored in AnalysisSnapshot.
        # Keep this time-series table compact by recording only count changes;
        # a 500-round window can add/remove only a small number of motifs per
        # new outcome, so writing every nonzero motif each round is redundant.
        if current_count and current_count != previous_count:
            session.add(MotifObservation(generated_at=now, motif=motif, horizon=500,
                                         occurrence_count=current_count,
                                         possible_windows=count_data["possible_windows_last_500"]))
    _evaluate_matured_disjoint_forecast(session, position)
    checkpoint = session.get(WorkerCheckpoint, "analysis")
    if checkpoint is None:
        checkpoint = WorkerCheckpoint(name="analysis", last_position=position, updated_at=now)
        session.add(checkpoint)
    else:
        checkpoint.last_position = position
        checkpoint.updated_at = now
    log.info("analysed position=%d outcome=%s state=%s forecast_blocks=%d",
             position, row.outcome, confirmed_state, len(reference_blocks))
    return features


def _hybrid_round_dict(cohort_row: HybridCohortRound, round_row: CanonicalRound) -> dict:
    if round_row.timestamp_quality == "platform_event_time" and round_row.event_timestamp:
        timestamp_role = "platform_event_time"
    elif round_row.source_observed_at:
        timestamp_role = "source_observed_at"
    else:
        timestamp_role = "ingestion_timestamp" if round_row.ingestion_timestamp else None
    return {
        "identity_key": round_row.identity_key, "round_id": round_row.round_id,
        "analysis_position": round_row.analysis_position,
        "cohort_position": cohort_row.cohort_position, "outcome": round_row.outcome,
        "source_name": round_row.source_name, "source_sequence": round_row.source_sequence,
        "order_quality": round_row.order_quality,
        "timestamp_quality": round_row.timestamp_quality,
        "event_timestamp": round_row.event_timestamp,
        "source_observed_at": round_row.source_observed_at,
        "ingestion_timestamp": round_row.ingestion_timestamp,
        "timestamp_role": timestamp_role,
    }


def _update_confirmed_high_state_test(session: Session, *, cohort: HybridCohort,
                                      row: CanonicalRound, position: int,
                                      previous_state: str, confirmed_state: str,
                                      now: datetime) -> None:
    """Score forward observations only during one confirmed HIGH episode.

    The state-entry decision round is an anchor and is not scored. An observation
    that itself causes the confirmed state to exit HIGH is retained as the final
    forward sample, after which this episode closes.
    """
    active = session.scalar(select(HighDominanceStateTest).where(
        HighDominanceStateTest.cohort_id == cohort.cohort_id,
        HighDominanceStateTest.status == "COLLECTING",
    ).order_by(HighDominanceStateTest.decision_position.desc()).limit(1))

    if active is not None and previous_state == "HIGH_DOMINANT":
        multiplier = Decimal(row.multiplier_raw)
        result = "WIN" if multiplier >= Decimal("2.00") else "LOSS"
        observation = {
            "cohort_position": position, "identity_key": row.identity_key,
            "round_id": row.round_id, "multiplier": row.multiplier_raw,
            "classification": row.outcome, "result_at_2x": result,
            "source_name": row.source_name, "source_sequence": row.source_sequence,
            "observed_at": ((row.source_observed_at or row.event_timestamp
                             or row.ingestion_timestamp).isoformat()
                            if (row.source_observed_at or row.event_timestamp
                                or row.ingestion_timestamp) else None),
        }
        outcomes = [*(active.outcomes or []), observation]
        active.outcomes = outcomes
        active.wins += int(result == "WIN")
        active.losses += int(result == "LOSS")
        active.exact_count += int(row.outcome == "E")
        count = len(outcomes)
        checkpoints = dict(active.checkpoint_results or {})
        if count in HIGH_DOMINANCE_STATE_TEST_CHECKPOINTS:
            prefix = outcomes[:count]
            wins = sum(item["result_at_2x"] == "WIN" for item in prefix)
            losses = sum(item["result_at_2x"] == "LOSS" for item in prefix)
            exact = sum(item["classification"] == "E" for item in prefix)
            checkpoints[str(count)] = {
                "verified_rounds": count, "wins": wins, "losses": losses,
                "exact_count": exact, "net_units": wins - losses,
                "win_rate": wins / count,
            }
        active.checkpoint_results = checkpoints
        active.updated_at = now
        if confirmed_state != "HIGH_DOMINANT":
            active.status = "STOPPED_STATE_EXIT"
            active.exit_position = position
            active.exit_state = confirmed_state
            active.completed_at = now
        elif count >= max(HIGH_DOMINANCE_STATE_TEST_CHECKPOINTS):
            active.status = "COMPLETED_50"
            active.completed_at = now

    bootstrap_current_high = False
    if active is None and previous_state == "HIGH_DOMINANT" \
            and confirmed_state == "HIGH_DOMINANT":
        # If the feature is deployed while confirmed HIGH is already active,
        # start prospectively from this verified row once. Never replay old
        # observations, and never reopen a completed 50-round episode in-place.
        bootstrap_current_high = session.scalar(select(HighDominanceStateTest.episode_id).where(
            HighDominanceStateTest.cohort_id == cohort.cohort_id
        ).order_by(HighDominanceStateTest.decision_position.desc()).limit(1)) is None
    entered_high = previous_state != "HIGH_DOMINANT" and confirmed_state == "HIGH_DOMINANT"
    if active is None and confirmed_state == "HIGH_DOMINANT" \
            and (entered_high or bootstrap_current_high):
        session.add(HighDominanceStateTest(
            episode_id=f"{cohort.cohort_id}:high-state:{position:010d}",
            cohort_id=cohort.cohort_id, decision_position=position,
            decision_identity_key=row.identity_key, decision_round_id=row.round_id,
            status="COLLECTING", exit_position=None, exit_state=None,
            outcomes=[], checkpoint_results={}, wins=0, losses=0, exact_count=0,
            policy_version=HIGH_DOMINANCE_STATE_TEST_POLICY_VERSION,
            opened_at=now, completed_at=None, updated_at=now,
        ))
    session.flush()


def process_hybrid_round(session: Session, row: CanonicalRound, *, live_delivery: bool = False) -> dict | None:
    """Append one eligible post-activation outcome and persist its local analysis."""
    cohort = session.get(HybridCohort, 1)
    if cohort is None or row.import_snapshot_id is not None:
        return None
    if row.inserted_at.replace(tzinfo=timezone.utc) <= cohort.activated_at.replace(tzinfo=timezone.utc):
        return None
    if row.order_quality not in AUTHORITATIVE_ORDER_QUALITIES or row.outcome not in {"H", "L", "E"}:
        return None
    if session.get(HybridCohortRound, row.identity_key) is not None:
        return None

    position = cohort.last_cohort_position + 1
    window_number = (position - 1) // COHORT_WINDOW_SIZE + 1
    window_start = (window_number - 1) * COHORT_WINDOW_SIZE + 1
    cohort_row = HybridCohortRound(
        identity_key=row.identity_key, cohort_id=cohort.cohort_id,
        cohort_position=position, window_number=window_number, outcome=row.outcome,
        source_name=row.source_name, source_sequence=row.source_sequence,
        order_quality=row.order_quality, timestamp_quality=row.timestamp_quality,
        event_timestamp=row.event_timestamp, source_observed_at=row.source_observed_at,
        ingestion_timestamp=row.ingestion_timestamp)
    session.add(cohort_row)
    session.flush()
    cohort.last_cohort_position = position
    cohort.last_identity_key = row.identity_key
    cohort.updated_at = datetime.now(timezone.utc)

    previous_window = session.get(DominanceWindow,
                                  f"{cohort.cohort_id}:{window_number - 1:06d}") if window_number > 1 else None
    window_id = f"{cohort.cohort_id}:{window_number:06d}"
    if previous_window is not None and previous_window.next_window_id is None:
        previous_window.next_window_id = window_id
    window = session.get(DominanceWindow, window_id)
    if window is None:
        window = DominanceWindow(
            window_id=window_id, cohort_id=cohort.cohort_id,
            window_number=window_number, lifecycle_status="OPEN",
            start_cohort_position=window_start, end_cohort_position=position,
            previous_window_id=previous_window.window_id if previous_window else None,
            next_window_id=None, current_summary={}, report=None, sound_alerts=[],
            opened_at=(row.source_observed_at or row.event_timestamp or row.ingestion_timestamp
                       or datetime.now(timezone.utc)), completed_at=None,
            report_generated_at=None, updated_at=datetime.now(timezone.utc))
        session.add(window)
    window.end_cohort_position = position
    window.lifecycle_status = "OPEN" if position == window_start else "COLLECTING"
    window.updated_at = datetime.now(timezone.utc)

    rows = session.execute(
        select(HybridCohortRound, CanonicalRound)
        .join(CanonicalRound, CanonicalRound.identity_key == HybridCohortRound.identity_key)
        .where(HybridCohortRound.cohort_id == cohort.cohort_id,
               HybridCohortRound.cohort_position.between(window_start, position))
        .order_by(HybridCohortRound.cohort_position)
    ).all()
    window_rounds = [_hybrid_round_dict(cohort_part, canonical)
                     for cohort_part, canonical in rows]
    previous_payload = ({"window_id": previous_window.window_id,
                         "report": previous_window.report or previous_window.current_summary}
                        if previous_window else None)
    complete = len(window_rounds) == COHORT_WINDOW_SIZE
    collector_health = (session.get(SystemHealth, "winner_predict_database")
                        or session.get(SystemHealth, "orbit_collector"))
    collector_state = collector_health.state if collector_health else "NOT_REPORTING"
    report = analyse_window(window_rounds, window_number=window_number,
                            cohort_id=cohort.cohort_id, previous=previous_payload,
                            completed=complete, collector_state=collector_state)
    confirmed_gaps = []
    for left, right in zip(window_rounds, window_rounds[1:]):
        if (left["source_name"] == right["source_name"]
                and left["source_sequence"] is not None and right["source_sequence"] is not None):
            gap = int(right["source_sequence"]) - int(left["source_sequence"]) - 1
            if gap > 0:
                confirmed_gaps.append({"after_identity_key": left["identity_key"],
                                       "before_identity_key": right["identity_key"],
                                       "confirmed_missing_source_sequence_count": gap,
                                       "source_name": right["source_name"]})
    coverage_flags = []
    if confirmed_gaps:
        coverage_flags.append("CONFIRMED_SOURCE_GAP")
    if collector_state not in {"HEALTHY", "CONNECTED", "COLLECTING"}:
        coverage_flags.append("COLLECTOR_UNHEALTHY_AT_REPORT")
    if any("cross_source_overlap_unknown" in part["order_quality"] for part in window_rounds):
        coverage_flags.append("CROSS_SOURCE_OVERLAP_UNKNOWN")
    report["coverage_flags"] = coverage_flags
    report["confirmed_source_gaps"] = confirmed_gaps
    report["completeness"] = "DEGRADED_COVERAGE" if coverage_flags else (
        "OBSERVED_100_COMPLETED" if complete else "COLLECTING")
    report["cohort_anchor"] = {
        "kind": "PROSPECTIVE_FIRST_ELIGIBLE_VERIFIED_ROUND_AFTER_ACTIVATION",
        "activated_at": cohort.activated_at.isoformat(), "position_is_provider_global": False,
    }
    rolling_rows = session.execute(
        select(HybridCohortRound, CanonicalRound)
        .join(CanonicalRound, CanonicalRound.identity_key == HybridCohortRound.identity_key)
        .where(HybridCohortRound.cohort_id == cohort.cohort_id,
               HybridCohortRound.cohort_position > max(0, position - 100))
        .order_by(HybridCohortRound.cohort_position)
    ).all()
    rolling = rolling_analysis([_hybrid_round_dict(cp, canonical)
                                for cp, canonical in rolling_rows])
    horizon_states = {key: item.get("state", "INSUFFICIENT_DATA")
                      for key, item in rolling["horizons"].items()}
    # This worker remains responsible for descriptive persistence only.
    # Live strategy transitions are committed by LiveStrategyProcessor at the
    # verified-round API boundary, before this analytics worker is scheduled.
    previous_confirmed_state = cohort.confirmed_state
    confirmed_state, candidate_state, candidate_count, state_changed = update_hysteresis(
        cohort.confirmed_state, cohort.candidate_state, rolling["state"], cohort.candidate_count)
    confirmed_momentum, candidate_momentum, momentum_count, momentum_changed = update_hysteresis(
        cohort.confirmed_momentum, cohort.candidate_momentum,
        rolling["momentum"], cohort.momentum_candidate_count)
    cohort.confirmed_state, cohort.candidate_state = confirmed_state, candidate_state
    cohort.candidate_count = candidate_count
    cohort.confirmed_momentum, cohort.candidate_momentum = confirmed_momentum, candidate_momentum
    cohort.momentum_candidate_count = momentum_count
    cohort.rolling_horizon_states = horizon_states
    _update_confirmed_high_state_test(
        session, cohort=cohort, row=row, position=position,
        previous_state=previous_confirmed_state, confirmed_state=confirmed_state,
        now=datetime.now(timezone.utc))
    rolling["confirmed_state"] = confirmed_state
    rolling["candidate_state"] = candidate_state
    rolling["state_change_confirmed"] = state_changed
    rolling["confirmed_momentum"] = confirmed_momentum
    rolling["momentum_change_confirmed"] = momentum_changed
    report["rolling_at_update"] = rolling
    if live_delivery:
        events = []
        if state_changed:
            event_type = ("behaviour.high" if confirmed_state == "HIGH_DOMINANT" else
                          "behaviour.low" if confirmed_state == "LOW_DOMINANT" else
                          "behaviour.balance" if confirmed_state == "BALANCED" else
                          "behaviour.transition")
            events.append((event_type, confirmed_state))
        if momentum_changed and confirmed_momentum in {"HIGH_RISING", "LOW_RISING"}:
            events.append(("behaviour.high_rising" if confirmed_momentum == "HIGH_RISING"
                           else "behaviour.low_rising", confirmed_momentum))
        if complete and window.report is None:
            events.append(("behaviour.window_completed", str(window_number)))
        for event_type, state_value in events:
            session.add(CollectorActivity(occurred_at=datetime.now(timezone.utc),
                event_type="hybrid_live_notification", details={
                    "event_id": f"{cohort.cohort_id}:{position}:{event_type}",
                    "event_type": event_type, "round_id": row.round_id,
                    "cohort_position": position, "state": state_value,
                    "window_number": window_number,
                }))
    window.current_summary = report
    if complete and window.report is None:
        now = datetime.now(timezone.utc)
        window.lifecycle_status = "REPORTED"
        window.completed_at = now
        window.report_generated_at = now
        report["lifecycle_status"] = "REPORTED"
        report["notification_events"] = [{
            "event_id": f"behaviour.window_completed:{window_id}",
            "event_type": "behaviour.window_completed", "published": False,
            "audibility_user_confirmed": False,
        }]
        window.report = report
        window.current_summary = report
        window.sound_alerts = report["notification_events"]
    session.flush()
    return {"cohort_id": cohort.cohort_id, "position": position,
            "window_id": window_id, "window_number": window_number,
            "window_completed": complete,
            "state_changed": state_changed, "momentum_changed": momentum_changed}


def analyse_pending(engine, *, limit: int = 500, state_confirm_rounds: int = 3,
                    min_state_rounds: int = 20, gap_seconds: float = 180.0452) -> int:
    processed = 0
    with Session(engine, expire_on_commit=False) as session:
        with session.begin():
            checkpoint = session.get(WorkerCheckpoint, "analysis")
            last_position = checkpoint.last_position if checkpoint else 0
            pending = session.scalars(select(CanonicalRound).where(
                CanonicalRound.analysis_position > last_position
            ).order_by(CanonicalRound.analysis_position).limit(limit)).all()
    if not pending:
        return 0
    # Load the Phase 1 reference once per worker pass. New completed blocks are
    # appended in-memory and durably in the same per-round transaction.
    with Session(engine) as session:
        reference_blocks = [
            {"block_number": b.block_number, "first_position": b.first_position,
             "last_position": b.last_position, "high_count": b.high_count,
             "low_count": b.low_count, "exact_count": b.exact_count,
             "high_rate_nonexact": float(b.high_rate_nonexact),
             "sequence": b.sequence_stats}
            for b in session.scalars(select(ForecastReferenceBlock).order_by(ForecastReferenceBlock.block_number)).all()
        ]
        research_import = session.scalar(select(ResearchImport).order_by(
            ResearchImport.snapshot_utc.desc()).limit(1))
        if research_import:
            snapshot_id = research_import.snapshot_id
            cache_key = research_import.input_files_sha256 or research_import.source_sha256
            recovery_reference = cached_recovery_reference(
                cache_key,
                lambda: session.scalars(select(CanonicalRound.outcome).where(
                    CanonicalRound.import_snapshot_id == snapshot_id
                ).order_by(CanonicalRound.analysis_position)).all())
            phase1_counts = {"H": research_import.high_count, "L": research_import.low_count,
                             "E": research_import.exact_count,
                             "rounds": research_import.canonical_rounds}
            phase1_high_rate = research_import.high_count / max(
                1, research_import.high_count + research_import.low_count)
        else:
            recovery_reference = None
            phase1_counts = None
            phase1_high_rate = None
    for candidate in pending:
        try:
            with Session(engine, expire_on_commit=False) as session:
                with session.begin():
                    if engine.dialect.name == "postgresql":
                        session.connection().exec_driver_sql(
                            "SELECT pg_advisory_xact_lock(hashtext('advanced-crash-behaviour-analysis-write'))")
                    persisted = session.get(CanonicalRound, candidate.identity_key)
                    checkpoint = session.get(WorkerCheckpoint, "analysis")
                    if persisted is None or (checkpoint and persisted.analysis_position <= checkpoint.last_position):
                        continue
                    expected_position = (checkpoint.last_position + 1) if checkpoint else 1
                    if persisted.analysis_position != expected_position:
                        raise RuntimeError(
                            f"analysis position gap: expected {expected_position}, found {persisted.analysis_position}")
                    process_round(session, persisted, reference_blocks,
                                  state_confirm_rounds=state_confirm_rounds,
                                  min_state_rounds=min_state_rounds,
                                  gap_seconds=gap_seconds,
                                  recovery_reference=recovery_reference,
                                  phase1_high_rate=phase1_high_rate,
                                  phase1_counts=phase1_counts)
                    # The hybrid cohort is additive and isolated from the
                    # legacy state/rule and SHADOW decision paths.
                    inserted_at = (persisted.inserted_at.replace(tzinfo=timezone.utc)
                                   if persisted.inserted_at.tzinfo is None
                                   else persisted.inserted_at.astimezone(timezone.utc))
                    stored_at = (persisted.ingestion_timestamp or persisted.source_observed_at)
                    stored_at = (stored_at.replace(tzinfo=timezone.utc) if stored_at.tzinfo is None
                                 else stored_at.astimezone(timezone.utc)) if stored_at else None
                    checked_at = datetime.now(timezone.utc)
                    live_delivery = (len(pending) == 1
                        and persisted.source_name == "winner_predict_database"
                        and persisted.order_quality == "winner_predict_canonical_order_verified"
                        and 0 <= (checked_at - inserted_at).total_seconds() <= 30
                        and stored_at is not None
                        and -2 <= (checked_at - stored_at).total_seconds() <= 60)
                    process_hybrid_round(session, persisted, live_delivery=live_delivery)
                    session.merge(SystemHealth(component="analysis_worker", state="HEALTHY",
                                               checked_at=datetime.now(timezone.utc),
                                               details={"last_position": persisted.analysis_position,
                                                        "last_round": persisted.identity_key}))
                    session.add(CollectorActivity(occurred_at=datetime.now(timezone.utc),
                                                  event_type="analysis_round_committed",
                                                  details={"position": persisted.analysis_position,
                                                           "identity_key": persisted.identity_key}))
            processed += 1
        except Exception:
            log.exception("analysis failed at position=%s; round remains durable and worker will retry",
                          candidate.analysis_position)
            raise
    return processed


def _publish_behaviour_eligibility(engine) -> None:
    """Publish eligibility facts; Rules Engine creates and settles signals."""
    import json
    import urllib.error
    import urllib.request
    from . import settings

    with Session(engine, expire_on_commit=False) as session:
        with session.begin():
            machine_row = session.scalars(select(HighDominanceSeriesState)
                .order_by(HighDominanceSeriesState.updated_at.desc()).limit(1)).first()
            machine = deepcopy(machine_row.payload) if machine_row else None
            pending_intents = (machine or {}).get("pending_strategy_eligibilities") or []
            intent = (pending_intents[0] if pending_intents else
                      (machine or {}).get("latest_strategy_eligibility"))
            if (not isinstance(machine, dict)
                    or machine.get("provenance") != "LIVE_VERIFIED"
                    or machine.get("state") == "UNSAFE_STATE"):
                return
            if not isinstance(intent, dict) or intent.get("status") not in {
                    "ELIGIBLE", "UNPUBLISHED", "PUBLISHING"}:
                return
            intent["status"] = "PUBLISHING"
            intent["attempt_count"] = int(intent.get("attempt_count") or 0) + 1
            intent["last_attempt_at"] = datetime.now(timezone.utc).isoformat()
            machine_row.payload = machine
            machine_row.updated_at = datetime.now(timezone.utc)
            payload = {key: intent[key] for key in (
                "eligibility_id", "strategy_id", "series_id", "part", "batch_id",
                "trigger_round_identity", "decision_round_identity", "source_round_id",
                "target_sequence_boundary", "direction", "decision_timestamp", "current_dominance_state",
                "configuration_version",
                "eligibility_reason",
                "trigger_horizon", "policy_version", "verified_data_freshness", "eligibility")}
            eligibility_id = str(intent["eligibility_id"])
    headers = {"Content-Type": "application/json"}
    if settings.SHADOW_BACKEND_API_KEY:
        headers["Authorization"] = f"Bearer {settings.SHADOW_BACKEND_API_KEY}"
    if settings.SHADOW_SHARED_SECRET:
        headers["X-Behaviour-Shadow-Secret"] = settings.SHADOW_SHARED_SECRET
    request = urllib.request.Request(
        settings.SHADOW_BACKEND_URL + "/api/v1/internal/behaviour/strategy-eligibility",
        data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        headers=headers, method="POST")
    result: dict | None = None
    terminal_status: str | None = None
    request_started_ns = time.perf_counter_ns()
    try:
        with urllib.request.urlopen(request, timeout=2.0) as response:
            result = json.loads(response.read().decode("utf-8"))
            terminal_status = "ACCEPTED" if result.get("status") in {
                "ACCEPTED", "ALREADY_ACCEPTED", "SETTLED_WIN", "SETTLED_LOSS",
                "ELIGIBILITY_RECORDED", "NOT_SELECTED"} else "REJECTED"
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read().decode("utf-8"))
        except Exception:
            detail = {"error": "backend_http_error", "status": exc.code}
        result = {"status": "REJECTED", "detail": detail}
        # 499 is the backend's explicit client-disconnect response. It says
        # nothing about eligibility validity, so keep the same fact retryable.
        terminal_status = "REJECTED" if 400 <= exc.code < 500 and exc.code != 499 else None
    except Exception:
        # Keep the eligibility fact retryable with the same identity.
        log.info("Behaviour eligibility delivery deferred id=%s", eligibility_id, exc_info=True)
        return {"status": "DEFERRED", "eligibility_id": eligibility_id,
                "behaviour_to_rules_response_ms":
                    (time.perf_counter_ns() - request_started_ns) / 1_000_000}
    if terminal_status is None:
        return {"status": "DEFERRED", "eligibility_id": eligibility_id,
                "response": result, "behaviour_to_rules_response_ms":
                    (time.perf_counter_ns() - request_started_ns) / 1_000_000}
    behaviour_to_rules_response_ms = (time.perf_counter_ns() - request_started_ns) / 1_000_000
    with Session(engine, expire_on_commit=False) as session:
        with session.begin():
            machine_row = session.scalars(select(HighDominanceSeriesState)
                .order_by(HighDominanceSeriesState.updated_at.desc()).limit(1)).first()
            machine = deepcopy(machine_row.payload) if machine_row else None
            latest = machine.get("latest_strategy_eligibility")
            if isinstance(latest, dict) and latest.get("eligibility_id") == eligibility_id:
                latest["status"] = "PUBLISHED" if terminal_status == "ACCEPTED" else terminal_status
                latest["response"] = result
                latest["resolved_at"] = datetime.now(timezone.utc).isoformat()
                latest["behaviour_to_rules_response_ms"] = behaviour_to_rules_response_ms
                signal = result.get("signal") if isinstance(result, dict) else None
                if isinstance(signal, dict):
                    latest["signal_id"] = signal.get("signal_id")
                    latest["session_id"] = result.get("session_id")
                    latest["target_round_id"] = signal.get("target_round_id")
                    latest["signal_status"] = result.get("signal_lock_status") or terminal_status
                    latest["signal_persistence"] = result.get("persistence")
                    latest["signal_accepted_at"] = result.get("signal_accepted_at")
                    latest["signal_locked_at"] = result.get("signal_locked_at")
                    latest["lock_acknowledged_at"] = result.get("lock_acknowledged_at")
                    latest["rules_engine_acceptance_to_durable_lock_ms"] = result.get(
                        "rules_engine_acceptance_to_durable_lock_ms")
                    lock_status = str(result.get("signal_lock_status") or "")
                    if (terminal_status == "ACCEPTED"
                            and lock_status in {"DURABLY_LOCKED", "EXISTING_DURABLE_SIGNAL"}
                            and signal.get("signal_id")):
                        locked_signals = machine.setdefault("locked_signals", {})
                        locked_signals[str(signal["signal_id"])] = {
                            "signal_id": str(signal["signal_id"]),
                            "session_id": str(result.get("session_id") or signal.get("session_id") or ""),
                            "series_id": str(latest.get("series_id") or ""),
                            "part": str(latest.get("part") or ""),
                            "batch_id": str(latest.get("batch_id") or ""),
                            "configuration_version": int(latest.get("configuration_version") or 0),
                            "target_round_id": str(signal.get("target_round_id") or ""),
                            "target_stream_id": str(signal.get("target_stream_id") or ""),
                            "target_round_index": int(signal.get("target_round_index") or 0),
                            "lock_status": lock_status,
                            "locked_at": result.get("lock_acknowledged_at") or signal.get("timestamp"),
                            "settlement_status": str(signal.get("settlement_status") or "PENDING"),
                        }
                        if len(locked_signals) > 5000:
                            settled = [key for key, value in locked_signals.items()
                                       if value.get("settlement_status") == "COMMITTED"]
                            for key in settled[:-4000]:
                                locked_signals.pop(key, None)
                event_type = ("STRATEGY_ELIGIBILITY_PUBLISHED" if terminal_status == "ACCEPTED"
                              else "STRATEGY_ELIGIBILITY_REJECTED")
                machine.setdefault("events", []).append({
                    "type": event_type, "round_identity": latest.get("decision_round_identity"),
                    "round_id": latest.get("source_round_id"), "timestamp": latest["resolved_at"],
                    "eligibility_id": eligibility_id})
                machine["events"] = machine["events"][-500:]
                session.add(CollectorActivity(occurred_at=datetime.now(timezone.utc),
                    event_type="hybrid_live_notification", details={
                        "event_id": f"{machine_row.cohort_id}:{event_type}:{eligibility_id}",
                        "event_type": ("behaviour.strategy_eligibility_published" if terminal_status == "ACCEPTED"
                                       else "behaviour.strategy_eligibility_rejected"),
                        "round_id": latest.get("source_round_id"), "eligibility_id": eligibility_id}))
            pending = machine.get("pending_strategy_eligibilities") or []
            machine["pending_strategy_eligibilities"] = [item for item in pending
                if item.get("eligibility_id") != eligibility_id]
            if machine_row:
                machine_row.payload = machine
                machine_row.updated_at = datetime.now(timezone.utc)
    return {"status": terminal_status, "eligibility_id": eligibility_id,
            "response": result,
            "behaviour_to_rules_response_ms": behaviour_to_rules_response_ms}


def quarantine_unverifiable_high_series(engine) -> int:
    """Fail closed on active legacy series that lack live provenance."""
    active_states = {"PART1_COLLECTING", "PART1_COMPLETED", "PART2_ACTIVE",
                     "PART2_PAUSED_LOW_DOMINANT", "PART2_PAUSED_NON_HIGH"}
    changed = 0
    with Session(engine, expire_on_commit=False) as session, session.begin():
        rows = session.scalars(select(HighDominanceSeriesState).with_for_update()).all()
        for row in rows:
            machine = deepcopy(row.payload or {})
            if (machine.get("state") not in active_states
                    or machine.get("provenance") == "LIVE_VERIFIED"):
                continue
            machine["quarantined_series"] = deepcopy(machine.get("series"))
            machine["quarantine_reason"] = "ACTIVE_SERIES_LACKS_VERIFIABLE_LIVE_PROVENANCE"
            machine["state"] = "UNSAFE_STATE"
            machine["data_health"] = "DATA_STALE"
            machine["provenance"] = "UNVERIFIABLE"
            eligibility = machine.get("latest_strategy_eligibility")
            if isinstance(eligibility, dict) and eligibility.get("status") in {
                    "ELIGIBLE", "UNPUBLISHED", "PUBLISHING"}:
                eligibility["status"] = "UNSAFE_QUARANTINED"
            machine.setdefault("events", []).append({
                "type": "ACTIVE_SERIES_QUARANTINED_UNVERIFIABLE_PROVENANCE",
                "round_identity": machine.get("last_processed_identity"),
                "round_id": None, "position": machine.get("last_processed_position"),
                "timestamp": datetime.now(timezone.utc).isoformat()})
            machine["events"] = machine["events"][-500:]
            row.payload = machine
            row.updated_at = datetime.now(timezone.utc)
            changed += 1
    return changed


def run_worker(database_url: str, *, poll_seconds: float = 1.0, once: bool = False,
               state_confirm_rounds: int = 3, min_state_rounds: int = 20,
               gap_seconds: float = 180.0452) -> None:
    from .db import make_engine, migrate
    engine = make_engine(database_url)
    migrate(engine)
    quarantined = quarantine_unverifiable_high_series(engine)
    if quarantined:
        log.error("[HIGH_SERIES] quarantined active series without verified live provenance count=%s",
                  quarantined)
    lock_connection = None
    worker_lock_acquired = False
    try:
        if engine.dialect.name == "postgresql":
            lock_connection = engine.connect()
            worker_lock_acquired = bool(lock_connection.exec_driver_sql(
                "SELECT pg_try_advisory_lock(hashtext('advanced-crash-behaviour-analysis-worker'))"
            ).scalar())
            if not worker_lock_acquired:
                raise RuntimeError("an independent Behaviour analysis worker is already running")
        with Session(engine) as session, session.begin():
            checkpoint = session.get(WorkerCheckpoint, "analysis")
            session.merge(SystemHealth(component="analysis_worker", state="HEALTHY",
                                       checked_at=datetime.now(timezone.utc),
                                       details={"status": "RUNNING",
                                                "last_position": checkpoint.last_position if checkpoint else 0}))
        log.info("analysis worker starting with independent behaviour database")
        while True:
            try:
                count = analyse_pending(engine, state_confirm_rounds=state_confirm_rounds,
                                        min_state_rounds=min_state_rounds, gap_seconds=gap_seconds)
                if once:
                    return
                if count == 0:
                    _expire_high_series_time_limits(engine)
                    with Session(engine) as session, session.begin():
                        checkpoint = session.get(WorkerCheckpoint, "analysis")
                        session.merge(SystemHealth(component="analysis_worker", state="HEALTHY",
                                                   checked_at=datetime.now(timezone.utc),
                                                   details={"status": "IDLE",
                                                            "last_position": checkpoint.last_position if checkpoint else 0}))
                    time.sleep(max(.25, poll_seconds))
            except KeyboardInterrupt:
                raise
            except Exception as exc:
                log.exception("analysis worker pass failed; durable rounds remain pending for retry")
                try:
                    with Session(engine) as session, session.begin():
                        session.merge(SystemHealth(component="analysis_worker", state="ERROR",
                                                   checked_at=datetime.now(timezone.utc),
                                                   details={"exception_type": type(exc).__name__,
                                                            "retrying": not once}))
                except Exception:
                    log.exception("could not persist analysis worker error status")
                if once:
                    raise
                time.sleep(min(30.0, max(1.0, poll_seconds)))
    finally:
        if lock_connection is not None:
            if worker_lock_acquired:
                try:
                    lock_connection.exec_driver_sql(
                        "SELECT pg_advisory_unlock(hashtext('advanced-crash-behaviour-analysis-worker'))")
                except Exception:
                    log.exception("could not release analysis worker advisory lock")
            lock_connection.close()
        engine.dispose()


def _expire_high_series_time_limits(engine) -> None:
    """Apply optional wall-clock pause safety without creating a round result."""
    now = datetime.now(timezone.utc)
    paused_states = {"PART2_PAUSED_LOW_DOMINANT", "PART2_PAUSED_NON_HIGH"}
    with Session(engine, expire_on_commit=False) as session, session.begin():
        rows = session.scalars(select(HighDominanceSeriesState)).all()
        for record in rows:
            state = deepcopy(record.payload or {})
            timeout = (state.get("config") or {}).get("pause_timeout_seconds")
            series = state.get("series") or {}
            part2 = series.get("part2") or {}
            started = part2.get("pause_started_at")
            if state.get("state") not in paused_states or not timeout or not started:
                continue
            try:
                began = datetime.fromisoformat(started.replace("Z", "+00:00"))
                if began.tzinfo is None:
                    began = began.replace(tzinfo=timezone.utc)
            except (AttributeError, ValueError):
                continue
            if (now - began).total_seconds() < int(timeout):
                continue
            state["state"] = "COOLDOWN"
            series["stop_reason"] = "STOPPED_TIME_LIMIT"
            series["terminal_state"] = "STOPPED_TIME_LIMIT"
            series["last_pause_reason"] = part2.get("pause_reason")
            series["terminal_round_identity"] = part2.get("pause_started_round_identity")
            series["terminal_at"] = now.isoformat()
            part2.setdefault("pause_intervals", []).append({
                "reason": part2.get("pause_reason"),
                "start_round_identity": part2.get("pause_started_round_identity"),
                "start_timestamp": part2.get("pause_started_at"),
                "end_round_identity": part2.get("pause_started_round_identity"),
                "end_timestamp": now.isoformat(), "verified_rounds": part2.get("pause_count", 0),
                "terminal_reason": "STOPPED_TIME_LIMIT"})
            part2["pause_started_at"] = None
            state["cooldown_progress"] = 0
            state["cooldown_started_at"] = now.isoformat()
            state.setdefault("events", []).extend([
                {"type": "STOPPED_TIME_LIMIT", "round_identity": part2.get("pause_started_round_identity"),
                 "round_id": None, "position": None, "timestamp": now.isoformat()},
                {"type": "COOLDOWN_STARTED", "round_identity": part2.get("pause_started_round_identity"),
                 "round_id": None, "position": None, "timestamp": now.isoformat()},
            ])
            state["events"] = state["events"][-500:]
            record.payload = state
            record.updated_at = now
