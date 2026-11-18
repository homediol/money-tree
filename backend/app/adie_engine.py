"""Deterministic, prospective Adaptive Dominance Intelligence for SHADOW.

ADIE is an observer of the existing verified round stream. It never executes
actions and intentionally starts with a locked, abstention-first policy because
Phase 2 found no corrected predictive edge.
"""
from __future__ import annotations

import json
import csv
import gzip
import hashlib
import logging
import math
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

log = logging.getLogger(__name__)

POLICY_CONFIG_PATH = Path(__file__).parent / "resources" / "adie_policy_v1.json"
try:
    POLICY_CONFIG_BYTES = POLICY_CONFIG_PATH.read_bytes()
    POLICY_CONFIG = json.loads(POLICY_CONFIG_BYTES)
    POLICY_CONFIG_HASH = hashlib.sha256(POLICY_CONFIG_BYTES).hexdigest()
except (OSError, ValueError):
    POLICY_CONFIG = {}
    POLICY_CONFIG_HASH = "CONFIG_UNAVAILABLE"
POLICY_VERSION = str(POLICY_CONFIG.get("policy_version") or "adie-shadow-exploratory-v1")
COHORT = str(POLICY_CONFIG.get("cohort") or "prospective-2026-11-16")
HORIZONS = tuple(int(value) for value in POLICY_CONFIG.get("horizons", [5, 10, 20, 30, 50]))
NORMAL_HORIZONS = tuple(int(value) for value in POLICY_CONFIG.get("primary_horizons", [5, 10]))
FORCE_AFTER = int(POLICY_CONFIG.get("activity_coverage", {}).get("force_after_eligible_rounds", 100))
WATCH_AFTER = int(POLICY_CONFIG.get("activity_coverage", {}).get("watch_after_eligible_rounds", 80))
MAX_AGE_SECONDS = float(POLICY_CONFIG.get("eligibility", {}).get("max_observation_age_seconds", 15))
FUTURE_TOLERANCE_SECONDS = float(POLICY_CONFIG.get("eligibility", {}).get("future_timestamp_tolerance_seconds", 2))
DOMINANCE_HIGH_THRESHOLD = float(POLICY_CONFIG.get("dominance_definition", {}).get("high_dominance_min_high_share", .6))
DOMINANCE_LOW_THRESHOLD = float(POLICY_CONFIG.get("dominance_definition", {}).get("low_dominance_max_high_share", .4))
MAX_CONTEXT_NEIGHBORS = int(POLICY_CONFIG.get("research_reference", {}).get("maximum_neighbors", 25))
CONTEXT_POSITION_SPACING = int(POLICY_CONFIG.get("research_reference", {}).get("minimum_historical_position_spacing", 60))
_REFERENCE_CACHE = None
_REFERENCE_CACHE_LOCK = threading.Lock()
PRIOR_HIGH_RATE = 25966 / (25966 + 27398)
RESEARCH = {
    "phase": "research_phase2_2026-11-16",
    "canonical_rounds": 53503,
    "high": 25966,
    "low": 27398,
    "exact": 139,
    "corrected_features_passed": 0,
    "corrected_interactions_passed": 0,
    "corrected_primary_window_comparisons_passed": 0,
    "high_5_offset_6_q": 0.118,
    "width20_persistence_q": 0.054,
    "predictive_status": "NO_VALIDATED_EDGE",
    "findings_path": "research_phase2_2026-11-16/reports/machine_readable_findings.json",
}


def classify_multiplier(multiplier: float) -> str:
    value = float(multiplier)
    if not math.isfinite(value) or value < 1.0:
        raise ValueError("multiplier_must_be_finite_and_at_least_1x")
    if value > 2.0:
        return "HIGH"
    if value < 2.0:
        return "LOW"
    return "EXACT"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _parse(value: Any, fallback=None):
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback


def _ensure_schema(repository) -> None:
    """Create ADIE ledgers in the existing repository database."""
    ddl = [
        """CREATE TABLE IF NOT EXISTS adie_state (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), state_json TEXT NOT NULL,
            updated_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS adie_round_events (
            round_id TEXT PRIMARY KEY, position BIGINT NOT NULL UNIQUE, stream_id TEXT,
            stream_sequence BIGINT, multiplier DOUBLE PRECISION NOT NULL, outcome TEXT NOT NULL,
            observed_at TEXT, received_at TEXT, verified_at TEXT, committed_at TEXT, delivery_mode TEXT,
            continuity_status TEXT NOT NULL, identity_source TEXT, source_round_index BIGINT,
            source_platform_index BIGINT, fingerprint_json TEXT NOT NULL, event_json TEXT NOT NULL,
            created_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS adie_decisions (
            decision_id TEXT PRIMARY KEY, round_id TEXT NOT NULL UNIQUE, position BIGINT NOT NULL,
            policy_version TEXT NOT NULL, cohort TEXT NOT NULL, eligible BOOLEAN NOT NULL,
            abstention_reason TEXT, context_state TEXT, eligible_since_selection INTEGER NOT NULL,
            features_json TEXT NOT NULL, candidates_json TEXT NOT NULL, selected_forecast_id TEXT,
            decision_at TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS adie_forecasts (
            forecast_id TEXT PRIMARY KEY, decision_id TEXT NOT NULL UNIQUE, decision_round_id TEXT NOT NULL,
            decision_position BIGINT NOT NULL, direction TEXT NOT NULL, horizon INTEGER NOT NULL,
            start_offset INTEGER NOT NULL, target_start_position BIGINT NOT NULL,
            target_end_position BIGINT NOT NULL, target_start_round_id TEXT, target_end_round_id TEXT,
            status TEXT NOT NULL, selection_status TEXT NOT NULL, policy_version TEXT NOT NULL,
            reason TEXT NOT NULL, evidence_json TEXT NOT NULL, created_at TEXT NOT NULL,
            committed_at TEXT NOT NULL, activated_at TEXT, completed_at TEXT,
            target_start_observed_at TEXT, target_end_observed_at TEXT,
            high_count INTEGER NOT NULL DEFAULT 0, low_count INTEGER NOT NULL DEFAULT 0,
            exact_count INTEGER NOT NULL DEFAULT 0, wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0, net_units INTEGER NOT NULL DEFAULT 0,
            positive_net BOOLEAN, invalidation_reason TEXT)""",
        """CREATE INDEX IF NOT EXISTS adie_forecasts_status_target_idx
            ON adie_forecasts(status,target_start_position,target_end_position)""",
        """CREATE TABLE IF NOT EXISTS adie_forecast_rounds (
            forecast_id TEXT NOT NULL, position BIGINT NOT NULL, round_id TEXT NOT NULL,
            multiplier DOUBLE PRECISION NOT NULL, outcome TEXT NOT NULL, accounted_win BOOLEAN NOT NULL,
            recorded_at TEXT NOT NULL, PRIMARY KEY(forecast_id,position), UNIQUE(forecast_id,round_id))""",
        """CREATE TABLE IF NOT EXISTS adie_baseline_windows (
            baseline_id TEXT PRIMARY KEY, decision_round_id TEXT NOT NULL, decision_position BIGINT NOT NULL,
            direction TEXT NOT NULL, horizon INTEGER NOT NULL, start_position BIGINT NOT NULL,
            end_position BIGINT NOT NULL, start_round_id TEXT, end_round_id TEXT,
            decision_at TEXT NOT NULL, status TEXT NOT NULL, high_count INTEGER NOT NULL DEFAULT 0, low_count INTEGER NOT NULL DEFAULT 0,
            exact_count INTEGER NOT NULL DEFAULT 0, wins INTEGER NOT NULL DEFAULT 0,
            losses INTEGER NOT NULL DEFAULT 0, net_units INTEGER NOT NULL DEFAULT 0,
            positive_net BOOLEAN, UNIQUE(decision_round_id,direction,horizon))""",
        """CREATE TABLE IF NOT EXISTS adie_baseline_window_rounds (
            baseline_id TEXT NOT NULL, position BIGINT NOT NULL, round_id TEXT NOT NULL,
            multiplier DOUBLE PRECISION NOT NULL, outcome TEXT NOT NULL, accounted_win BOOLEAN NOT NULL,
            PRIMARY KEY(baseline_id,position), UNIQUE(baseline_id,round_id))""",
        "CREATE INDEX IF NOT EXISTS adie_baseline_status_idx ON adie_baseline_windows(status,start_position,end_position)",
        """CREATE INDEX IF NOT EXISTS adie_decisions_policy_idx ON adie_decisions(policy_version,position)""",
        """CREATE INDEX IF NOT EXISTS adie_events_position_idx ON adie_round_events(position)""",
    ]
    with repository.connect() as conn:
        for statement in ddl:
            conn.execute(statement)
        conn.execute("INSERT INTO adie_state(singleton,state_json,updated_at) VALUES(1,?,?) ON CONFLICT(singleton) DO NOTHING",
                     (_json({"position": 0, "eligible_since_selection": 0,
                             "stream_id": None, "stream_sequence": 0,
                             "last_round_id": None, "health": "WARMING_UP"}), _now()))


class ADIEEngine:
    """Idempotent event processor; caller serializes observed stream order."""

    def __init__(self, repository):
        self.repository = repository
        self._lock = threading.RLock()
        self.ready = False
        self.last_error: str | None = None
        self.reference_rows: list[dict[str, Any]] = []
        self.reference_manifest: dict[str, Any] = {}
        self.reference_matrix = np.empty((0, 6), dtype=float)
        self.reference_positions = np.empty((0,), dtype=np.int64)
        self._load_reference()
        self._ensure()

    def _load_reference(self):
        global _REFERENCE_CACHE
        with _REFERENCE_CACHE_LOCK:
            if _REFERENCE_CACHE is not None:
                (self.reference_rows, self.reference_matrix,
                 self.reference_positions, self.reference_manifest) = _REFERENCE_CACHE
                return
        path = Path(__file__).parent / "resources" / "adie_research_reference.csv.gz"
        manifest_path = path.with_name("adie_research_reference_manifest.json")
        if manifest_path.exists():
            try:
                self.reference_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.reference_manifest = {"status": "MANIFEST_INVALID"}
        if not path.exists():
            self.reference_manifest.setdefault("status", "REFERENCE_MISSING")
            return
        expected = self.reference_manifest.get("artifact_sha256")
        if expected:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest != expected:
                self.reference_manifest["status"] = "REFERENCE_HASH_MISMATCH"
                return
            self.reference_manifest["status"] = "VERIFIED"
        else:
            self.reference_manifest["status"] = "MANIFEST_HASH_MISSING"
            return
        matrix = []
        positions = []
        with gzip.open(path, "rt", newline="", encoding="utf-8") as source:
            for row in csv.DictReader(source):
                def val(key, default=0.5):
                    try:
                        number = float(row.get(key, ""))
                        return number if math.isfinite(number) else default
                    except (TypeError, ValueError):
                        return default
                matrix.append([
                    val("prev_20_high_share_nonexact"), val("prev_50_high_share_nonexact"),
                    val("prev_100_high_share_nonexact"),
                    val("current_streak_length", 0) / 10,
                    val("prev_50_hl_alternation_rate"),
                    val("prev_50_log_sd", 0),
                ])
                positions.append(int(row["decision_position"]))
                self.reference_rows.append(row)
        self.reference_matrix = np.asarray(matrix, dtype=np.float32)
        self.reference_positions = np.asarray(positions, dtype=np.int64)
        with _REFERENCE_CACHE_LOCK:
            if _REFERENCE_CACHE is None:
                _REFERENCE_CACHE = (self.reference_rows, self.reference_matrix,
                                    self.reference_positions, self.reference_manifest)

    def _similar_contexts(self, features: dict[str, Any]) -> list[dict[str, Any]]:
        if not len(self.reference_matrix):
            return []
        rolling = features["rolling"]
        def share(size):
            value = rolling[str(size)]["high_share_nonexact"]
            return .5 if value is None else float(value)
        vector = np.asarray([
            share(20), share(50), share(100),
            features.get("last_streak_length", 0) / 10,
            features.get("alternation_rate_50") if features.get("alternation_rate_50") is not None else .5,
            features.get("log_multiplier_sd_50") or 0,
        ], dtype=np.float32)
        distances = np.linalg.norm(self.reference_matrix - vector, axis=1)
        nearest = np.argsort(distances)[:MAX_CONTEXT_NEIGHBORS * 4]
        selected = []
        for idx in nearest:
            pos = int(self.reference_positions[idx])
            # Separate contexts by 60 source rounds to reduce direct overlap
            # among the largest historical target windows.
            if any(abs(pos - item["position"]) < CONTEXT_POSITION_SPACING for item in selected):
                continue
            row = self.reference_rows[int(idx)]
            selected.append({"position": pos, "distance": float(distances[idx]), "row": row})
            if len(selected) >= MAX_CONTEXT_NEIGHBORS:
                break
        return selected

    def _context_evidence(self, contexts: list[dict[str, Any]]) -> dict[str, Any]:
        result = {"neighbor_count": len(contexts), "distance_mean": None, "horizons": {},
                  "nearest_historical_decisions": [
                      {"position": int(item["position"]), "distance": round(float(item["distance"]), 6)}
                      for item in contexts[:10]]}
        if contexts:
            result["distance_mean"] = sum(item["distance"] for item in contexts) / len(contexts)
        for horizon in HORIZONS:
            rows = []
            for item in contexts:
                row = item["row"]
                try:
                    samples = int(float(row.get(f"matured_n_{horizon}") or 0))
                    h = float(row.get(f"context_H_net_per_round_{horizon}") or "nan")
                    l = float(row.get(f"context_L_net_per_round_{horizon}") or "nan")
                    if samples and math.isfinite(h) and math.isfinite(l):
                        rows.append((h, l, samples))
                except (TypeError, ValueError):
                    pass
            result["horizons"][str(horizon)] = {
                "context_count": len(rows),
                "matured_context_samples": sum(row[2] for row in rows),
                "high_net_per_round_mean": sum(row[0] for row in rows) / len(rows) if rows else None,
                "low_net_per_round_mean": sum(row[1] for row in rows) / len(rows) if rows else None,
                "note": "retrospective, overlapping context aggregates; exploratory only",
            }
        return result

    def _ensure(self):
        _ensure_schema(self.repository)
        self._seed_live_context()
        self.ready = True

    def _seed_live_context(self):
        """Seed features from the existing canonical verified suffix once."""
        with self.repository.connect() as conn:
            row = conn.execute("SELECT state_json FROM adie_state WHERE singleton=1").fetchone()
            state = _parse(row["state_json"], {}) if row else {}
            if state.get("context_seeded"):
                return
        seed = []
        try:
            source = self.repository.load_recent_round_context(500)
            suffix = []
            for record in reversed(source):
                if not bool(record.get("continuity_verified")) or bool(record.get("gap_before")):
                    break
                try:
                    multiplier = float(record["multiplier"])
                    round_id = str(record.get("round_id") or "")
                    if not round_id:
                        break
                    suffix.append({"round_id": round_id, "multiplier": multiplier,
                                   "outcome": classify_multiplier(multiplier),
                                   "observed_at": record.get("observed_at") or record.get("timestamp"),
                                   "source_round_index": record.get("round_index")})
                except (TypeError, ValueError):
                    break
            seed = list(reversed(suffix))[-200:]
        except Exception:
            # Historical feature warm-up is optional; the live verified stream
            # remains the source of durable ADIE decisions.
            log.warning("ADIE could not load bounded canonical context seed", exc_info=True)
        with self.repository.connect() as conn:
            row = conn.execute("SELECT state_json FROM adie_state WHERE singleton=1").fetchone()
            state = _parse(row["state_json"], {}) if row else {}
            if state.get("context_seeded"):
                return
            state["context_seeded"] = True
            state["seed_history"] = seed
            if seed:
                last = seed[-1]
                state["last_source_round_index"] = last.get("source_round_index")
                state["last_observed_at"] = last.get("observed_at")
                fingerprint = self._features(seed, float(last["multiplier"]))
                state["last_context"] = fingerprint["context_state"]
                state["seed_fingerprint"] = fingerprint
                state["seed_latest_round"] = last
            conn.execute("UPDATE adie_state SET state_json=?,updated_at=? WHERE singleton=1",
                         (_json(state), _now()))

    @staticmethod
    def _features(history: list[dict[str, Any]], multiplier: float) -> dict[str, Any]:
        seq = history[-200:]
        outcomes = [str(r["outcome"]) for r in seq]
        ratios = {}
        for size in (5, 10, 20, 30, 50, 100, 200):
            recent = outcomes[-size:]
            nonexact = sum(v in ("HIGH", "LOW") for v in recent)
            ratios[str(size)] = {
                "high": sum(v == "HIGH" for v in recent),
                "low": sum(v == "LOW" for v in recent),
                "exact": sum(v == "EXACT" for v in recent),
                "high_share_nonexact": (sum(v == "HIGH" for v in recent) / nonexact)
                    if nonexact else None,
            }
        streak = 0
        last = outcomes[-1] if outcomes else None
        for value in reversed(outcomes):
            if value != last:
                break
            streak += 1
        tail = [x for x in outcomes[-50:] if x != "EXACT"]
        alternations = sum(a != b for a, b in zip(tail, tail[1:]))
        alternation_rate = alternations / (len(tail) - 1) if len(tail) > 1 else None
        interrupted = {"high": 0, "low": 0}
        # Exploratory H{4..6}, L{1..3}, H{4..6} and inverse motifs.
        runs = []
        for val in outcomes:
            if val == "EXACT":
                if runs and runs[-1][0] == "EXACT": runs[-1][1] += 1
                else: runs.append([val, 1])
            elif runs and runs[-1][0] == val:
                runs[-1][1] += 1
            else:
                runs.append([val, 1])
        for i in range(2, len(runs)):
            a, b, c = runs[i - 2], runs[i - 1], runs[i]
            if a[0] == c[0] and a[0] in ("HIGH", "LOW") and b[0] in ("HIGH", "LOW"):
                if 4 <= a[1] <= 6 and 1 <= b[1] <= 3 and 4 <= c[1] <= 6:
                    interrupted["high" if a[0] == "HIGH" else "low"] += 1
        h20 = ratios["20"]["high_share_nonexact"]
        context = "HIGH_DOMINANCE" if h20 is not None and h20 >= DOMINANCE_HIGH_THRESHOLD else (
            "LOW_DOMINANCE" if h20 is not None and h20 <= DOMINANCE_LOW_THRESHOLD else "BALANCED_TRANSITION")
        log_mult = [math.log(max(1.0, float(r["multiplier"]))) for r in seq[-50:]]
        if log_mult:
            mean = sum(log_mult) / len(log_mult)
            log_sd = math.sqrt(sum((v - mean) ** 2 for v in log_mult) / len(log_mult))
        else:
            log_sd = None
        share5 = ratios["5"]["high_share_nonexact"]
        share10 = ratios["10"]["high_share_nonexact"]
        share20 = ratios["20"]["high_share_nonexact"]
        share50 = ratios["50"]["high_share_nonexact"]
        time_gap = None
        if len(seq) >= 2:
            try:
                previous_at = datetime.fromisoformat(str(seq[-2].get("observed_at") or "").replace("Z", "+00:00"))
                current_at = datetime.fromisoformat(str(seq[-1].get("observed_at") or "").replace("Z", "+00:00"))
                if previous_at.tzinfo and current_at.tzinfo:
                    time_gap = (current_at - previous_at).total_seconds()
            except (TypeError, ValueError):
                pass
        return {
            "context_state": context, "rolling": ratios, "last_outcome": last,
            "last_streak_length": streak, "alternation_rate_50": alternation_rate,
            "interrupted_clusters_50": interrupted,
            "last_multiplier": float(multiplier), "log_multiplier_sd_50": log_sd,
            "dominance_momentum": {
                "high_share_5_minus_20": share5 - share20 if share5 is not None and share20 is not None else None,
                "high_share_10_minus_50": share10 - share50 if share10 is not None and share50 is not None else None,
                "high_share_20_minus_50": share20 - share50 if share20 is not None and share50 is not None else None,
            },
            "seconds_from_previous_observation": time_gap,
            "history_rounds": len(seq),
        }

    @staticmethod
    def _interval(net_units: int, n: int) -> tuple[float | None, float | None]:
        if n <= 0:
            return None, None
        # Conservative Wilson interval on per-round win probability, converted
        # to net EV per stake (2*p-1); exploratory overlapping samples only.
        p = (net_units + n) / (2 * n)
        z = 1.96
        den = 1 + z*z/n
        center = (p + z*z/(2*n))/den
        half = z*math.sqrt((p*(1-p)/n) + z*z/(4*n*n))/den
        return 2*(center-half)-1, 2*(center+half)-1

    def _recent(self, conn, limit=200):
        rows = conn.execute("SELECT position,round_id,multiplier,outcome,observed_at FROM adie_round_events ORDER BY position DESC LIMIT ?",
                            (limit,)).fetchall()
        return [dict(row) for row in reversed(rows)]

    def _active_forecasts(self, conn):
        return conn.execute("SELECT * FROM adie_forecasts WHERE status IN ('LOCKED','WAITING','ACTIVE') ORDER BY committed_at").fetchall()

    def _apply_target(self, conn, forecast, row, position):
        expected_start = int(forecast["target_start_position"])
        expected_end = int(forecast["target_end_position"])
        if position < expected_start or position > expected_end:
            return
        if position == expected_start:
            try:
                observed = datetime.fromisoformat(str(row.get("observed_at") or "").replace("Z", "+00:00"))
                committed = datetime.fromisoformat(str(forecast["committed_at"]).replace("Z", "+00:00"))
                if observed.tzinfo is None or committed.tzinfo is None or observed <= committed:
                    raise ValueError("target_not_proven_after_commit")
            except (TypeError, ValueError, OverflowError):
                conn.execute("UPDATE adie_forecasts SET status='INCOMPLETE',invalidation_reason='target_start_not_proven_after_forecast_commit' WHERE forecast_id=?",
                             (forecast["forecast_id"],))
                return
        outcome = str(row["outcome"])
        won = outcome in ("HIGH", "EXACT") if forecast["direction"] == "HIGH" else outcome == "LOW"
        conn.execute("INSERT INTO adie_forecast_rounds(forecast_id,position,round_id,multiplier,outcome,accounted_win,recorded_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(forecast_id,position) DO NOTHING",
                     (forecast["forecast_id"], position, row["round_id"], row["multiplier"], outcome, won, _now()))
        counts = conn.execute("SELECT COUNT(*) n,SUM(CASE WHEN outcome='HIGH' THEN 1 ELSE 0 END) h,SUM(CASE WHEN outcome='LOW' THEN 1 ELSE 0 END) l,SUM(CASE WHEN outcome='EXACT' THEN 1 ELSE 0 END) e,SUM(CASE WHEN accounted_win THEN 1 ELSE 0 END) w FROM adie_forecast_rounds WHERE forecast_id=?",
                              (forecast["forecast_id"],)).fetchone()
        status = "ACTIVE" if position >= expected_start else "WAITING"
        activated = _now() if position == expected_start and not forecast["activated_at"] else forecast["activated_at"]
        complete = int(counts["n"] or 0) == int(forecast["horizon"])
        if complete:
            status = "COMPLETED"
        wins = int(counts["w"] or 0)
        losses = int(counts["n"] or 0) - wins
        net = wins - losses
        conn.execute("""UPDATE adie_forecasts SET status=?,activated_at=?,completed_at=?,
            high_count=?,low_count=?,exact_count=?,wins=?,losses=?,net_units=?,positive_net=?,
            target_start_round_id=CASE WHEN ?=? THEN ? ELSE target_start_round_id END,
            target_end_round_id=CASE WHEN ?=? THEN ? ELSE target_end_round_id END,
            target_start_observed_at=CASE WHEN ?=? THEN ? ELSE target_start_observed_at END,
            target_end_observed_at=CASE WHEN ?=? THEN ? ELSE target_end_observed_at END
            WHERE forecast_id=?""",
                     (status, activated, _now() if complete else None, int(counts["h"] or 0),
                      int(counts["l"] or 0), int(counts["e"] or 0), wins, losses, net,
                      bool(net > 0) if complete else None, position, expected_start,
                      row["round_id"], position, expected_end, row["round_id"],
                      position, expected_start, row.get("observed_at"),
                      position, expected_end, row.get("observed_at"), forecast["forecast_id"]))

    def _apply_baselines(self, conn, row, position):
        windows = conn.execute("SELECT * FROM adie_baseline_windows WHERE status IN ('WAITING','ACTIVE') AND start_position<=? AND end_position>=?",
                               (position, position)).fetchall()
        for window in windows:
            if position == int(window["start_position"]):
                try:
                    observed = datetime.fromisoformat(str(row.get("observed_at") or "").replace("Z", "+00:00"))
                    decided = datetime.fromisoformat(str(window["decision_at"]).replace("Z", "+00:00"))
                    if observed.tzinfo is None or decided.tzinfo is None or observed <= decided:
                        raise ValueError("target_not_proven_after_decision")
                except (TypeError, ValueError, OverflowError):
                    conn.execute("UPDATE adie_baseline_windows SET status='INCOMPLETE' WHERE baseline_id=?",
                                 (window["baseline_id"],))
                    continue
            won = row["outcome"] in ("HIGH", "EXACT") if window["direction"] == "HIGH" else row["outcome"] == "LOW"
            conn.execute("INSERT INTO adie_baseline_window_rounds(baseline_id,position,round_id,multiplier,outcome,accounted_win) VALUES(?,?,?,?,?,?) ON CONFLICT(baseline_id,position) DO NOTHING",
                         (window["baseline_id"], position, row["round_id"], row["multiplier"], row["outcome"], won))
            counts = conn.execute("SELECT COUNT(*) n,SUM(CASE WHEN outcome='HIGH' THEN 1 ELSE 0 END) h,SUM(CASE WHEN outcome='LOW' THEN 1 ELSE 0 END) l,SUM(CASE WHEN outcome='EXACT' THEN 1 ELSE 0 END) e,SUM(CASE WHEN accounted_win THEN 1 ELSE 0 END) w FROM adie_baseline_window_rounds WHERE baseline_id=?",
                                  (window["baseline_id"],)).fetchone()
            complete = int(counts["n"] or 0) == int(window["horizon"])
            wins = int(counts["w"] or 0)
            losses = int(counts["n"] or 0) - wins
            net = wins - losses
            conn.execute("""UPDATE adie_baseline_windows SET status=?,high_count=?,low_count=?,exact_count=?,
                wins=?,losses=?,net_units=?,positive_net=?,
                start_round_id=CASE WHEN ?=? THEN ? ELSE start_round_id END,
                end_round_id=CASE WHEN ?=? THEN ? ELSE end_round_id END WHERE baseline_id=?""",
                ("COMPLETED" if complete else "ACTIVE", int(counts["h"] or 0), int(counts["l"] or 0),
                 int(counts["e"] or 0), wins, losses, net, bool(net > 0) if complete else None,
                 position, int(window["start_position"]), row["round_id"], position,
                 int(window["end_position"]), row["round_id"], window["baseline_id"]))

    def _candidate_evidence(self, direction: str, horizon: int, offset: int,
                            context_summary: dict[str, Any] | None = None) -> dict[str, Any]:
        # No Phase 2 context cell survived multiplicity correction. Keep the
        # candidate visible and evaluate it prospectively, without inventing
        # pseudo-support from overlapping retrospective windows.
        horizon_context = ((context_summary or {}).get("horizons") or {}).get(str(horizon), {})
        estimate = horizon_context.get("high_net_per_round_mean" if direction == "HIGH" else "low_net_per_round_mean")
        supported = int(horizon_context.get("context_count") or 0)
        return {
            "matured_context_samples": int(horizon_context.get("matured_context_samples") or 0) if offset == 0 else 0,
            "comparable_context_count": supported if offset == 0 else 0,
            "estimated_net_per_staked_round": estimate if offset == 0 else None,
            "conservative_95_ci": None,
            "context_similarity": context_summary,
            "unconditional_reference_high_rate": PRIOR_HIGH_RATE,
            "unconditional_reference_low_rate": 1 - PRIOR_HIGH_RATE,
            "research_status": "NO_VALIDATED_EDGE",
            "qualification": "INSUFFICIENT_INDEPENDENT_CONTEXT_SUPPORT",
            "policy_version": POLICY_VERSION,
            "policy_config_sha256": POLICY_CONFIG_HASH,
            "direction": direction, "horizon": horizon, "start_offset": offset,
        }

    def _choose_normal(self, features, context_summary):
        candidates = [{
            "direction": direction,
            "horizons_evaluated": list(HORIZONS),
            "offsets_evaluated": [int(value) for value in POLICY_CONFIG.get("start_offsets", range(0, 11))],
            "candidate_count": len(HORIZONS) * len(POLICY_CONFIG.get("start_offsets", range(0, 11))),
            "matured_context_samples": int((context_summary or {}).get("neighbor_count") or 0),
            "qualifying_candidates": 0,
            "status": "INSUFFICIENT_INDEPENDENT_CONTEXT_SUPPORT",
        } for direction in ("HIGH", "LOW")]
        # Frozen v1 abstains: the corrected historical study has no validated
        # evidence; no dataset is allowed to auto-tune this rule.
        return None, candidates, "NO_CANDIDATE_HAS_VALIDATED_CONTEXT_SUPPORT"

    def process_observed(self, event: dict[str, Any], *, received_at: str | None = None,
                         recovery: bool = False) -> dict[str, Any]:
        """Consume an observed event; a selected forecast commits before return."""
        with self._lock:
            round_id = str(event.get("round_id") or "")
            if not round_id:
                raise ValueError("adie_round_identity_missing")
            multiplier = float(event["multiplier"])
            outcome = classify_multiplier(multiplier)
            continuity = str(event.get("continuity_status") or "UNVERIFIED").upper()
            delivery = str(event.get("delivery_mode") or "LIVE").upper()
            seen = received_at or event.get("api_received_at") or _now()
            fresh = False
            observed_at_dt = None
            try:
                observed = datetime.fromisoformat(str(event.get("observed_at") or "").replace("Z", "+00:00"))
                accepted = datetime.fromisoformat(str(event.get("api_received_at") or seen).replace("Z", "+00:00"))
                if observed.tzinfo is None or accepted.tzinfo is None:
                    raise ValueError("timezone_required")
                observed_at_dt = observed.astimezone(timezone.utc)
                age = (accepted.astimezone(timezone.utc) - observed.astimezone(timezone.utc)).total_seconds()
                fresh = -FUTURE_TOLERANCE_SECONDS <= age <= MAX_AGE_SECONDS
            except (TypeError, ValueError, OverflowError):
                fresh = False
            with self.repository.connect() as conn:
                prior = conn.execute("SELECT position FROM adie_round_events WHERE round_id=?", (round_id,)).fetchone()
                if prior:
                    if recovery:
                        conn.execute("UPDATE adie_round_events SET stream_id=COALESCE(?,stream_id),stream_sequence=COALESCE(?,stream_sequence),committed_at=COALESCE(?,?) WHERE round_id=?",
                                     (event.get("stream_id"), event.get("stream_sequence"),
                                      event.get("stored_at"), _now(), round_id))
                    return {"duplicate": True, "position": int(prior["position"])}
                state_row = conn.execute("SELECT state_json FROM adie_state WHERE singleton=1").fetchone()
                state = _parse(state_row["state_json"], {}) if state_row else {}
                seeded_ids = {str(item.get("round_id")) for item in state.get("seed_history", [])}
                if round_id in seeded_ids:
                    # Existing canonical outcomes predate ADIE activation. They
                    # warm the context but can never create retrospective forecasts.
                    return {"duplicate": True, "seeded_context": True,
                            "position": int(state.get("position") or 0)}
                source_index = event.get("round_index")
                try:
                    source_index = int(source_index) if source_index is not None else None
                except (TypeError, ValueError):
                    source_index = None
                previous_source_index = state.get("last_source_round_index")
                continuity_reason = None
                if continuity == "VERIFIED" and source_index is not None and previous_source_index is not None:
                    previous_source_index = int(previous_source_index)
                    if source_index <= previous_source_index:
                        continuity = "IDENTITY_ORDER_CONFLICT"
                        continuity_reason = "source_round_index_not_increasing"
                    elif source_index != previous_source_index + 1:
                        continuity = "GAP_DETECTED"
                        continuity_reason = "source_round_index_gap"
                previous_observed_at = state.get("last_observed_at")
                if continuity == "VERIFIED" and observed_at_dt is not None and previous_observed_at:
                    try:
                        previous_time = datetime.fromisoformat(str(previous_observed_at).replace("Z", "+00:00"))
                        if previous_time.tzinfo is None:
                            raise ValueError("timezone_required")
                        previous_time = previous_time.astimezone(timezone.utc)
                        if observed_at_dt == previous_time:
                            continuity = "TIMESTAMP_COLLISION"
                            continuity_reason = "observed_timestamp_collision"
                        elif observed_at_dt < previous_time:
                            continuity = "TIMESTAMP_INVERSION"
                            continuity_reason = "observed_timestamp_inversion"
                    except (TypeError, ValueError):
                        continuity = "TIMESTAMP_UNVERIFIED"
                        continuity_reason = "observed_timestamp_unverified"
                if continuity != "VERIFIED" and not continuity_reason:
                    continuity_reason = f"continuity_{continuity.lower()}"
                target_already_completed = bool(event.get("adie_target_already_completed"))
                eligible = (continuity == "VERIFIED" and delivery == "LIVE" and fresh
                            and not recovery and not target_already_completed)
                position = int(state.get("position") or 0) + 1
                history = (state.get("seed_history") or []) + self._recent(conn)
                history = history[-200:]
                # Decisions are made after this round is settled, so its
                # outcome is known context for the next prospective window.
                history.append({"outcome": outcome, "multiplier": multiplier,
                                "observed_at": event.get("observed_at")})
                features = self._features(history, multiplier)
                previous_context = state.get("last_context")
                current_context = features["context_state"]
                previous_strength = state.get("last_dominance_strength")
                current_share = features["rolling"]["20"]["high_share_nonexact"]
                current_strength = abs(float(current_share) - .5) * 2 if current_share is not None else 0.0
                run_length = int(state.get("context_run_length") or 0) + 1 if previous_context == current_context else 1
                if previous_context in {"HIGH_DOMINANCE", "LOW_DOMINANCE"} and current_context == "BALANCED_TRANSITION":
                    phase = "TERMINATION_TO_BALANCE"
                elif previous_context == "LOW_DOMINANCE" and current_context == "HIGH_DOMINANCE":
                    phase = "LOW_TO_HIGH_TRANSITION"
                elif previous_context == "HIGH_DOMINANCE" and current_context == "LOW_DOMINANCE":
                    phase = "HIGH_TO_LOW_TRANSITION"
                elif previous_context != current_context and current_context in {"HIGH_DOMINANCE", "LOW_DOMINANCE"}:
                    phase = "DOMINANCE_ONSET"
                elif current_context in {"HIGH_DOMINANCE", "LOW_DOMINANCE"}:
                    phase = "DOMINANCE_PERSISTENCE"
                else:
                    phase = "BALANCED_OR_TRANSITION"
                strength_change = (current_strength - float(previous_strength)
                                   if previous_strength is not None else None)
                strength_trend = ("STRENGTHENING" if strength_change is not None and strength_change > 0
                                  else "WEAKENING" if strength_change is not None and strength_change < 0
                                  else "STABLE_OR_INITIAL")
                features["previous_context_state"] = previous_context
                features["context_transition"] = (
                    f"{previous_context}->{current_context}"
                    if previous_context and previous_context != current_context else "PERSISTENT_OR_INITIAL")
                features["dominance_lifecycle"] = {
                    "phase": phase, "context_run_length": run_length,
                    "strength": current_strength,
                    "strength_change": strength_change, "strength_trend": strength_trend,
                }
                features["data_quality"] = {
                    "continuity_status": continuity, "delivery_mode": delivery,
                    "identity_source": event.get("identity_source"),
                    "freshness_verified": fresh, "source_round_index": source_index,
                }
                features["policy_config_sha256"] = POLICY_CONFIG_HASH
                context_summary = self._context_evidence(self._similar_contexts(features))
                features["historical_context_similarity"] = context_summary
                now = _now()
                fingerprint = {key: features[key] for key in (
                    "rolling", "last_outcome", "last_streak_length", "alternation_rate_50",
                    "log_multiplier_sd_50", "context_state", "interrupted_clusters_50")}
                event_row = {
                    "round_id": round_id, "position": position, "multiplier": multiplier,
                    "outcome": outcome, "observed_at": event.get("observed_at"),
                }
                conn.execute("""INSERT INTO adie_round_events(round_id,position,stream_id,stream_sequence,
                    multiplier,outcome,observed_at,received_at,verified_at,committed_at,delivery_mode,continuity_status,
                    identity_source,source_round_index,source_platform_index,fingerprint_json,event_json,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (round_id, position, event.get("stream_id"), event.get("stream_sequence"), multiplier,
                     outcome, event.get("observed_at"), seen, now if continuity == "VERIFIED" else None,
                     (event.get("stored_at") or now) if recovery else None, delivery, continuity,
                     event.get("identity_source"), event.get("round_index"), event.get("platform_round_index"),
                     _json(fingerprint), _json(event), now))

                # Explicit gaps terminate active windows without assigning
                # unobserved rounds or settling across a discontinuity.
                if continuity != "VERIFIED":
                    conn.execute("UPDATE adie_forecasts SET status='INCOMPLETE',invalidation_reason=? WHERE status IN ('LOCKED','WAITING','ACTIVE')",
                                 (continuity_reason,))
                    conn.execute("UPDATE adie_baseline_windows SET status='INCOMPLETE' WHERE status IN ('WAITING','ACTIVE')")
                elif delivery != "LIVE":
                    conn.execute("UPDATE adie_forecasts SET status='INCOMPLETE',invalidation_reason='recovered_round_order_ambiguous' WHERE status IN ('LOCKED','WAITING','ACTIVE')")
                    conn.execute("UPDATE adie_baseline_windows SET status='INCOMPLETE' WHERE status IN ('WAITING','ACTIVE')")
                else:
                    for active in self._active_forecasts(conn):
                        self._apply_target(conn, active, event_row, position)
                    self._apply_baselines(conn, event_row, position)

                # Only an uninterrupted sequence of eligible live events
                # counts toward coverage; gaps and stale/recovered deliveries
                # reset the timer and cannot manufacture activity.
                count = int(state.get("eligible_since_selection") or 0) + 1 if eligible else 0
                selected = None
                candidates = []
                abstention = (continuity_reason if continuity != "VERIFIED" else
                              "recovered_round_not_eligible" if delivery != "LIVE" else
                              "target_settled_before_adie_processing" if target_already_completed else
                              "recovery_processing_no_new_forecast" if recovery else
                              "stale_round_not_eligible" if not fresh else None)
                if eligible and self._active_forecasts(conn):
                    abstention = "ONE_PRIMARY_FORECAST_ALREADY_ACTIVE"
                elif eligible:
                    selected, candidates, abstention = self._choose_normal(features, context_summary)
                    forced = False
                    if selected is None and count >= FORCE_AFTER:
                        # Round 100 is deterministic exploratory coverage.
                        # Alternate forced directions to avoid selecting one
                        # side from its nominal unconditional rate.
                        forced_count = conn.execute("SELECT COUNT(*) n FROM adie_forecasts WHERE selection_status='FORCED_EXPLORATORY'").fetchone()
                        direction = "HIGH" if int(forced_count["n"] or 0) % 2 == 0 else "LOW"
                        selected = {
                            "direction": direction,
                            "horizon": int(POLICY_CONFIG.get("activity_coverage", {}).get("forced_horizon", 5)),
                            "offset": int(POLICY_CONFIG.get("activity_coverage", {}).get("forced_offset", 0)),
                            "evidence": self._candidate_evidence(direction, 5, 0, context_summary),
                        }
                        forced = True
                        abstention = "FORCED_EXPLORATORY_ACTIVITY_COVERAGE_100_ELIGIBLE_ROUNDS"
                    else:
                        forced = False
                    if selected:
                        fid = uuid.uuid5(uuid.NAMESPACE_URL, f"adie:{POLICY_VERSION}:{round_id}").hex
                        forecast = {
                            "forecast_id": fid, "direction": selected["direction"],
                            "horizon": selected["horizon"], "offset": selected["offset"],
                            "evidence": selected.get("evidence") or {},
                        }
                        start = position + 1 + int(forecast["offset"])
                        end = start + int(forecast["horizon"]) - 1
                        selection_status = "FORCED_EXPLORATORY" if forced else "EXPLORATORY"
                        reason = abstention or "NORMAL_CONTEXT_SUPPORTED_SELECTION"
                        status = "LOCKED" if forecast["offset"] == 0 else "WAITING"
                        conn.execute("""INSERT INTO adie_forecasts(forecast_id,decision_id,decision_round_id,
                            decision_position,direction,horizon,start_offset,target_start_position,target_end_position,
                            status,selection_status,policy_version,reason,evidence_json,created_at,committed_at)
                            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (fid, "", round_id, position, forecast["direction"], forecast["horizon"],
                             forecast["offset"], start, end, status, selection_status, POLICY_VERSION,
                             reason, _json(forecast["evidence"]), now, now))
                        selected = {**forecast, "selection_status": selection_status,
                                    "reason": reason, "target_start_position": start,
                                    "target_end_position": end, "status": status}
                        count = 0
                decision_id = uuid.uuid5(uuid.NAMESPACE_URL, f"adie-decision:{POLICY_VERSION}:{round_id}").hex
                if selected:
                    # forecast decision_id is filled before transaction commit.
                    conn.execute("UPDATE adie_forecasts SET decision_id=? WHERE forecast_id=?",
                                 (decision_id, selected["forecast_id"]))
                conn.execute("""INSERT INTO adie_decisions(decision_id,round_id,position,policy_version,
                    cohort,eligible,abstention_reason,context_state,eligible_since_selection,features_json,
                    candidates_json,selected_forecast_id,decision_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (decision_id, round_id, position, POLICY_VERSION, COHORT, eligible,
                     None if selected else abstention, features["context_state"], count,
                     _json(features), _json(candidates), selected["forecast_id"] if selected else None, now))
                if eligible:
                    for direction in ("HIGH", "LOW"):
                        for horizon in NORMAL_HORIZONS:
                            bid = uuid.uuid5(uuid.NAMESPACE_URL,
                                f"adie-baseline:{POLICY_VERSION}:{round_id}:{direction}:{horizon}").hex
                            conn.execute("""INSERT INTO adie_baseline_windows(baseline_id,decision_round_id,
                            decision_position,direction,horizon,start_position,end_position,decision_at,status)
                                VALUES(?,?,?,?,?,?,?,?,'WAITING') ON CONFLICT(decision_round_id,direction,horizon) DO NOTHING""",
                                (bid, round_id, position, direction, horizon, position + 1,
                                 position + horizon, now))
                state.update({"position": position, "eligible_since_selection": count,
                              "last_round_id": round_id, "last_received_at": seen,
                              "last_observed_at": event.get("observed_at") or state.get("last_observed_at"),
                              "last_source_round_index": (source_index if source_index is not None
                                                           else state.get("last_source_round_index")),
                              "last_continuity": continuity, "last_delivery_mode": delivery,
                              "health": "HEALTHY" if eligible else "DEGRADED",
                              "last_context": features["context_state"],
                              "last_lifecycle": features["dominance_lifecycle"],
                              "context_run_length": run_length,
                              "last_dominance_strength": current_strength,
                              "stream_id": event.get("stream_id") or state.get("stream_id"),
                              "stream_sequence": event.get("stream_sequence") or state.get("stream_sequence") or 0,
                              "last_decision_at": now})
                conn.execute("UPDATE adie_state SET state_json=?,updated_at=? WHERE singleton=1", (_json(state), now))
                self.last_error = None
                return {"duplicate": False, "position": position, "eligible": eligible,
                        "features": features, "selected": selected,
                        "activity": {"eligible_since_selection": count,
                                     "watch_active": eligible and count >= WATCH_AFTER,
                                     "forced_at": FORCE_AFTER}}

    def status(self) -> dict[str, Any]:
        with self.repository.connect() as conn:
            state_row = conn.execute("SELECT state_json,updated_at FROM adie_state WHERE singleton=1").fetchone()
            state = _parse(state_row["state_json"], {}) if state_row else {}
            latest = conn.execute("SELECT * FROM adie_round_events ORDER BY position DESC LIMIT 1").fetchone()
            active = conn.execute("SELECT * FROM adie_forecasts WHERE status IN ('LOCKED','WAITING','ACTIVE') ORDER BY committed_at DESC LIMIT 1").fetchone()
            decisions = conn.execute("SELECT COUNT(*) n FROM adie_decisions").fetchone()
            state["updated_at"] = state_row["updated_at"] if state_row else None
            state["latest_round"] = dict(latest) if latest else None
            if not latest and state.get("seed_latest_round"):
                state["latest_round"] = {**state["seed_latest_round"],
                                          "outcome": state["seed_latest_round"].get("outcome"),
                                          "fingerprint": state.get("seed_fingerprint"),
                                          "seeded_context": True}
            if state.get("latest_round"):
                state["latest_round"]["fingerprint"] = state["latest_round"].get("fingerprint") or _parse(state["latest_round"].pop("fingerprint_json", None), {})
                state["latest_round"].pop("event_json", None)
            state["active_forecast"] = self._forecast_dict(active) if active else None
            state["decision_count"] = int(decisions["n"] or 0)
            state["policy_version"] = POLICY_VERSION
            state["policy_config_sha256"] = POLICY_CONFIG_HASH
            state["policy_configuration"] = POLICY_CONFIG
            state["cohort"] = COHORT
            state["research_reference"] = RESEARCH
            state["reference_manifest"] = self.reference_manifest
            state["last_error"] = self.last_error
            if latest and latest["received_at"]:
                try:
                    stamp = datetime.fromisoformat(str(latest["received_at"]).replace("Z", "+00:00"))
                    if stamp.tzinfo is None: stamp = stamp.replace(tzinfo=timezone.utc)
                    state["freshness_seconds"] = max(0.0, (datetime.now(timezone.utc)-stamp).total_seconds())
                except ValueError:
                    state["freshness_seconds"] = None
            if state.get("freshness_seconds") is not None and state["freshness_seconds"] > MAX_AGE_SECONDS:
                state["health"] = "STALE"
                state["health_reason"] = f"No fresh verified round observation within {MAX_AGE_SECONDS:g} seconds"
            elif state.get("freshness_seconds") is not None:
                state["health_reason"] = None
            return state

    @staticmethod
    def _forecast_dict(row):
        if not row: return None
        out = dict(row)
        out["evidence"] = _parse(out.pop("evidence_json", None), {})
        return out

    def forecasts(self, *, limit: int = 100, status: str | None = None):
        limit = min(max(int(limit), 1), 500)
        with self.repository.connect() as conn:
            if status:
                rows = conn.execute("SELECT * FROM adie_forecasts WHERE status=? ORDER BY decision_position DESC LIMIT ?", (status.upper(), limit)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM adie_forecasts ORDER BY decision_position DESC LIMIT ?", (limit,)).fetchall()
        return [self._forecast_dict(row) for row in rows]

    def performance(self) -> dict[str, Any]:
        with self.repository.connect() as conn:
            rows = conn.execute("SELECT selection_status,direction,horizon,status,COUNT(*) n,SUM(wins) wins,SUM(losses) losses,SUM(net_units) net,SUM(CASE WHEN positive_net THEN 1 ELSE 0 END) positive FROM adie_forecasts GROUP BY selection_status,direction,horizon,status ORDER BY selection_status,direction,horizon,status").fetchall()
            totals = conn.execute("""SELECT COUNT(*) total,
                SUM(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END) completed,
                SUM(CASE WHEN status='COMPLETED' THEN wins ELSE 0 END) wins,
                SUM(CASE WHEN status='COMPLETED' THEN losses ELSE 0 END) losses,
                SUM(CASE WHEN status='COMPLETED' THEN net_units ELSE 0 END) net,
                SUM(CASE WHEN status='COMPLETED' AND positive_net THEN 1 ELSE 0 END) positive,
                SUM(CASE WHEN status<>'COMPLETED' THEN wins ELSE 0 END) open_wins,
                SUM(CASE WHEN status<>'COMPLETED' THEN losses ELSE 0 END) open_losses,
                SUM(CASE WHEN status<>'COMPLETED' THEN net_units ELSE 0 END) open_net
                FROM adie_forecasts""").fetchone()
            baseline_groups = conn.execute("""SELECT direction,horizon,COUNT(*) windows,
                SUM(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END) completed,
                SUM(CASE WHEN status='COMPLETED' THEN wins ELSE 0 END) wins,
                SUM(CASE WHEN status='COMPLETED' THEN losses ELSE 0 END) losses,
                SUM(CASE WHEN status='COMPLETED' THEN net_units ELSE 0 END) net,
                SUM(CASE WHEN status='COMPLETED' AND positive_net THEN 1 ELSE 0 END) positive
                FROM adie_baseline_windows GROUP BY direction,horizon ORDER BY direction,horizon""").fetchall()
            baseline_totals = conn.execute("""SELECT COUNT(*) windows,
                SUM(CASE WHEN status='COMPLETED' THEN 1 ELSE 0 END) completed,
                SUM(CASE WHEN status='INCOMPLETE' THEN 1 ELSE 0 END) incomplete
                FROM adie_baseline_windows""").fetchone()
            timing_rows = conn.execute("SELECT committed_at,target_start_observed_at,target_end_observed_at FROM adie_forecasts WHERE status='COMPLETED' AND target_start_observed_at IS NOT NULL").fetchall()
        lead_seconds = []
        duration_seconds = []
        for row in timing_rows:
            try:
                committed = datetime.fromisoformat(str(row["committed_at"]).replace("Z", "+00:00"))
                start = datetime.fromisoformat(str(row["target_start_observed_at"]).replace("Z", "+00:00"))
                if committed.tzinfo and start.tzinfo:
                    lead_seconds.append((start - committed).total_seconds())
                if row["target_end_observed_at"]:
                    end = datetime.fromisoformat(str(row["target_end_observed_at"]).replace("Z", "+00:00"))
                    if start.tzinfo and end.tzinfo:
                        duration_seconds.append((end - start).total_seconds())
            except (TypeError, ValueError):
                continue
        timing = {"completed_with_timing": len(lead_seconds),
                  "mean_commit_to_target_start_seconds": round(sum(lead_seconds) / len(lead_seconds), 3) if lead_seconds else None,
                  "mean_target_window_elapsed_seconds": round(sum(duration_seconds) / len(duration_seconds), 3) if duration_seconds else None,
                  "early_late_definition": "window starts only when its first eligible target is observed after durable forecast commit; otherwise marked incomplete"}
        return {"accounting": "SIMULATED_SHADOW_FIXED_STAKE_2X_NOT_REALIZED_PROFIT",
                "settlement": "HIGH wins at >=2x including EXACT; LOW wins <2x; +1/-1 net units",
                "totals": dict(totals), "groups": [dict(r) for r in rows],
                "fixed_horizon_baselines": {"overlap_warning": "windows overlap; window rows are descriptive and not independent trials",
                    "totals": dict(baseline_totals), "groups": [dict(r) for r in baseline_groups]},
                "timing": timing,
                "baseline": {"prior_high_rate": PRIOR_HIGH_RATE,
                             "prior_low_rate": 1-PRIOR_HIGH_RATE,
                             "prior_expected_net_per_round_high": 2*PRIOR_HIGH_RATE-1,
                             "prior_expected_net_per_round_low": 2*(1-PRIOR_HIGH_RATE)-1}}

    def decisions(self, *, limit: int = 100):
        limit = min(max(int(limit), 1), 500)
        with self.repository.connect() as conn:
            rows = conn.execute("SELECT * FROM adie_decisions ORDER BY position DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["features"] = _parse(item.pop("features_json", None), {})
            item["candidates"] = _parse(item.pop("candidates_json", None), [])
            out.append(item)
        return out

