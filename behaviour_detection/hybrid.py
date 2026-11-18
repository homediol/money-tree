"""Past-only descriptive dominance windows for the independent Behaviour service.

Policy v1 uses a symmetric 60/40 dominance boundary, excludes EXACT from the
H/L denominator, and requires at least half of each observed horizon to be
non-EXACT (minimum four). These states describe completed observations; they
are not forecasts or validated betting signals.
"""
from __future__ import annotations

import math
import statistics
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Mapping, Sequence

POLICY_VERSION = "hybrid-dominance-v1"
ROLLING_HORIZONS = (5, 10, 20, 30, 50, 100)
COHORT_WINDOW_SIZE = 100
DOMINANCE_HIGH = 0.60
DOMINANCE_LOW = 0.40
MIN_NONEXACT_SHARE = 0.50
MIN_NONEXACT = 4
HYSTERESIS_ROUNDS = 3
HIGH_DOMINANCE_2X_BATCH_SIZE = 5
HIGH_DOMINANCE_2X_POLICY_VERSION = "rolling-high-dominance-part2-resumable-windows-v6"
HIGH_DOMINANCE_STATE_TEST_POLICY_VERSION = "confirmed-high-forward-checkpoints-5-10-30-50-v1"
HIGH_DOMINANCE_STATE_TEST_CHECKPOINTS = (5, 10, 30, 50)
HISTORICAL_HIGH_RATE = 25966 / (25966 + 27398)
PHASE1_P99_GAP_SECONDS = 180.0452
AUTHORITATIVE_ORDER_QUALITIES = frozenset({
    "winner_predict_canonical_order_verified",
    "verified_orbit_delta_aligned_to_phase1",
})


def _counts(symbols: Sequence[str]) -> dict:
    counts = Counter(symbols)
    return {"H": counts["H"], "L": counts["L"], "E": counts["E"]}


def _wilson(successes: int, trials: int, z: float = 1.959963984540054) -> list[float] | None:
    if trials <= 0:
        return None
    p = successes / trials
    den = 1 + z * z / trials
    center = (p + z * z / (2 * trials)) / den
    margin = z * math.sqrt((p * (1 - p) / trials) + z * z / (4 * trials * trials)) / den
    return [max(0.0, center - margin), min(1.0, center + margin)]


def _dominance(counts: Mapping[str, int], required: int) -> dict:
    high, low, exact = (int(counts.get(symbol, 0)) for symbol in ("H", "L", "E"))
    nonexact = high + low
    support = high + low + exact
    minimum = max(MIN_NONEXACT, math.ceil(required * MIN_NONEXACT_SHARE))
    high_share = high / nonexact if nonexact else None
    ci = _wilson(high, nonexact)
    if support < required or nonexact < minimum or high_share is None:
        state = "INSUFFICIENT_DATA"
    elif high_share >= DOMINANCE_HIGH:
        state = "HIGH_DOMINANT"
    elif high_share <= DOMINANCE_LOW:
        state = "LOW_DOMINANT"
    else:
        state = "BALANCED"
    return {
        "state": state, "rounds": support, "required_rounds": required,
        "nonexact_rounds": nonexact, "minimum_nonexact_rounds": minimum,
        "counts": {"H": high, "L": low, "E": exact},
        "high_share_nonexact": high_share,
        "low_share_nonexact": low / nonexact if nonexact else None,
        "exact_share_all": exact / support if support else None,
        "high_share_wilson_95": ci,
        "baseline_high_share_nonexact": HISTORICAL_HIGH_RATE,
        "baseline_deviation_pp": ((high_share - HISTORICAL_HIGH_RATE) * 100
                                   if high_share is not None else None),
        "confidence_interval_excludes_descriptive_baseline": bool(
            ci and (ci[0] > HISTORICAL_HIGH_RATE or ci[1] < HISTORICAL_HIGH_RATE)),
        "inferential_status": "DESCRIPTIVE_ONLY_NOT_A_VALIDATED_PREDICTIVE_SIGNAL",
    }


def _run_statistics(symbols: Sequence[str]) -> dict:
    runs = {"H": [], "L": [], "E": []}
    current = None
    length = 0
    for symbol in symbols:
        if symbol == current:
            length += 1
            continue
        if current is not None:
            runs[current].append(length)
        current, length = symbol, 1
    if current is not None:
        runs[current].append(length)
    return {
        "run_count": sum(len(items) for items in runs.values()),
        "run_lengths": runs,
        "max_run": {symbol: max(lengths, default=0) for symbol, lengths in runs.items()},
        "current_streak": {"symbol": current, "length": length if current else 0},
    }


def _interrupted_clusters(symbols: Sequence[str], dominant: str) -> dict:
    """Group same-direction runs separated by one to three opposite rounds."""
    clusters = []
    current = None
    gap = 0
    for index, symbol in enumerate(symbols, 1):
        if symbol == dominant:
            if current is None:
                current = {"start_offset": index, "end_offset": index,
                           "dominant_rounds": 1, "interruptions": []}
            elif gap == 0 or gap <= 3:
                if gap:
                    current["interruptions"].append(gap)
                current["dominant_rounds"] += 1
                current["end_offset"] = index
            else:
                _finish_cluster(current, clusters)
                current = {"start_offset": index, "end_offset": index,
                           "dominant_rounds": 1, "interruptions": []}
            gap = 0
        elif symbol == "E":
            if current is not None:
                _finish_cluster(current, clusters)
                current = None
            gap = 0
        elif current is not None:
            gap += 1
            if gap > 3:
                _finish_cluster(current, clusters)
                current = None
                gap = 0
    if current is not None:
        _finish_cluster(current, clusters)
    return {
        "dominant_symbol": dominant, "cluster_count": len(clusters),
        "interrupted_cluster_count": sum(bool(item["interruptions"]) for item in clusters),
        "interruption_round_counts": dict(Counter(
            gap for item in clusters for gap in item["interruptions"])),
        "clusters": clusters,
        "description": "Clusters bridge one to three opposite H/L rounds; EXACT terminates a cluster.",
    }


def _finish_cluster(cluster: dict, output: list[dict]) -> None:
    span = cluster["end_offset"] - cluster["start_offset"] + 1
    cluster["span_rounds"] = span
    cluster["density"] = cluster["dominant_rounds"] / span
    cluster["interruption_count"] = len(cluster["interruptions"])
    output.append(cluster)


def _slice_summary(rounds: Sequence[Mapping], start: int, end: int, label: str) -> dict:
    selected = rounds[start:end]
    counts = _counts([str(row["outcome"]) for row in selected])
    state = _dominance(counts, len(selected))
    state.update({"label": label, "start_offset": start + 1,
                  "end_offset": end, "observed_rounds": len(selected),
                  "run_structure": _run_statistics([str(row["outcome"]) for row in selected])})
    return state


def _window_transition(opening: str, closing: str) -> str:
    return f"{opening.removesuffix('_DOMINANT')} → {closing.removesuffix('_DOMINANT')}"


def analyse_window(rounds: Sequence[Mapping], *, window_number: int,
                   cohort_id: str, previous: Mapping | None = None,
                   completed: bool = False, collector_state: str | None = None) -> dict:
    symbols = [str(row["outcome"]) for row in rounds]
    n = len(rounds)
    counts = _counts(symbols)
    opening = {str(width): _slice_summary(rounds, 0, min(width, n), f"opening_{width}")
               for width in (5, 10, 20)}
    closing = {str(width): _slice_summary(rounds, max(0, n - width), n, f"closing_{width}")
               for width in (5, 10, 20)}
    middle = _slice_summary(rounds, max(0, n // 2 - 10), min(n, n // 2 + 10), "middle_20")
    transitions = []
    for offset, left, right in zip(range(1, n), symbols, symbols[1:]):
        if left != right:
            transitions.append({"cohort_offset": offset, "from": left, "to": right})
    start = rounds[0] if rounds else {}
    end = rounds[-1] if rounds else {}
    if all(row.get("event_timestamp") and row.get("timestamp_quality") == "platform_event_time"
           for row in rounds):
        start_at, end_at, duration_role = start.get("event_timestamp"), end.get("event_timestamp"), "platform_event_time"
    elif all(row.get("source_observed_at") for row in rounds):
        start_at, end_at, duration_role = start.get("source_observed_at"), end.get("source_observed_at"), "source_observed_at"
    elif all(row.get("ingestion_timestamp") for row in rounds):
        start_at, end_at, duration_role = start.get("ingestion_timestamp"), end.get("ingestion_timestamp"), "ingestion_timestamp"
    else:
        start_at = end_at = duration_role = None
    duration = None
    if start_at and end_at:
        duration = max(0.0, (as_utc(end_at) - as_utc(start_at)).total_seconds())
    opening_state = opening["20"]["state"] if n >= 20 else opening[str(min((5, 10, 20), key=lambda w: abs(w - n)))]["state"]
    closing_state = closing["20"]["state"] if n >= 20 else closing[str(min((5, 10, 20), key=lambda w: abs(w - n)))]["state"]
    prev_report = previous.get("report") if previous else None
    previous_closing = ((prev_report or {}).get("closing_state") if previous else None)
    window_id = f"{cohort_id}:{window_number:06d}"
    report = {
        "policy_version": POLICY_VERSION,
        "window_id": window_id, "cohort_id": cohort_id, "window_number": window_number,
        "lifecycle_status": "REPORTED" if completed else "COLLECTING",
        "coverage_status": "OBSERVED_100_COMPLETED" if completed else "OBSERVED_PARTIAL",
        "complete": completed, "verified_round_count": n, "required_round_count": 100,
        "start_cohort_position": int(start.get("cohort_position") or 0),
        "end_cohort_position": int(end.get("cohort_position") or 0),
        "start_identity_key": start.get("identity_key"), "end_identity_key": end.get("identity_key"),
        "start_round_id": start.get("round_id"), "end_round_id": end.get("round_id"),
        "first_analysis_position": start.get("analysis_position"),
        "last_analysis_position": end.get("analysis_position"),
        "start_timestamp": as_utc(start_at).isoformat() if start_at else None,
        "end_timestamp": as_utc(end_at).isoformat() if end_at else None,
        "duration_seconds": duration,
        "duration_timestamp_role": duration_role,
        "timestamp_roles": sorted({str(row.get("timestamp_role")) for row in rounds
                                    if row.get("timestamp_role")}),
        "counts": counts,
        "high_share_nonexact": counts["H"] / (counts["H"] + counts["L"])
            if counts["H"] + counts["L"] else None,
        "opening": opening, "middle": middle, "closing": closing,
        "opening_state": opening_state, "closing_state": closing_state,
        "observed_window_transition": _window_transition(opening_state, closing_state),
        "dominance_transitions": transitions,
        "runs": _run_statistics(symbols),
        "high_clusters": _interrupted_clusters(symbols, "H"),
        "low_clusters": _interrupted_clusters(symbols, "L"),
        "previous_window_id": previous.get("window_id") if previous else None,
        "next_window_id": None,
        "relationship_to_previous": _window_relationship(previous_closing, opening_state),
        "collector_state_at_report": collector_state,
        "continuity": {"local_sequence_contiguous": True,
                       "provider_global_round_ids_invented": False},
        "notification_events": [],
        "limitations": [
            "Observed classifications describe this cohort and do not predict future rounds.",
            "Time estimates do not confirm missing outcomes or provider rounds.",
            "The historical baseline is descriptive and not a forecast guarantee.",
        ],
    }
    if prev_report:
        old_high = prev_report.get("high_clusters") or {}
        old_low = prev_report.get("low_clusters") or {}
        report["cluster_change_from_previous"] = {
            "high_cluster_count_delta": report["high_clusters"]["cluster_count"] - int(old_high.get("cluster_count", 0)),
            "low_cluster_count_delta": report["low_clusters"]["cluster_count"] - int(old_low.get("cluster_count", 0)),
            "high_interrupted_cluster_delta": report["high_clusters"]["interrupted_cluster_count"] - int(old_high.get("interrupted_cluster_count", 0)),
            "low_interrupted_cluster_delta": report["low_clusters"]["interrupted_cluster_count"] - int(old_low.get("interrupted_cluster_count", 0)),
            "descriptive_only": True,
        }
    else:
        report["cluster_change_from_previous"] = None
    return report


def _window_relationship(previous_closing: str | None, current_opening: str) -> dict:
    if not previous_closing:
        return {"status": "FIRST_WINDOW", "previous_closing_state": None,
                "current_opening_state": current_opening, "classification": None}
    left = previous_closing.removesuffix("_DOMINANT")
    right = current_opening.removesuffix("_DOMINANT")
    if "INSUFFICIENT" in left or "INSUFFICIENT" in right:
        state = "INSUFFICIENT_OPENING_SUPPORT"
    elif left == right:
        state = f"{left}_CONTINUED"
    elif left == "BALANCED":
        state = f"BALANCED_TO_{right}"
    elif right == "BALANCED":
        state = f"{left}_TO_BALANCED"
    else:
        state = f"{left}_TO_{right}"
    return {"status": state, "previous_closing_state": previous_closing,
            "current_opening_state": current_opening, "classification": state,
            "predictive_evidence": False}


def rolling_analysis(rounds: Sequence[Mapping]) -> dict:
    out = {}
    for width in ROLLING_HORIZONS:
        selected = rounds[-width:]
        counts = _counts([str(row["outcome"]) for row in selected])
        state = _dominance(counts, width)
        if len(selected) < width:
            state["state"] = "INSUFFICIENT_DATA"
        symbols = [str(row["outcome"]) for row in selected]
        state.update({"horizon": width,
                      "high_runs": _run_statistics(symbols)["max_run"]["H"],
                      "low_runs": _run_statistics(symbols)["max_run"]["L"],
                      "high_clusters": _interrupted_clusters(symbols, "H"),
                      "low_clusters": _interrupted_clusters(symbols, "L")})
        out[str(width)] = state
    wide = out["50"]["state"] if out["50"]["state"] != "INSUFFICIENT_DATA" else out["20"]["state"]
    short = out["20"]["state"]
    if short == "INSUFFICIENT_DATA":
        state = "INSUFFICIENT_DATA"
    elif short in {"HIGH_DOMINANT", "LOW_DOMINANT"} and wide in {"HIGH_DOMINANT", "LOW_DOMINANT"} and short != wide:
        state = "TRANSITION"
    elif short in {"HIGH_DOMINANT", "LOW_DOMINANT"} and wide == "BALANCED":
        state = "TRANSITION"
    elif short == "BALANCED" and wide in {"HIGH_DOMINANT", "LOW_DOMINANT"}:
        state = wide
    else:
        state = short
    for short_width, long_width in ((5, 10), (10, 20), (20, 30), (30, 50), (50, 100)):
        short_share = out[str(short_width)]["high_share_nonexact"]
        long_share = out[str(long_width)]["high_share_nonexact"]
        share_delta = short_share - long_share if short_share is not None and long_share is not None else None
        if share_delta is None:
            horizon_momentum = "INSUFFICIENT_DATA"
        elif share_delta >= .10:
            horizon_momentum = "HIGH_RISING"
        elif share_delta <= -.10:
            horizon_momentum = "LOW_RISING"
        elif abs(share_delta) < .04:
            horizon_momentum = "STABLE"
        else:
            horizon_momentum = "TRANSITION"
        out[str(short_width)]["momentum_vs_next_horizon"] = horizon_momentum
        out[str(short_width)]["high_share_delta_vs_next_horizon"] = share_delta
    out["100"]["momentum_vs_next_horizon"] = "LONGEST_HORIZON"
    out["100"]["high_share_delta_vs_next_horizon"] = None
    high10, high50 = out["10"]["high_share_nonexact"], out["50"]["high_share_nonexact"]
    delta = high10 - high50 if high10 is not None and high50 is not None and out["50"]["state"] != "INSUFFICIENT_DATA" else None
    if delta is None:
        momentum = "INSUFFICIENT_DATA"
    elif delta >= 0.10:
        momentum = "LOW_WEAKENING" if state == "LOW_DOMINANT" else "HIGH_RISING"
    elif delta <= -0.10:
        momentum = "HIGH_WEAKENING" if state == "HIGH_DOMINANT" else "LOW_RISING"
    elif abs(delta) < 0.04:
        momentum = "STABLE"
    else:
        momentum = "TRANSITION"
    strength = ("STRONG_DESCRIPTIVE" if state in {"HIGH_DOMINANT", "LOW_DOMINANT"}
                and out["50"]["state"] == state
                and max(out["50"]["high_share_nonexact"] or 0,
                        out["50"]["low_share_nonexact"] or 0) >= 0.65
                else "STABLE_DESCRIPTIVE" if state in {"HIGH_DOMINANT", "LOW_DOMINANT"}
                else "EMERGING" if state == "TRANSITION" else state)
    return {"policy_version": POLICY_VERSION, "state": state, "momentum": momentum,
            "strength": strength, "short_vs_long_high_share_delta": delta,
            "horizons": out,
            "thresholds": {"high_dominant_at_or_above": DOMINANCE_HIGH,
                           "low_dominant_at_or_below": DOMINANCE_LOW,
                           "minimum_nonexact_fraction": MIN_NONEXACT_SHARE,
                           "minimum_nonexact_count": MIN_NONEXACT,
                           "hysteresis_rounds": HYSTERESIS_ROUNDS},
            "descriptive_only": True}


def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _quantile(values: Sequence[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(float(value) for value in values)
    position = (len(ordered) - 1) * quantile
    low, high = math.floor(position), math.ceil(position)
    return ordered[low] + (ordered[high] - ordered[low]) * (position - low)


def _cadence(samples: Sequence[tuple[datetime | None, str, int, str]]) -> dict:
    gaps = []
    roles = set()
    ties = 0
    previous = None
    for timestamp, role, position, source in samples:
        if timestamp is None:
            previous = None
            continue
        if previous and previous[1] == role and previous[3] == source and position == previous[2] + 1:
            seconds = (as_utc(timestamp) - as_utc(previous[0])).total_seconds()
            if seconds > 0:
                gaps.append(seconds)
                roles.add(role)
            elif seconds == 0:
                ties += 1
        previous = (timestamp, role, position, source)
    if not gaps:
        return {"sample_count": 0, "timestamp_roles": sorted(roles), "median_seconds": None,
                "p10_seconds": None, "p90_seconds": None, "p95_seconds": None,
                "mad_seconds": None, "timestamp_ties": ties,
                "method": "positive same-role gaps between adjacent observed rounds only"}
    median = statistics.median(gaps)
    mad = statistics.median(abs(value - median) for value in gaps)
    return {"sample_count": len(gaps), "timestamp_roles": sorted(roles),
            "median_seconds": median, "p10_seconds": _quantile(gaps, .10),
            "p90_seconds": _quantile(gaps, .90), "p95_seconds": _quantile(gaps, .95),
            "mad_seconds": mad, "timestamp_ties": ties,
            "method": "median and empirical quantiles of recent positive same-role adjacent-round gaps"}


def time_coverage(rounds: Sequence[Mapping], *, now: datetime | None = None,
                  collector_state: str | None = None,
                  excluded_rounds: Sequence[Mapping] = ()) -> dict:
    now = as_utc(now or datetime.now(timezone.utc))
    recent = rounds[-500:]
    event_samples = []
    arrival_samples = []
    for index, row in enumerate(recent):
        event_timestamp = (row.get("event_timestamp")
                           if row.get("timestamp_quality") == "platform_event_time" else None)
        source_name = str(row.get("source_name") or "unknown")
        event_samples.append((event_timestamp, "platform_event_time", index, source_name))
        observed = row.get("source_observed_at")
        stored = row.get("ingestion_timestamp")
        arrival_samples.append((observed or stored,
                                "source_observed_at" if observed else "ingestion_timestamp", index,
                                source_name))
    event_cadence = _cadence(event_samples)
    arrival_cadence = _cadence(arrival_samples)
    selected = event_cadence if event_cadence["sample_count"] >= 20 else arrival_cadence
    role = "platform_event_time" if selected is event_cadence else "delivery_observation_time"
    latest = recent[-1] if recent else {}
    latest_arrival = latest.get("source_observed_at") or latest.get("ingestion_timestamp")
    event_latest = latest.get("event_timestamp") if latest.get("timestamp_quality") == "platform_event_time" else None
    freshness_cadence = arrival_cadence if arrival_cadence["sample_count"] else event_cadence
    freshness_timestamp = latest_arrival or event_latest
    age = max(0.0, (now - as_utc(freshness_timestamp)).total_seconds()) if freshness_timestamp else None
    median = freshness_cadence.get("median_seconds")
    p10 = freshness_cadence.get("p10_seconds")
    p90 = freshness_cadence.get("p90_seconds")
    p95 = freshness_cadence.get("p95_seconds")
    mad = freshness_cadence.get("mad_seconds")
    threshold = max(PHASE1_P99_GAP_SECONDS,
                    (p95 or 0) * 3,
                    (median or 0) + 6 * 1.4826 * (mad or 0)) if median is not None else None
    source_state = str(collector_state or "UNKNOWN").upper()
    source_outage = source_state in {"ERROR", "OFFLINE", "DISCONNECTED", "OUTAGE", "STOPPED"}
    source_degraded = source_state not in {"HEALTHY", "CONNECTED", "COLLECTING", ""} and not source_outage
    possible_missing = bool(age is not None and threshold is not None and age > threshold and not source_outage)
    confirmed_gaps = []
    for left, right in zip(recent, recent[1:]):
        if (left.get("source_name") == right.get("source_name")
                and left.get("order_quality") in AUTHORITATIVE_ORDER_QUALITIES
                and right.get("order_quality") in AUTHORITATIVE_ORDER_QUALITIES
                and left.get("source_sequence") is not None
                and right.get("source_sequence") is not None):
            gap = int(right["source_sequence"]) - int(left["source_sequence"]) - 1
            if gap > 0:
                excluded_between = [row for row in excluded_rounds
                    if row.get("source_name") == right.get("source_name")
                    and row.get("source_sequence") is not None
                    and int(left["source_sequence"]) < int(row["source_sequence"])
                    < int(right["source_sequence"])]
                confirmed_gaps.append({"after_identity_key": left.get("identity_key"),
                    "before_identity_key": right.get("identity_key"),
                    "after_source_sequence": int(left["source_sequence"]),
                    "before_source_sequence": int(right["source_sequence"]),
                    "confirmed_missing_source_sequence_count": gap,
                    "observed_but_excluded_rounds": [{
                        "identity_key": row.get("identity_key"),
                        "round_id": row.get("round_id"),
                        "source_sequence": int(row["source_sequence"]),
                        "multiplier": row.get("multiplier_raw"),
                        "order_quality": row.get("order_quality"),
                    } for row in excluded_between],
                    "unrepresented_analyzed_sequence_count": max(0, gap - len(excluded_between)),
                    "source_name": right.get("source_name")})
    missing_range = None
    if possible_missing and p10 and p90:
        lower = max(0, math.floor(age / max(p90, 1e-9)) - 1)
        upper = max(lower, math.ceil(age / max(p10, 1e-9)) - 1)
        missing_range = {"lower": lower, "upper": upper, "confidence": "LOW",
                         "basis": "cadence quantiles during a stale interval; not a round identity count"}
    return {
        "status": ("CONFIRMED_SOURCE_GAP" if confirmed_gaps else
                   "KNOWN_COLLECTOR_OUTAGE" if source_outage else
                   "POSSIBLE_MISSING_ROUNDS" if possible_missing else
                   "DATA_FEED_UNHEALTHY" if source_degraded else "COVERAGE_OBSERVED"),
        "collector_state": collector_state or "UNKNOWN",
        "data_fresh": bool(age is not None and threshold is not None and age <= threshold),
        "last_observed_at": as_utc(freshness_timestamp).isoformat() if freshness_timestamp else None,
        "last_observation_age_seconds": age,
        "possible_missing_round_range": missing_range,
        "confirmed_source_gaps": confirmed_gaps,
        "event_round_cadence": event_cadence,
        "delivery_cadence": arrival_cadence,
        "freshness_cadence": freshness_cadence,
        "selected_cadence": {**selected, "basis": role,
                              "estimated_round_durations_seconds": {
                                  str(width): {"median": selected["median_seconds"] * max(0, width - 1),
                                               "low": (selected["p10_seconds"] or 0) * max(0, width - 1),
                                               "high": (selected["p90_seconds"] or 0) * max(0, width - 1)}
                                  if selected["median_seconds"] is not None else None
                                  for width in ROLLING_HORIZONS}},
        "stale_threshold_seconds": threshold,
        "timestamp_ties_in_sample": {"platform_event_time": event_cadence["timestamp_ties"],
                                     "delivery_time": arrival_cadence["timestamp_ties"]},
        "identity_invented": False,
        "method": "Positive, same-timestamp-role gaps only; time can flag possible absence but cannot complete windows or confirm identities.",
    }


def estimated_window_progress(rounds: Sequence[Mapping], coverage: Mapping, *,
                               now: datetime | None = None) -> dict:
    now = as_utc(now or datetime.now(timezone.utc))
    count = len(rounds)
    cadence = (coverage.get("selected_cadence") or {}).get("median_seconds")
    first = rounds[0] if rounds else {}
    basis = (coverage.get("selected_cadence") or {}).get("basis")
    if basis == "platform_event_time":
        start_time = first.get("event_timestamp")
    else:
        start_time = first.get("source_observed_at") or first.get("ingestion_timestamp")
    elapsed = max(0.0, (now - as_utc(start_time)).total_seconds()) if start_time else None
    estimated = math.floor(elapsed / cadence) + 1 if elapsed is not None and cadence and cadence > 0 else None
    estimated_progress = min(1.0, estimated / 100) if estimated is not None else None
    expected_status = ("TIME_ESTIMATED_100_ELAPSED" if estimated is not None and estimated >= 100 and count < 100
                       else "OBSERVED_100_COMPLETED" if count >= 100 else "TIME_ESTIMATE_ONLY")
    q10 = (coverage.get("selected_cadence") or {}).get("p10_seconds")
    q90 = (coverage.get("selected_cadence") or {}).get("p90_seconds")
    missing_count = max(0, 100 - count)
    remaining = {"low_seconds": missing_count * q10,
                 "median_seconds": missing_count * cadence if cadence else None,
                 "high_seconds": missing_count * q90} if cadence is not None else None
    return {"verified_round_progress": {"completed": count, "required": 100,
                                         "fraction": min(1.0, count / 100)},
            "time_estimate_progress": {"estimated_rounds": estimated,
                                       "fraction": estimated_progress,
                                       "elapsed_seconds": elapsed,
                                       "remaining_seconds": remaining,
                                       "status": expected_status,
                                       "never_completes_observed_window": True}}


def update_hysteresis(confirmed_state: str, previous_candidate: str,
                      candidate_state: str, previous_count: int
                      ) -> tuple[str, str, int, bool]:
    if candidate_state in {"INSUFFICIENT_DATA", "DEGRADED_COVERAGE"}:
        return confirmed_state, candidate_state, 0, False
    if candidate_state == confirmed_state:
        return confirmed_state, candidate_state, 0, False
    count = previous_count + 1 if candidate_state == previous_candidate else 1
    if count >= HYSTERESIS_ROUNDS:
        return candidate_state, candidate_state, 0, True
    return confirmed_state, candidate_state, count, False

