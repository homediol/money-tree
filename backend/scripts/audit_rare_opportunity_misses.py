"""Evidence-led audit of missed targets in the active Frozen V3 experiment.

Run from backend/: ``.venv/bin/python scripts/audit_rare_opportunity_misses.py``.
Read-only: it does not update experiment records.
"""
from __future__ import annotations

import json
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core.config import get_settings
from app.database.repository import Repository

ROOT = Path(__file__).resolve().parents[2]
EXPERIMENT_ID = "2352d0681f324190abe9c2c0e860a6c3"
REASONS = ("OBSERVER_TOO_SLOW", "TARGET_ARRIVED_BEFORE_ASSESSMENT", "FEATURES_UNAVAILABLE",
           "CONTINUITY_FAILURE", "DATABASE_LATENCY", "OBSERVER_RESTART", "COLLECTOR_DELAY",
           "IDENTITY_UNVERIFIED", "MODEL_ERROR", "OTHER")


def parse_time(value):
    if not value:
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def read_backend_intervals(path: Path):
    starts = {}
    intervals = []
    if not path.exists():
        return intervals
    for line in path.read_text(errors="replace").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        event = row.get("event")
        pid = row.get("pid")
        stamp = parse_time(row.get("timestamp"))
        if not stamp or pid is None:
            continue
        pid = int(pid)
        if event == "backend_start":
            # Enforce the single-owner invariant when old lifecycle shutdown
            # events are absent: a new owner supersedes any prior start.
            for old_pid, old_start in list(starts.items()):
                if old_pid != pid:
                    intervals.append((old_start, stamp))
                    starts.pop(old_pid, None)
            if pid in starts:
                intervals.append((starts.pop(pid), stamp))
            starts[pid] = stamp
        elif event in {"backend_shutdown", "supervisor_exit"} and pid in starts:
            intervals.append((starts.pop(pid), stamp))
    now = datetime.now(timezone.utc)
    intervals.extend((start, now) for start in starts.values())
    return intervals


def read_collector_events(path: Path):
    events = []
    if not path.exists():
        return events
    prefix = re.compile(r"^\[(\d{4}-\d\d-\d\dT[^\]]+)\]")
    for line in path.read_text(errors="replace").splitlines():
        match = prefix.match(line)
        if not match:
            continue
        stamp = parse_time(match.group(1))
        if not stamp:
            continue
        if "State:" in line and "→ COLLECTING" in line:
            events.append((stamp, True))
        elif ("State:" in line and any(state in line for state in ("→ RECOVERING", "→ RELOGIN", "→ STOPPED"))
              or "=== Collector stopped" in line or "=== Aviator Collector starting ===" in line):
            events.append((stamp, False))
    return sorted(events)


def active_at(stamp, intervals):
    return any(start <= stamp <= end for start, end in intervals)


def collector_active_at(stamp, events):
    state = None
    for event_time, active in events:
        if event_time > stamp:
            break
        state = active
    return state


def main():
    settings = get_settings()
    repository = Repository(settings.database_path, settings.database_url, require_postgres=True)
    backend_intervals = read_backend_intervals(ROOT / "data" / "backend_lifecycle.jsonl")
    collector_events = read_collector_events(ROOT / "data" / "bot" / "collector.log")
    with repository.connect() as conn:
        rows = conn.execute("""SELECT e.target_round_index,e.observed_ordinal,e.reason AS stored_reason,
                src.round_id AS source_round_id,src.round_index AS source_round_index,
                src.observed_at AS source_observed_at,src.stored_at AS source_stored_at,
                src.continuity_verified AS source_continuity,src.gap_before AS source_gap,
                src.identity_confidence AS source_identity,
                dst.round_id AS target_round_id,dst.observed_at AS target_observed_at,
                dst.stored_at AS target_stored_at,dst.continuity_verified AS target_continuity,
                dst.gap_before AS target_gap,dst.identity_confidence AS target_identity,
                a.payload AS assessment_payload
            FROM opportunity_experiment_targets e
            LEFT JOIN aviator_rounds dst ON dst.round_index=e.target_round_index
            LEFT JOIN aviator_rounds src ON src.round_index=e.target_round_index-1
            LEFT JOIN opportunity_v4_assessments a ON a.model_version=e.model_version
                AND a.round_index=e.target_round_index-1
            WHERE e.experiment_id=? AND e.classification='MISSED'
            ORDER BY e.target_round_index""", (EXPERIMENT_ID,)).fetchall()
    by_reason = defaultdict(list)
    by_period = {"initial_69_targets": defaultdict(list), "after_initial_69": defaultdict(list)}
    details = []
    for row in rows:
        d = dict(row)
        source_at = parse_time(d.get("source_stored_at")) or parse_time(d.get("source_observed_at"))
        target_at = parse_time(d.get("target_observed_at"))
        reason = "OTHER"
        evidence = "missing round-order evidence"
        if not d.get("target_round_id") or not d.get("source_round_id"):
            reason, evidence = "OTHER", "source or target round row unavailable"
        elif not bool(d.get("source_continuity")) or not bool(d.get("target_continuity")) or bool(d.get("target_gap")):
            reason, evidence = "CONTINUITY_FAILURE", "continuity flags do not verify source→target"
        elif str(d.get("source_identity") or "").upper() in {"UNKNOWN", "UNVERIFIED"} or str(d.get("target_identity") or "").upper() in {"UNKNOWN", "UNVERIFIED"}:
            reason, evidence = "IDENTITY_UNVERIFIED", "round identity confidence is unknown"
        elif d.get("assessment_payload"):
            assessment = json.loads(d["assessment_payload"])
            assessment_at = parse_time(assessment.get("created_at") or assessment.get("observed_at"))
            if target_at and assessment_at and assessment_at >= target_at:
                reason, evidence = "TARGET_ARRIVED_BEFORE_ASSESSMENT", "assessment timestamp is at/after target observed_at"
            elif not assessment.get("scorable"):
                reason, evidence = "FEATURES_UNAVAILABLE", str(assessment.get("failed_gate") or assessment.get("failed_reason") or "assessment was not scorable")
            else:
                reason, evidence = "OTHER", "source assessment exists but experiment proof/association is invalid"
        elif source_at and target_at and source_at >= target_at:
            reason, evidence = "COLLECTOR_DELAY", "source became available in PostgreSQL at/after target observation"
        elif source_at and not active_at(source_at, backend_intervals):
            reason, evidence = "OBSERVER_RESTART", "backend lifecycle has no running backend at source availability time"
        elif source_at and collector_active_at(source_at, collector_events) is False:
            reason, evidence = "COLLECTOR_DELAY", "collector lifecycle shows recovery/stopped at source availability time"
        elif source_at and target_at:
            reason = "OBSERVER_TOO_SLOW"
            evidence = "source was stored before target, backend was running, but latest-marker observer has no durable source queue"
        by_reason[reason].append(int(d["target_round_index"]))
        period = "initial_69_targets" if int(d.get("observed_ordinal") or 0) <= 69 else "after_initial_69"
        by_period[period][reason].append(int(d["target_round_index"]))
        details.append({"target": int(d["target_round_index"]), "reason": reason, "evidence": evidence,
                        "source_stored_at": str(d.get("source_stored_at")),
                        "target_observed_at": str(d.get("target_observed_at"))})
    counts = Counter({reason: len(by_reason[reason]) for reason in REASONS})
    period_counts = {period: {reason: len(indexes) for reason, indexes in counts_by_reason.items()}
                     for period, counts_by_reason in by_period.items()}
    print(json.dumps({"experiment_id": EXPERIMENT_ID, "missed_total": len(rows),
                      "missed_in_initial_69_targets": sum(len(items) for items in by_period["initial_69_targets"].values()),
                      "missed_after_initial_69": sum(len(items) for items in by_period["after_initial_69"].values()),
                      "reason_counts": counts, "reason_counts_by_period": period_counts,
                      "target_indexes_by_reason": dict(by_reason),
                      "target_indexes_by_period": {period: dict(mapping) for period, mapping in by_period.items()},
                      "details": details if "--details" in __import__("sys").argv else None},
                     indent=2, default=str))


if __name__ == "__main__":
    main()
