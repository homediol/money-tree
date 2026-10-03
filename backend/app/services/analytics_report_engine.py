"""Immutable, source-traceable statistical snapshots of collected Aviator rounds.

This module is descriptive research. It never creates a betting decision. Every
input row must be a validated persisted round supplied by DatasetService.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import hashlib
import math
import time
from typing import Any, Iterable

import numpy as np
from app.services.pattern_engine import wilson_interval


LOW = "L"  # multiplier < 2.00x
HIGH = "H"  # multiplier >= 2.00x
BIN_LABELS = ("<1.20x", "1.20-1.49x", "1.50-1.99x", "2.00-2.99x",
              "3.00-4.99x", "5.00-9.99x", ">=10.00x")
WINDOWS = (50, 100, 250, 500, 1000)
MIN_PATTERN_SAMPLE = 30


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    try:
        stamp = pd_timestamp(value)
        return stamp.isoformat() if stamp else None
    except (TypeError, ValueError, OverflowError):
        return None


def pd_timestamp(value: Any) -> datetime | None:
    # Keep parsing dependency-light and reject invalid timestamps rather than
    # deriving a time from the round index.
    if isinstance(value, datetime):
        parsed = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _round_values(rows: Iterable[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[float], list[str]]:
    clean: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        try:
            rid = str(row.get("round_id") or row.get("round_index"))
            index = int(row.get("round_index"))
            value = float(row.get("multiplier"))
        except (TypeError, ValueError):
            continue
        if not rid or rid == "None" or rid in seen or not math.isfinite(value) or value <= 0:
            continue
        seen.add(rid)
        clean.append({"round_id": rid, "round_index": index,
                      "multiplier": value, "timestamp": _iso(row.get("timestamp"))})
    clean.sort(key=lambda item: (item["round_index"], item["round_id"]))
    vals = [item["multiplier"] for item in clean]
    states = [HIGH if value >= 2.0 else LOW for value in vals]
    return clean, vals, states


def _rate(successes: int, total: int) -> dict[str, Any]:
    return {"count": total, "successes": successes,
            "rate": successes / total if total else None,
            "percentage": 100 * successes / total if total else None,
            "confidence_95": wilson_interval(successes, total),
            "sample_status": "INSUFFICIENT_SAMPLE" if total < MIN_PATTERN_SAMPLE else "MEASURABLE"}


def _runs(states: list[str]) -> dict[str, Any]:
    streaks: dict[str, Counter] = {LOW: Counter(), HIGH: Counter()}
    longest = {LOW: 0, HIGH: 0}
    completed: list[tuple[str, int]] = []
    if not states:
        return {"length_distribution": {side: {} for side in (LOW, HIGH)},
                "average_length": {side: None for side in (LOW, HIGH)},
                "median_length": {side: None for side in (LOW, HIGH)},
                "longest": {"<2x": 0, ">=2x": 0}, "sequences": []}
    side, length = states[0], 1
    for current in states[1:]:
        if current == side:
            length += 1
        else:
            streaks[side]["6+" if length >= 6 else str(length)] += 1
            completed.append((side, length))
            longest[side] = max(longest[side], length)
            side, length = current, 1
    streaks[side]["6+" if length >= 6 else str(length)] += 1
    completed.append((side, length))
    longest[side] = max(longest[side], length)
    result: dict[str, Any] = {"length_distribution": {}, "average_length": {}, "median_length": {}}
    for key, label in ((LOW, "<2x"), (HIGH, ">=2x")):
        result["length_distribution"][label] = {str(n): streaks[key].get(str(n), 0) for n in range(1, 6)} | {"6+": streaks[key].get("6+", 0)}
        lengths = [n for state, n in completed if state == key]
        result["average_length"][label] = float(np.mean(lengths)) if lengths else None
        result["median_length"][label] = float(np.median(lengths)) if lengths else None
    result["longest"] = {"<2x": longest[LOW], ">=2x": longest[HIGH]}
    result["sequences"] = [{"state": "<2x" if state == LOW else ">=2x", "length": n} for state, n in completed]
    return result


def _transition_analysis(states: list[str]) -> dict[str, Any]:
    baseline = _rate(states.count(HIGH), len(states))
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # samples, H next
    for i in range(len(states) - 1):
        for size in range(1, min(6, i + 1) + 1):
            sequence = "".join(states[i - size + 1:i + 1])
            counts[sequence][0] += 1
            counts[sequence][1] += int(states[i + 1] == HIGH)
    patterns = {}
    for sequence, (sample, successes) in sorted(counts.items()):
        empirical = _rate(successes, sample)
        patterns[sequence] = {**empirical, "sequence": sequence,
                              "baseline_rate": baseline["rate"],
                              "difference_from_baseline": (empirical["rate"] - baseline["rate"])
                                if empirical["rate"] is not None and baseline["rate"] is not None else None}
    return {"baseline_p_h": baseline, "matrix": {
        LOW: {"next_L": counts.get(LOW, [0, 0])[0] - counts.get(LOW, [0, 0])[1],
              "next_H": counts.get(LOW, [0, 0])[1]},
        HIGH: {"next_L": counts.get(HIGH, [0, 0])[0] - counts.get(HIGH, [0, 0])[1],
               "next_H": counts.get(HIGH, [0, 0])[1]},
    }, "conditional_sequences": patterns}


def _summary(rows: list[dict[str, Any]], vals: list[float], states: list[str]) -> dict[str, Any]:
    n = len(vals)
    high = sum(state == HIGH for state in states)
    cuts = (("<1.20x", lambda x: x < 1.20), ("1.20-1.49x", lambda x: 1.20 <= x < 1.50),
            ("1.50-1.99x", lambda x: 1.50 <= x < 2.00), ("2.00-2.99x", lambda x: 2.00 <= x < 3.00),
            ("3.00-4.99x", lambda x: 3.00 <= x < 5.00), ("5.00-9.99x", lambda x: 5.00 <= x < 10.00),
            (">=10.00x", lambda x: x >= 10.00))
    low = {**_rate(n - high, n), "count": n - high, "sample_size": n}
    high_rate = {**_rate(high, n), "count": high, "sample_size": n}
    histogram = {label: sum(bool(test(value)) for value in vals) for label, test in cuts}
    quantiles = {f"p{int(q * 100)}": float(np.quantile(vals, q, method="linear")) for q in (.01, .05, .10, .25, .50, .75, .90, .95, .99)} if vals else {}
    log_values = np.log(np.asarray(vals, dtype=float)) if vals else np.asarray([], dtype=float)
    return {"round_count": n, "under_2x": low, "at_least_2x": high_rate,
            "baseline_p_h": high_rate["rate"], "multiplier": {
                "mean": float(np.mean(vals)) if vals else None,
                "median": float(np.median(vals)) if vals else None,
                "minimum": min(vals) if vals else None, "maximum": max(vals) if vals else None,
                "quantiles": quantiles,
                "population_stddev": float(np.std(vals)) if vals else None,
                "log_population_stddev": float(np.std(log_values)) if vals else None,
                "coefficient_of_variation": float(np.std(vals) / np.mean(vals)) if vals and np.mean(vals) else None,
            }, "multiplier_histogram": {label: {"count": count,
                "percentage": (100 * count / n if n else None)} for label, count in histogram.items()},
            "streaks": _runs(states), "transitions": _transition_analysis(states)}


def analyze_rounds(rows: Iterable[dict[str, Any]], quality: dict[str, Any] | None = None,
                   *, include_details: bool = True) -> dict[str, Any]:
    """Build descriptive analytics from validated rows only; timestamps stay nullable."""
    clean, vals, states = _round_values(rows)
    summary = _summary(clean, vals, states)
    if not include_details:
        summary["data_quality"] = dict(quality or {})
        summary["time_of_day_status"] = ("RELIABLE_TIMESTAMPS" if clean and all(row["timestamp"] for row in clean)
                                          else "UNAVAILABLE_TIMESTAMP_COVERAGE")
        summary["warnings"] = ([] if len(vals) >= MIN_PATTERN_SAMPLE else
                               [f"Total sample {len(vals)} is below {MIN_PATTERN_SAMPLE}; conditional patterns are insufficient."])
        return summary
    all_rows = clean
    summary["windows"] = {}
    for window in WINDOWS:
        selected = clean[-window:]
        selected_vals = vals[-window:]
        selected_states = states[-window:]
        summary["windows"][str(window)] = _summary(selected, selected_vals, selected_states) if selected else _summary([], [], [])
        summary["windows"][str(window)]["available"] = len(selected) == window
    timestamps = [pd_timestamp(row.get("timestamp")) for row in all_rows]
    if all(timestamps) and timestamps:
        by_hour: dict[str, list[str]] = defaultdict(list)
        for stamp, state in zip(timestamps, states):
            by_hour[f"{stamp.hour:02d}:00 UTC"].append(state)
        summary["time_of_day_utc"] = {hour: _rate(values.count(HIGH), len(values)) for hour, values in sorted(by_hour.items())}
        summary["time_of_day_status"] = "RELIABLE_TIMESTAMPS"
    else:
        summary["time_of_day_utc"] = None
        summary["time_of_day_status"] = "UNAVAILABLE_TIMESTAMP_COVERAGE"
    blocks = []
    for offset in range(0, len(clean), 1000):
        block = clean[offset:offset + 1000]
        if len(block) == 1000:
            block_high = sum(item["multiplier"] >= 2 for item in block)
            blocks.append({"block": offset // 1000 + 1, "first_round": block[0]["round_id"],
                           "last_round": block[-1]["round_id"], "round_count": 1000,
                           "p_h": block_high / 1000, "log_stddev": float(np.std(np.log([item["multiplier"] for item in block])))})
    summary["block_persistence"] = {"complete_1000_round_blocks": blocks,
        "pattern_persistence": _block_persistence(blocks),
        "data_available_through_round": clean[-1]["round_id"] if clean else None}
    summary["sequence_pattern_persistence"] = _sequence_block_persistence(states)
    recent = states[-250:]
    prior = states[max(0, len(states) - 500):max(0, len(states) - 250)]
    recent_ci = wilson_interval(recent.count(HIGH), len(recent))
    prior_ci = wilson_interval(prior.count(HIGH), len(prior))
    enough_drift_sample = len(recent) >= MIN_PATTERN_SAMPLE and len(prior) >= MIN_PATTERN_SAMPLE
    overlap = (recent_ci["lower"] is not None and prior_ci["lower"] is not None
               and max(recent_ci["lower"], prior_ci["lower"]) <= min(recent_ci["upper"], prior_ci["upper"]))
    summary["drift"] = {"status": ("INSUFFICIENT_DATA" if not enough_drift_sample
                                     else "STABLE" if overlap else "CHANGING"),
        "method": "Recent 250 versus preceding 250 >=2x rate; Wilson 95% interval overlap heuristic.",
        "recent_250": {"sample_size": len(recent), "rate": recent.count(HIGH) / len(recent) if recent else None,
                        "confidence_95": recent_ci},
        "preceding_250": {"sample_size": len(prior), "rate": prior.count(HIGH) / len(prior) if prior else None,
                           "confidence_95": prior_ci},
        "difference": (recent.count(HIGH) / len(recent) - prior.count(HIGH) / len(prior)) if recent and prior else None,
        "note": "Descriptive change screen only; it does not establish a predictive regime."}
    summary["data_quality"] = dict(quality or {})
    summary["rolling_at_least_2x"] = [{"window": width, "round_id": clean[i]["round_id"],
        "rate": float(np.mean([value >= 2 for value in vals[i - width + 1:i + 1]]))}
        for width in WINDOWS if len(vals) >= width
        for i in range(width - 1, len(vals), max(1, width // 10))]
    summary["warnings"] = ([] if len(vals) >= MIN_PATTERN_SAMPLE else [f"Total sample {len(vals)} is below {MIN_PATTERN_SAMPLE}; conditional patterns are insufficient."])
    return summary


def _block_persistence(blocks: list[dict[str, Any]]) -> dict[str, Any]:
    if len(blocks) < 2:
        return {"status": "INSUFFICIENT_DATA", "blocks": len(blocks), "rates": [b["p_h"] for b in blocks]}
    rates = [block["p_h"] for block in blocks]
    intervals = [wilson_interval(round(block["p_h"] * block["round_count"]), block["round_count"]) for block in blocks]
    common_lower = max(interval["lower"] for interval in intervals)
    common_upper = min(interval["upper"] for interval in intervals)
    return {"status": "STABLE" if common_lower <= common_upper else "CHANGING",
            "blocks": len(blocks), "rates": rates,
            "confidence_intervals_95": intervals,
            "range": max(rates) - min(rates),
            "note": "STABLE means block Wilson intervals share a common range; this is descriptive and not evidence of a predictive regime."}


def _sequence_block_persistence(states: list[str], block_size: int = 1000, max_length: int = 4) -> dict[str, Any]:
    samples: dict[str, list[dict[str, Any]]] = defaultdict(list)
    complete = len(states) // block_size
    for block_no in range(complete):
        start, end = block_no * block_size, (block_no + 1) * block_size
        block_counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
        for index in range(start, end - 1):
            for size in range(1, min(max_length, index - start + 1) + 1):
                sequence = "".join(states[index - size + 1:index + 1])
                block_counts[sequence][0] += 1
                block_counts[sequence][1] += int(states[index + 1] == HIGH)
        for sequence, (count, successes) in block_counts.items():
            samples[sequence].append({"block": block_no + 1, "sample_size": count,
                                      "p_h_next": successes / count,
                                      "confidence_95": wilson_interval(successes, count)})
    result = {}
    for sequence, observed in sorted(samples.items()):
        qualifying = [item for item in observed if item["sample_size"] >= MIN_PATTERN_SAMPLE]
        if len(qualifying) < 2:
            status = "INSUFFICIENT_DATA"
        else:
            lower = max(item["confidence_95"]["lower"] for item in qualifying)
            upper = min(item["confidence_95"]["upper"] for item in qualifying)
            status = "STABLE" if lower <= upper else "CHANGING"
        result[sequence] = {"blocks_observed": len(observed), "block_evidence": observed,
                            "status": status,
                            "note": "Cross-block descriptive check; no predictive claim."}
    return {"complete_1000_round_blocks": complete, "minimum_samples_per_block": MIN_PATTERN_SAMPLE,
            "sequences": result}


def compare_summaries(current: dict[str, Any], previous: dict[str, Any] | None) -> dict[str, Any]:
    if not previous:
        return {"classification": "INSUFFICIENT_DATA", "previous_report_id": None,
                "changes": [], "reason": "No previous comparable report."}
    previous_report = previous
    previous = previous.get("analysis", previous) if isinstance(previous, dict) else previous
    changes = []
    current_rate = current.get("at_least_2x", {}).get("rate")
    previous_rate = previous.get("at_least_2x", {}).get("rate")
    current_n = current.get("round_count", 0)
    previous_n = previous.get("round_count", 0)
    rate_change = None if current_rate is None or previous_rate is None else current_rate - previous_rate
    changes.append({"metric": "at_least_2x_rate", "current": current_rate, "previous": previous_rate,
                    "difference": rate_change,
                    "current_confidence_95": current.get("at_least_2x", {}).get("confidence_95"),
                    "previous_confidence_95": previous.get("at_least_2x", {}).get("confidence_95")})
    curr_hist = current.get("multiplier_histogram", {})
    prev_hist = previous.get("multiplier_histogram", {})
    hist_diffs = {key: (curr_hist.get(key, {}).get("percentage") or 0) - (prev_hist.get(key, {}).get("percentage") or 0)
                  for key in set(curr_hist) | set(prev_hist)}
    changes.append({"metric": "multiplier_distribution_percentage_point_change", "differences": hist_diffs,
                    "changed": any(abs(value) >= 5 for value in hist_diffs.values())})
    for title, get_val in (
        ("streak_behavior", lambda data: data.get("streaks", {}).get("longest")),
        ("transition_probabilities", lambda data: {key: item["rate"] for key, item in data.get("transitions", {}).get("conditional_sequences", {}).items() if len(key) <= 2}),
        ("volatility", lambda data: data.get("multiplier", {}).get("log_population_stddev")),
    ):
        left, right = get_val(current), get_val(previous)
        changes.append({"metric": title, "current": left, "previous": right, "changed": left != right})
    current_patterns = set(current.get("transitions", {}).get("conditional_sequences", {}))
    previous_patterns = set(previous.get("transitions", {}).get("conditional_sequences", {}))
    changes.append({"metric": "patterns_appeared_disappeared",
                    "appeared": sorted(current_patterns - previous_patterns),
                    "disappeared": sorted(previous_patterns - current_patterns)})
    if min(current_n, previous_n) < MIN_PATTERN_SAMPLE:
        classification = "INSUFFICIENT_DATA"
    else:
        ci_now = current.get("at_least_2x", {}).get("confidence_95", {})
        ci_prev = previous.get("at_least_2x", {}).get("confidence_95", {})
        overlap = ci_now.get("lower") is None or ci_prev.get("lower") is None or max(ci_now["lower"], ci_prev["lower"]) <= min(ci_now["upper"], ci_prev["upper"])
        classification = "STABLE" if overlap else "CHANGING"
    return {"classification": classification, "previous_report_id": previous_report.get("report_id"),
            "changes": changes,
            "interpretation": "Descriptive period comparison only; overlap/non-overlap of Wilson intervals is a conservative heuristic, not a causal or predictive claim."}


def build_report(report_type: str, rows: list[dict[str, Any]], *, start_time: str | None,
                 end_time: str | None, data_source: str, quality: dict[str, Any] | None = None,
                 previous: dict[str, Any] | None = None, period_key: str | None = None,
                 generated_at: datetime | None = None) -> dict[str, Any]:
    normalized, _, _ = _round_values(rows)
    if not normalized:
        raise ValueError("Cannot generate a report without valid stored rounds")
    started = time.perf_counter()
    generated = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc).isoformat()
    first, last = normalized[0], normalized[-1]
    identity = f"{report_type}|{period_key or ''}|{first['round_id']}|{last['round_id']}|{len(normalized)}"
    report_id = f"analytics-{report_type.lower()}-{hashlib.sha256(identity.encode()).hexdigest()[:24]}"
    analytics = analyze_rounds(normalized, quality)
    analytics["comparison"] = compare_summaries(analytics, previous)
    report = {"report_id": report_id, "report_type": report_type,
            "period": period_key, "period_key": period_key,
            "start_time": start_time or first["timestamp"], "end_time": end_time or last["timestamp"],
            "period_start": start_time or first["timestamp"], "period_end": end_time or last["timestamp"],
            "first_round": first["round_id"], "last_round": last["round_id"],
            "first_round_index": first["round_index"], "last_round_index": last["round_index"],
            "round_count": len(normalized), "data_source": data_source,
            "generated_at": generated, "generation_time_ms": None,
            "data_quality": {**(quality or {}), "valid_report_rounds": len(normalized),
                             "duplicates_excluded": max(0, len(rows) - len(normalized)),
                             "timestamps_reliable": all(row["timestamp"] is not None for row in normalized)},
            "analysis": analytics,
            "scope": {"definition": "Validated, unique, positive finite multipliers from persisted Aviator round history.",
                      "threshold": "L means <2.00x; H means >=2.00x.",
                      "warning": "Historical frequencies do not imply the next round must reverse or follow a pattern. This report does not authorize bets."}}
    report["generation_time_ms"] = round((time.perf_counter() - started) * 1000, 3)
    return report


def completed_daily_windows(rows: list[dict[str, Any]], now: datetime | None = None) -> list[tuple[str, datetime, datetime, list[dict[str, Any]]]]:
    """Return completed UTC dates with at least one actual timestamped round."""
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    windows: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        stamp = pd_timestamp(row.get("timestamp"))
        if stamp is not None:
            windows[stamp.date().isoformat()].append(row)
    ready = []
    for day, selected in sorted(windows.items()):
        start = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
        end = start + timedelta(days=1)
        if end <= now:
            ready.append((day, start, end, selected))
    return ready


def next_round_blocks(rows: list[dict[str, Any]], block_size: int = 1000) -> list[tuple[int, list[dict[str, Any]]]]:
    """Deterministic, non-overlapping snapshots of complete valid-round blocks."""
    clean, _, _ = _round_values(rows)
    return [(offset // block_size + 1, clean[offset:offset + block_size])
            for offset in range(0, len(clean) - block_size + 1, block_size)]


def _prefix_rows(rows: list[dict[str, Any]], through_round_id: str) -> list[dict[str, Any]]:
    clean, _, _ = _round_values(rows)
    for index, row in enumerate(clean):
        if row["round_id"] == str(through_round_id):
            return clean[:index + 1]
    raise KeyError(f"round_id not found in validated persisted history: {through_round_id}")


def past_only_evidence(rows: list[dict[str, Any]], through_round_id: str) -> dict[str, Any]:
    """Evidence through N only, tagged for research of N+1; never includes N+1."""
    prefix = _prefix_rows(rows, through_round_id)
    analytics = analyze_rounds(prefix)
    return {"data_available_through_round": prefix[-1]["round_id"],
            "data_available_through_index": prefix[-1]["round_index"],
            "prediction_scope": "N+1 after data through N; research evidence only",
            "future_round_included": False,
            "evidence": {"baseline_p_h": analytics["baseline_p_h"],
                         "windows": analytics["windows"],
                         "transitions": analytics["transitions"],
                         "streaks": analytics["streaks"]}}


class AnalyticsReportEngine:
    """Schedules durable report snapshots using Repository as immutable storage."""

    def __init__(self, repository, dataset_service):
        self.repository = repository
        self.dataset_service = dataset_service

    def _rows(self) -> list[dict[str, Any]]:
        frame = self.dataset_service.clean_rounds
        if frame is None or frame.empty:
            return []
        return [{"round_id": str(row.round_id), "round_index": int(row.round_index),
                 "multiplier": float(row.multiplier), "timestamp": row.timestamp}
                for row in frame.itertuples(index=False)]

    def _quality(self) -> dict[str, Any]:
        quality = self.dataset_service.quality or {}
        if hasattr(quality, "model_dump"):
            quality = quality.model_dump()
        return dict(quality)

    def generate_due_reports(self, now: datetime | None = None) -> dict[str, Any]:
        now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
        rows = self._rows()
        if not rows:
            return {"generated": [], "status": "NO_VALID_STORED_ROUNDS"}
        source = "postgresql.aviator_rounds" if self.repository.database_url else "repository.validated_rounds"
        quality = self._quality()
        generated: list[str] = []

        # Completed UTC calendar days are exact 24-hour periods. No synthetic
        # timestamps are assigned: untimestamped rounds cannot enter these.
        known_daily = self.repository.list_analytics_report_periods("DAILY_REPORT")
        previous_daily = self.repository.list_analytics_reports("DAILY_REPORT", 1)
        for day, start, end, selected in completed_daily_windows(rows, now):
            if day in known_daily:
                continue
            report_quality = {**quality, "period_timestamped_rounds": len(selected),
                              "period_valid_rounds": len(_round_values(selected)[0])}
            report = build_report("DAILY_REPORT", selected, start_time=start.isoformat(), end_time=end.isoformat(),
                                  data_source=source, quality=report_quality,
                                  previous=previous_daily[0] if previous_daily else None,
                                  period_key=day, generated_at=now)
            if self.repository.save_analytics_report(report):
                generated.append(report["report_id"])
                previous_daily = [report]

        # Keep both review scales: quicker 100-round reports and the original
        # 1,000-round reports. Each uses independent immutable block keys.
        for report_type, block_size in (("ROUND_100_REPORT", 100), ("ROUND_REPORT", 1000)):
            known_blocks = self.repository.list_analytics_report_periods(report_type)
            previous_blocks = self.repository.list_analytics_reports(report_type, 1)
            for block_number, block in next_round_blocks(rows, block_size):
                # Idempotency follows source-round boundaries. A stored report
                # stays unchanged if late data is subsequently added.
                first_idx, last_idx = block[0]["round_index"], block[-1]["round_index"]
                period_key = f"{block_number}:{first_idx}-{last_idx}"
                if period_key in known_blocks:
                    continue
                report = build_report(report_type, block, start_time=block[0]["timestamp"],
                                      end_time=block[-1]["timestamp"], data_source=source,
                                      quality={**quality, "block_number": block_number,
                                               "block_round_count": len(block),
                                               "scheduled_block_size": block_size},
                                      previous=previous_blocks[0] if previous_blocks else None,
                                      period_key=period_key, generated_at=now)
                if self.repository.save_analytics_report(report):
                    generated.append(report["report_id"])
                    previous_blocks = [report]
        return {"generated": generated, "status": "GENERATED" if generated else "UP_TO_DATE",
                "observed_valid_rounds": len(rows), "checked_at": now.isoformat()}

    def report_progress_snapshot(self) -> dict[str, Any]:
        """Small progress payload for live collection updates; no analysis scan."""
        frame = self.dataset_service.clean_rounds
        count = 0 if frame is None else len(frame)

        def progress(report_type: str, block_size: int) -> dict[str, Any]:
            completed, remainder = divmod(count, block_size)
            latest_report = None
            missing_completed_report = False

            def find_block(block_number: int) -> dict[str, Any] | None:
                if block_number < 1:
                    return None
                first = frame.iloc[(block_number - 1) * block_size]
                last = frame.iloc[block_number * block_size - 1]
                period_key = f"{block_number}:{int(first.round_index)}-{int(last.round_index)}"
                return self.repository.get_analytics_report_by_period(report_type, period_key)

            if completed:
                latest_report = find_block(completed)
                missing_completed_report = latest_report is None
                if latest_report is None and completed > 1:
                    latest_report = find_block(completed - 1)

            pending = bool(completed and missing_completed_report)
            collected = block_size if pending and remainder == 0 else remainder
            remaining = 0 if pending and remainder == 0 else (block_size - remainder if remainder else block_size)
            preview = None
            if latest_report:
                analysis = latest_report.get("analysis") or {}
                preview = {key: latest_report.get(key) for key in
                           ("report_id", "report_type", "first_round", "last_round", "round_count", "generated_at")}
                preview["under_2x"] = analysis.get("under_2x")
                preview["at_least_2x"] = analysis.get("at_least_2x")
            return {"block_size": block_size, "rounds_collected": collected,
                    "rounds_remaining": remaining, "progress_percent": round(100 * collected / block_size, 1),
                    "status": "REPORT_PENDING" if pending else ("COLLECTING" if count else "NO_VALID_ROUNDS"),
                    "latest_report": preview}

        return {"valid_round_count": count,
                "every_100": progress("ROUND_100_REPORT", 100),
                "every_1000": progress("ROUND_REPORT", 1000)}

    def current_snapshot(self) -> dict[str, Any]:
        rows = self._rows()
        quality = self._quality()
        def summarize_view(selected_rows: list[dict[str, Any]], selected_quality: dict[str, Any] | None = None) -> dict[str, Any]:
            result = analyze_rounds(selected_rows, selected_quality, include_details=False)
            result["first_round"] = selected_rows[0]["round_id"] if selected_rows else None
            result["last_round"] = selected_rows[-1]["round_id"] if selected_rows else None
            return result

        # Current dashboard views only need concise summaries. Full cross-block
        # drift, rolling series and persistence calculations belong in saved
        # reports; repeating them for every request caused the 20s API timeout.
        current = summarize_view(rows, quality)
        current["windows"] = {
            str(window): summarize_view(rows[-window:])
            for window in WINDOWS
        }
        now = datetime.now(timezone.utc)
        last_24 = [row for row in rows if (stamp := pd_timestamp(row.get("timestamp"))) is not None
                   and now - timedelta(hours=24) <= stamp <= now]
        today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        today = [row for row in rows if (stamp := pd_timestamp(row.get("timestamp"))) is not None
                 and today_start <= stamp <= now]

        return {"generated_at": now.isoformat(), "data_source": "postgresql.aviator_rounds" if self.repository.database_url else "repository.validated_rounds",
                "today": summarize_view(today, {"period": "UTC day so far", "timestamped_rounds": len(today)}),
                "last_24_hours": summarize_view(last_24, {"period": "rolling 24 hours", "timestamped_rounds": len(last_24)}),
                "historical": current,
                "latest_100": summarize_view(rows[-100:], {"selected_rounds": min(100, len(rows)), "available": len(rows) >= 100}),
                "latest_1000": summarize_view(rows[-1000:], {"selected_rounds": min(1000, len(rows)), "available": len(rows) >= 1000}),
                "reports": self.repository.list_analytics_reports(limit=20),
                "report_progress": self.report_progress_snapshot(),
                "valid_round_count": len(rows),
                "data_available_through_round": rows[-1]["round_id"] if rows else None}

    def research_features_through(self, through_round_id: str) -> dict[str, Any]:
        return past_only_evidence(self._rows(), through_round_id)

    def past_round_snapshot(self, through_round_id: str) -> dict[str, Any]:
        prefix = _prefix_rows(self._rows(), through_round_id)
        analysis = analyze_rounds(prefix, {"validated_prefix_rounds": len(prefix)})
        return {"report_type": "PAST_ROUND_SNAPSHOT", "persisted": False,
                "start_time": prefix[0]["timestamp"], "end_time": prefix[-1]["timestamp"],
                "first_round": prefix[0]["round_id"], "last_round": prefix[-1]["round_id"],
                "round_count": len(prefix), "data_source": "postgresql.aviator_rounds" if self.repository.database_url else "repository.validated_rounds",
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "data_available_through_round": prefix[-1]["round_id"],
                "prediction_scope": "research cutoff through round N only; no N+1 outcome included",
                "analysis": analysis}

    def round_options(self, limit: int = 100, before_round_index: int | None = None) -> dict[str, Any]:
        rows = self._rows()
        if before_round_index is not None:
            rows = [row for row in rows if row["round_index"] < before_round_index]
        selected = rows[-min(max(int(limit), 1), 500):]
        selected_ids = {row["round_id"] for row in selected}
        has_older = any(row["round_id"] not in selected_ids for row in rows)
        return {"rounds": list(reversed(selected)),
                "next_before_round_index": selected[0]["round_index"] if has_older and selected else None,
                "count": len(selected), "has_older": has_older,
                "data_source": "postgresql.aviator_rounds" if self.repository.database_url else "repository.validated_rounds"}
