from __future__ import annotations

import math
import statistics
import threading
from collections import Counter
from decimal import Decimal, InvalidOperation
from itertools import product
from typing import Iterable, Mapping, Sequence

WINDOWS = (10, 20, 50, 100, 200, 500)
# Phase 1 tested the complete binary H/L family at lengths 2 through 6.
# This builds all 124 combinations, instead of limiting monitoring to examples.
MOTIFS = tuple("".join(symbols) for size in range(2, 7) for symbols in product("HL", repeat=size))


def classify_multiplier(raw: str | Decimal) -> str:
    try:
        value = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid multiplier") from None
    if not value.is_finite() or value < Decimal("1"):
        raise ValueError("invalid multiplier")
    if value > Decimal("2.00"):
        return "H"
    if value < Decimal("2.00"):
        return "L"
    return "E"


def _runs(symbols: Sequence[str]) -> dict:
    result = {"H": [], "L": [], "E": []}
    if not symbols:
        return {"run_count": 0, "max_run": {"H": 0, "L": 0, "E": 0},
                "run_lengths": result, "current_symbol": None, "current_length": 0}
    start = 0
    for end in range(1, len(symbols) + 1):
        if end == len(symbols) or symbols[end] != symbols[start]:
            result[symbols[start]].append(end - start)
            start = end
    return {
        "run_count": sum(len(v) for v in result.values()),
        "max_run": {k: max(v, default=0) for k, v in result.items()},
        "run_lengths": result,
        "current_symbol": symbols[-1],
        "current_length": result[symbols[-1]][-1],
    }


def _entropy(counts: Iterable[int]) -> float | None:
    counts = [c for c in counts if c]
    total = sum(counts)
    if not total:
        return None
    return -sum((c / total) * math.log2(c / total) for c in counts)


def _quantile(values: Sequence[float], q: float) -> float | None:
    if not values:
        return None
    xs = sorted(float(v) for v in values)
    pos = (len(xs) - 1) * q
    low = math.floor(pos)
    high = math.ceil(pos)
    return xs[low] + (xs[high] - xs[low]) * (pos - low)


_RECOVERY_REFERENCE_CACHE: dict[str, dict] = {}
_RECOVERY_REFERENCE_LOCK = threading.Lock()


def cached_recovery_reference(cache_key: str, symbols_loader) -> dict:
    """Build the frozen Phase 1 analogue index once per research snapshot/process."""
    with _RECOVERY_REFERENCE_LOCK:
        cached = _RECOVERY_REFERENCE_CACHE.get(cache_key)
        if cached is not None:
            return cached
        reference = build_recovery_reference(symbols_loader())
        _RECOVERY_REFERENCE_CACHE.clear()
        _RECOVERY_REFERENCE_CACHE[cache_key] = reference
        return reference


def build_recovery_reference(symbols: Sequence[str], *, context_width: int = 50) -> dict:
    """Index Phase 1 contexts with their following 50/100-round outcomes.

    Windows overlap deliberately to show all historical analogues. Consumers
    must label their summaries descriptive: these are dependent observations,
    not independent samples or validated recovery probabilities.
    """
    symbols = tuple(symbols)
    if any(symbol not in {"H", "L", "E"} for symbol in symbols):
        raise ValueError("Phase 1 recovery reference contains an invalid sequence symbol")
    prefix = {symbol: [0] for symbol in ("H", "L", "E")}
    for symbol in symbols:
        for key in prefix:
            prefix[key].append(prefix[key][-1] + int(symbol == key))

    def counts(start: int, end: int) -> tuple[int, int, int]:
        return tuple(prefix[symbol][end] - prefix[symbol][start] for symbol in ("H", "L", "E"))

    by_rate_bin: dict[int, list[tuple]] = {}
    last_start = len(symbols) - context_width - 100
    for start in range(max(0, last_start + 1)):
        high, low, _exact = counts(start, start + context_width)
        nonexact = high + low
        if not nonexact:
            continue
        context_rate = high / nonexact
        future50 = counts(start + context_width, start + context_width + 50)
        future100 = counts(start + context_width, start + context_width + 100)
        row = (context_rate, *future50, *future100)
        bin_id = round(context_rate * 100)
        by_rate_bin.setdefault(bin_id, []).append(row)
    return {"context_width": context_width, "future_horizons": (50, 100),
            "window_count": sum(map(len, by_rate_bin.values())),
            "by_rate_bin": by_rate_bin}


def _future_distribution(rows: Sequence[tuple], offset: int, horizon: int) -> dict:
    high_counts = [int(row[offset]) for row in rows]
    low_counts = [int(row[offset + 1]) for row in rows]
    exact_counts = [int(row[offset + 2]) for row in rows]
    rates = [high / (high + low) for high, low in zip(high_counts, low_counts) if high + low]
    return {"matched_windows": len(rows),
            "high_count": {"mean": statistics.fmean(high_counts) if rows else None,
                           "p05": _quantile(high_counts, .05), "p50": _quantile(high_counts, .50),
                           "p95": _quantile(high_counts, .95)},
            "low_count": {"mean": statistics.fmean(low_counts) if rows else None,
                          "p05": _quantile(low_counts, .05), "p50": _quantile(low_counts, .50),
                          "p95": _quantile(low_counts, .95)},
            "exact_count": {"mean": statistics.fmean(exact_counts) if rows else None,
                            "p05": _quantile(exact_counts, .05), "p50": _quantile(exact_counts, .50),
                            "p95": _quantile(exact_counts, .95)},
            "high_rate_nonexact_mean": statistics.fmean(rates) if rates else None,
            "horizon_rounds": horizon}


def compare_historical_recovery(current_symbols: Sequence[str], reference: Mapping | None,
                                phase1_high_rate: float | None, *, tolerance: float = .05) -> dict:
    """Compare a live 50-round context with Phase 1 without asserting recovery."""
    current = tuple(current_symbols[-50:])
    counts = Counter(current)
    high, low, exact = counts["H"], counts["L"], counts["E"]
    nonexact = high + low
    current_rate = high / nonexact if nonexact else None
    result = {"context_width": len(current), "required_context_width": 50,
              "counts": {"H": high, "L": low, "E": exact},
              "high_rate_nonexact": current_rate,
              "phase1_high_rate_nonexact": phase1_high_rate,
              "reference_deviation_percentage_points": (
                  (current_rate - phase1_high_rate) * 100
                  if current_rate is not None and phase1_high_rate is not None else None),
              "high_deficit_rounds_vs_phase1_reference": (
                  high - phase1_high_rate * nonexact
                  if phase1_high_rate is not None else None),
              "match_tolerance_percentage_points": tolerance * 100,
              "matched_historical_windows": 0,
              "future_50": _future_distribution([], 1, 50),
              "future_100": _future_distribution([], 4, 100),
              "evidence_status": "INSUFFICIENT_CONTEXT",
              "recovery_likelihood": None,
              "recovery_likelihood_status": "NOT_ESTIMATED_NO_VALIDATED_RECOVERY_SIGNAL",
              "predictive_adjustment_applied": False}
    if len(current) < 50 or current_rate is None or reference is None:
        return result

    by_rate_bin = reference.get("by_rate_bin", {})
    lower_bin = max(0, math.floor((current_rate - tolerance) * 100) - 1)
    upper_bin = min(100, math.ceil((current_rate + tolerance) * 100) + 1)
    matches = []
    for bin_id in range(lower_bin, upper_bin + 1):
        for row in by_rate_bin.get(bin_id, []):
            if abs(float(row[0]) - current_rate) <= tolerance + 1e-12:
                matches.append(row)
    result.update({"matched_historical_windows": len(matches),
                   "future_50": _future_distribution(matches, 1, 50),
                   "future_100": _future_distribution(matches, 4, 100),
                   "historical_windows_overlap": True,
                   "historical_window_source": "frozen_Phase1_valid_canonical_rounds",
                   "evidence_status": "DESCRIPTIVE_ANALOGUES_ONLY" if matches else "NO_MATCHING_ANALOGUES",
                   "limitations": [
                       "Matched contexts overlap and are statistically dependent.",
                       "Phase 1 precursor tests found no features surviving multiple-test correction.",
                       "Historical analogue outcomes are descriptive and do not estimate validated recovery likelihood.",
                   ]})
    return result


def sequence_features(symbols: Sequence[str]) -> dict:
    n = len(symbols)
    counts = Counter(symbols)
    non_exact = counts["H"] + counts["L"]
    adjacent = [(a, b) for a, b in zip(symbols, symbols[1:]) if a != "E" and b != "E"]
    transitions = Counter(zip(symbols, symbols[1:]))
    previous = Counter(a for a, _ in zip(symbols, symbols[1:]))
    conditional_entropy = 0.0
    transition_total = max(1, n - 1)
    for a, total in previous.items():
        row = [transitions[a, b] for b in ("H", "L", "E")]
        h = _entropy(row)
        conditional_entropy += (total / transition_total) * (h or 0.0)
    runs = _runs(symbols)
    return {
        "rounds": n,
        "counts": {s: counts[s] for s in ("H", "L", "E")},
        "high_rate_all": counts["H"] / n if n else None,
        "low_rate_all": counts["L"] / n if n else None,
        "exact_rate": counts["E"] / n if n else None,
        "high_rate_nonexact": counts["H"] / non_exact if non_exact else None,
        "low_rate_nonexact": counts["L"] / non_exact if non_exact else None,
        "alternation_count": sum(a != b for a, b in adjacent),
        "alternation_rate": sum(a != b for a, b in adjacent) / len(adjacent) if adjacent else None,
        "alternation_denominator": len(adjacent),
        "transition_counts": {f"{a}{b}": transitions[a, b] for a in ("H", "L", "E") for b in ("H", "L", "E")},
        "transition_entropy_bits": conditional_entropy if n > 1 else None,
        "outcome_entropy_bits": _entropy(counts[s] for s in ("H", "L", "E")),
        "run_count": runs["run_count"],
        "run_max": runs["max_run"],
        "run_lengths": runs["run_lengths"],
        "current_streak": {"symbol": runs["current_symbol"], "length": runs["current_length"]},
        "motifs": {
            motif: sum("".join(symbols[i:i + len(motif)]) == motif
                       for i in range(max(0, n - len(motif) + 1)))
            for motif in MOTIFS
        },
    }


def distribution_features(multiplier_raw: Sequence[str]) -> dict:
    values = [float(Decimal(x)) for x in multiplier_raw]
    if not values:
        return {"n": 0}
    logs = [math.log(v) for v in values]
    return {
        "n": len(values), "minimum": min(values), "maximum": max(values),
        "mean": statistics.fmean(values), "median": statistics.median(values),
        "quantiles": {str(q): _quantile(values, q) for q in (0.01, 0.05, 0.10, 0.25, 0.5, 0.75, 0.9, 0.95, 0.99)},
        "ge_5": sum(x >= 5 for x in values), "ge_10": sum(x >= 10 for x in values),
        "ge_100": sum(x >= 100 for x in values), "ge_1000": sum(x >= 1000 for x in values),
        "log_mean": statistics.fmean(logs),
        "log_volatility": statistics.stdev(logs) if len(logs) > 1 else 0.0,
    }


def rolling_features(rounds: Sequence[Mapping]) -> dict:
    symbols = [r["outcome"] for r in rounds]
    raw = [r["multiplier_raw"] for r in rounds]
    out = {}
    for width in WINDOWS:
        window = rounds[-width:]
        if len(window) < width:
            out[str(width)] = {"support": len(window), "required": width, "complete": False}
            continue
        seq = sequence_features([r["outcome"] for r in window])
        dist = distribution_features([r["multiplier_raw"] for r in window])
        times = [r.get("event_timestamp") for r in window]
        duration = (times[-1] - times[0]).total_seconds() if times[0] is not None and times[-1] is not None else None
        out[str(width)] = {"support": width, "required": width, "complete": True,
                           **seq, "multiplier": dist, "duration_seconds": duration,
                           "event_time_available": all(t is not None for t in times)}
    return out


def classify_state(features: Mapping, *, min_rounds: int = 20,
                   high_dominance: float = 0.60, low_dominance: float = 0.40,
                   balanced_band: float = 0.08, alternation_threshold: float = 0.65,
                   high_cluster_run_p90: int = 7, low_cluster_run_p90: int = 8) -> tuple[str, dict]:
    counts = features.get("counts", {})
    h, l = int(counts.get("H", 0)), int(counts.get("L", 0))
    den = h + l
    high_share = h / den if den else None
    alternate = features.get("alternation_rate")
    max_run = features.get("run_max", {})
    if den < min_rounds:
        state = "UNCERTAIN"
        why = f"needs at least {min_rounds} non-EXACT rounds; currently {den}"
    elif high_share is not None and high_share >= high_dominance:
        state, why = "HIGH_DOMINANT", f"HIGH share {high_share:.3f} >= {high_dominance:.2f}"
    elif high_share is not None and high_share <= low_dominance:
        state, why = "LOW_DOMINANT", f"HIGH share {high_share:.3f} <= {low_dominance:.2f}"
    elif alternate is not None and alternate >= alternation_threshold:
        state, why = "ALTERNATING", f"H/L alternation {alternate:.3f} >= {alternation_threshold:.2f}"
    elif int(max_run.get("H", 0)) >= high_cluster_run_p90:
        state, why = "HIGH_CLUSTERING", f"maximum HIGH run {max_run.get('H')} >= Phase 1 rolling-50 p90 {high_cluster_run_p90}"
    elif int(max_run.get("L", 0)) >= low_cluster_run_p90:
        state, why = "LOW_CLUSTERING", f"maximum LOW run {max_run.get('L')} >= Phase 1 rolling-50 p90 {low_cluster_run_p90}"
    elif high_share is not None and abs(high_share - 0.5) <= balanced_band:
        state, why = "BALANCED", f"HIGH share {high_share:.3f} is within ±{balanced_band:.2f} of 0.50"
    else:
        state, why = "TRANSITION", "supported window meets no dominant, alternating, clustering or balanced condition"
    return state, {"explanation": why, "high_share_nonexact": high_share,
                   "alternation_rate": alternate, "max_run": max_run,
                   "support_nonexact": den, "minimum_support": min_rounds,
                   "thresholds": {"high_dominance": high_dominance, "low_dominance": low_dominance,
                                  "balanced_band": balanced_band, "alternation": alternation_threshold,
                                  "high_cluster_run_p90": high_cluster_run_p90,
                                  "low_cluster_run_p90": low_cluster_run_p90},
                   "evidence_class": "descriptive_heuristic"}


def confirm_state(candidate: str, prior: Mapping | None, confirm_rounds: int = 3) -> dict:
    prior = prior or {}
    active = prior.get("confirmed_state", "UNCERTAIN")
    pending = prior.get("pending_state")
    count = int(prior.get("pending_count", 0))
    if candidate == active:
        return {"confirmed_state": active, "pending_state": None, "pending_count": 0}
    count = count + 1 if pending == candidate else 1
    if count >= confirm_rounds:
        return {"confirmed_state": candidate, "pending_state": None, "pending_count": 0}
    return {"confirmed_state": active, "pending_state": candidate, "pending_count": count}


def time_groups(rounds: Sequence[Mapping], gap_seconds: float = 180.0452) -> list[dict]:
    """Phase 1 p99-gap grouping; missing times and uncertain order stay explicit."""
    groups: list[list[Mapping]] = []
    current: list[Mapping] = []
    for r in rounds:
        ts = r.get("event_timestamp")
        if ts is None:
            if current:
                groups.append(current)
                current = []
            groups.append([r])
            continue
        previous_ts = current[-1].get("event_timestamp") if current else None
        if current and previous_ts is not None and (ts - previous_ts).total_seconds() > gap_seconds:
            groups.append(current)
            current = []
        current.append(r)
    if current:
        groups.append(current)
    result = []
    for idx, group in enumerate(groups, 1):
        seq = sequence_features([r["outcome"] for r in group])
        ts = [r.get("event_timestamp") for r in group]
        times_known = all(t is not None for t in ts)
        durations = (ts[-1] - ts[0]).total_seconds() if times_known else None
        quality = "event_time_unknown" if not times_known else (
            "shared_timestamp" if len(set(ts)) < len(ts) else "event_time_recorded")
        h_rate = seq.get("high_rate_nonexact")
        alternation = seq.get("alternation_rate") or 0.0
        if len(group) < 20 or h_rate is None:
            state, reason = "UNCERTAIN", "group has fewer than 20 rounds"
        elif h_rate >= 0.60:
            state, reason = "HIGH_DOMINANT", "Phase 1 group rule: HIGH share >= 0.60"
        elif h_rate <= 0.40:
            state, reason = "LOW_DOMINANT", "Phase 1 group rule: HIGH share <= 0.40"
        elif alternation >= 0.65:
            state, reason = "ALTERNATING", "Phase 1 group rule: alternation >= 0.65"
        elif abs(h_rate - 0.5) <= 0.08:
            state, reason = "BALANCED", "Phase 1 group rule: HIGH share within 0.50 +/- 0.08"
        else:
            state, reason = "TRANSITION", "supported group meets no Phase 1 group label boundary"
        state_evidence = {"explanation": reason, "evidence_class": "descriptive_phase1_group_label",
                          "high_rate_nonexact": h_rate, "alternation_rate": alternation,
                          "minimum_group_rounds": 20}
        result.append({"group_number": idx, "first_position": group[0]["analysis_position"],
                       "last_position": group[-1]["analysis_position"], "rounds": len(group),
                       "start": ts[0].isoformat() if times_known else None,
                       "end": ts[-1].isoformat() if times_known else None,
                       "duration_seconds": durations, "gap_threshold_seconds": gap_seconds,
                       "timestamp_quality": quality, "sequence": seq,
                       "multiplier": distribution_features([r["multiplier_raw"] for r in group]),
                       "state": state, "state_evidence": state_evidence})
    return result


def disjoint_reference_blocks(symbols: Sequence[str], start_origin: int = 199, horizon: int = 100) -> list[dict]:
    """Exact Phase 1 target construction: position 199, non-overlapping future-100 blocks."""
    result = []
    for origin in range(start_origin, len(symbols) - horizon, horizon):
        future = symbols[origin + 1:origin + horizon + 1]
        high, low, exact = future.count("H"), future.count("L"), future.count("E")
        denom = high + low
        # positions exposed in the database are 1-based, while origin and
        # Python slices above are 0-based. The first target round is origin+2.
        result.append({"block_number": len(result) + 1, "first_position": origin + 2,
                       "last_position": origin + horizon + 1, "high_count": high,
                       "low_count": low, "exact_count": exact,
                       "high_rate_nonexact": high / denom if denom else 0.5,
                       "high_rate_all": high / horizon, "low_rate_all": low / horizon,
                       "exact_rate": exact / horizon,
                       "sequence": sequence_features(future)})
    return result


def baseline_assessment(blocks: Sequence[Mapping], *, latest_position: int,
                        latest_identity_key: str | None, state: str = "UNCERTAIN") -> dict:
    if not blocks:
        return {"horizon": 100, "data_sufficiency": "INSUFFICIENT_HISTORY",
                "expected_high_rate_nonexact": None, "expected_high_count": None,
                "expected_low_count": None, "expected_exact_count": None,
                "historical_block_count": 0, "latest_position": latest_position,
                "latest_identity_key": latest_identity_key, "state": state,
                "forecast_method": "phase1_expanding_mean_of_disjoint_future100_blocks"}
    p_h = statistics.fmean(float(b["high_rate_nonexact"]) for b in blocks)
    p_e = statistics.fmean(float(b["exact_count"]) / 100.0 for b in blocks)
    expected_h = 100.0 * p_h * (1.0 - p_e)
    expected_e = 100.0 * p_e
    expected_l = 100.0 - expected_h - expected_e
    high_counts = [int(b["high_count"]) for b in blocks]
    low_counts = [int(b["low_count"]) for b in blocks]
    exact_counts = [int(b["exact_count"]) for b in blocks]
    h_all_probability = min(1 - 1e-12, max(1e-12, expected_h / 100.0))
    threshold_probabilities = {}
    pmf = binomial_pmf(100, h_all_probability)
    for threshold in (55, 60, 65):
        threshold_probabilities[f"high_count_ge_{threshold}"] = sum(pmf[threshold:])
    for threshold in (45, 40, 35):
        threshold_probabilities[f"high_count_le_{threshold}"] = sum(pmf[:threshold + 1])
    for lo, hi in ((45, 55), (40, 60)):
        threshold_probabilities[f"balanced_{lo}_{hi}"] = sum(pmf[lo:hi + 1])
    count_dist = {
        "historical_windows": len(blocks),
        "high_count": {"mean": statistics.fmean(high_counts), "p05": _quantile(high_counts, .05),
                       "p50": _quantile(high_counts, .50), "p95": _quantile(high_counts, .95)},
        "low_count": {"mean": statistics.fmean(low_counts), "p05": _quantile(low_counts, .05),
                      "p50": _quantile(low_counts, .50), "p95": _quantile(low_counts, .95)},
        "exact_count": {"mean": statistics.fmean(exact_counts), "p05": _quantile(exact_counts, .05),
                        "p50": _quantile(exact_counts, .50), "p95": _quantile(exact_counts, .95)},
        "high_alternations": _empirical(blocks, lambda b: b["sequence"]["alternation_count"]),
        "high_max_run": _empirical(blocks, lambda b: b["sequence"]["run_max"]["H"]),
        "low_max_run": _empirical(blocks, lambda b: b["sequence"]["run_max"]["L"]),
        "source": "empirical disjoint future-100 blocks; descriptive interval, not guaranteed calibration",
    }
    sequence_outlook = {
        "evidence_status": "empirical_next100_distribution_descriptive_not_predictive",
        "historical_blocks": len(blocks),
        "alternation_changes": _empirical(blocks, lambda b: b["sequence"]["alternation_count"]),
        "maximum_high_run": _empirical(blocks, lambda b: b["sequence"]["run_max"]["H"]),
        "maximum_low_run": _empirical(blocks, lambda b: b["sequence"]["run_max"]["L"]),
        "probability_max_high_run_at_least": {
            str(k): sum(b["sequence"]["run_max"]["H"] >= k for b in blocks) / len(blocks)
            for k in range(2, 13)},
        "probability_max_low_run_at_least": {
            str(k): sum(b["sequence"]["run_max"]["L"] >= k for b in blocks) / len(blocks)
            for k in range(2, 13)},
        "motif_occurrences": {
            motif: {**_empirical(blocks, lambda b, m=motif: b["sequence"]["motifs"].get(m, 0)),
                    "probability_nonzero": sum(b["sequence"]["motifs"].get(motif, 0) > 0
                                                for b in blocks) / len(blocks)}
            for motif in MOTIFS},
    }
    count_dist["sequence_outlook"] = sequence_outlook
    return {
        "horizon": 100, "data_sufficiency": "SUPPORTED_HISTORICAL_BASELINE" if len(blocks) >= 20 else "LOW_SUPPORT",
        "expected_high_rate_nonexact": p_h, "expected_exact_rate": p_e,
        "expected_high_count": expected_h, "expected_low_count": expected_l,
        "expected_exact_count": expected_e, "threshold_probabilities": threshold_probabilities,
        "historical_count_distribution": count_dist, "historical_block_count": len(blocks),
        "latest_position": latest_position, "latest_identity_key": latest_identity_key,
        "state": state, "forecast_method": "phase1_expanding_mean_of_disjoint_future100_blocks",
        "prediction_status": "historical_statistical_assessment_not_exact_outcome_prediction",
        "explanation": {
            "high_rate": "Arithmetic mean of each fully observed, non-overlapping 100-round block's H/(H+L), starting after the initial 200-round warm-up as in Phase 1.",
            "exact_rate": "Arithmetic mean of E counts divided by 100 across those same completed blocks.",
            "expected_counts": "Expected H = 100 * H-rate * (1 - E-rate); E = 100 * E-rate; L = remainder.",
            "threshold_probabilities": "Binomial approximation using expected all-round H probability; serial dependence is not represented.",
            "uncertainty": "Historical empirical p05/p50/p95 across disjoint future-100 windows, shown separately from binomial threshold calculations.",
            "baseline_evidence": "Phase 1 expanding baseline MSE 0.002923; contextual model MSE 0.003144. No contextual adjustment is active.",
        },
    }


def binomial_pmf(n: int, p: float) -> list[float]:
    p = min(1.0 - 1e-15, max(1e-15, float(p)))
    q = 1.0 - p
    values = [q ** n]
    for k in range(1, n + 1):
        values.append(values[-1] * (n - k + 1) / k * p / q)
    total = sum(values)
    return [x / total for x in values]


def _empirical(blocks: Sequence[Mapping], fn) -> dict:
    values = [float(fn(b)) for b in blocks]
    return {"mean": statistics.fmean(values), "p05": _quantile(values, .05),
            "p50": _quantile(values, .5), "p95": _quantile(values, .95)}


def validate_expanding_baseline(symbols: Sequence[str]) -> dict:
    blocks = disjoint_reference_blocks(symbols)
    if len(blocks) < 20:
        return {"blocks": len(blocks), "test_origins": 0, "status": "INSUFFICIENT_HISTORY"}
    first_test = len(blocks) // 2
    errors = []
    abs_errors = []
    for index in range(first_test, len(blocks)):
        forecast = statistics.fmean(float(b["high_rate_nonexact"]) for b in blocks[:index])
        actual = float(blocks[index]["high_rate_nonexact"])
        errors.append((forecast - actual) ** 2)
        abs_errors.append(abs(forecast - actual))
    return {"blocks": len(blocks), "warmup_blocks": first_test,
            "test_origins": len(errors), "brier_mse_rate": statistics.fmean(errors),
            "mae_rate": statistics.fmean(abs_errors),
            "research_reference_mse_rate": 0.002922870360206869,
            "matches_phase1_tolerance": abs(statistics.fmean(errors) - 0.002922870360206869) < 1e-9,
            "method": "first 50 percent of disjoint target blocks warm-up; each test forecast averages all prior target H/(H+L) rates"}


def motif_evidence(symbols: Sequence[str], motifs: Sequence[str]) -> dict:
    max_horizon = 500
    sample = symbols[-max_horizon:]
    result = {}
    for motif in motifs:
        n = len(motif)
        result[motif] = {"count_last_500": sum("".join(sample[i:i + n]) == motif
                                               for i in range(max(0, len(sample) - n + 1))),
                         "possible_windows_last_500": max(0, len(sample) - n + 1)}
    return result
