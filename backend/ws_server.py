"""
ws_server.py — Real-time synchronization pipeline.

Single source of truth flow (triggered on every new round):
  1. round arrives  → emit  "round:new"
  2. predict next   → emit  "prediction:new"   (for round N+1)
  3. backfill prev  → emit  "prediction:resolved"  (WIN/LOSS for round N-1)
  4. emit           "sync:state"  (full dashboard snapshot, incremental)

Socket.IO events:
  SERVER → CLIENT:
    round:new            { round_id, multiplier, category, timestamp, total_rounds }
    prediction:new       { prediction_id, for_round, prediction, confidence, ... }
    prediction:resolved  { prediction_id, for_round, result, actual_multiplier, ... }
    sync:state           { current_round, next_prediction, stats, collector }
    system:status        { collector, browser, frame, logged_in, uptime, ... }

  CLIENT → SERVER:
    sync:request         → server replies with full sync:state immediately
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

log = logging.getLogger("ws-server")

_sio = None
_sync_state: Optional["SyncState"] = None


def _get_sio():
    if _sio is None:
        raise RuntimeError("SocketIO not initialized")
    return _sio


# ── SyncState — in-memory single source of truth ─────────────────────────────

class SyncState:
    """
    Holds the authoritative live state for the dashboard.
    Thread-safe. Updated atomically on every new round.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._state: Dict[str, Any] = {
            "current_round":     None,   # latest round object
            "next_prediction":   None,   # prediction for current_round + 1
            "pending_for_round": None,   # round_id the pending prediction is for
            "stats": {
                "total_rounds":    0,
                "wins":            0,
                "losses":          0,
                "accuracy_pct":    None,
                "current_streak":  0,
                "streak_type":     None,   # "WIN" | "LOSS"
                "win_streak_best": 0,
                "loss_streak_best":0,
                "total_predictions": 0,
                "avg_confidence":  None,
                "avg_multiplier":  None,
                "last_round_time": None,
                "prediction_latency_ms": None,
            },
            "collector": {
                "running":   False,
                "browser":   False,
                "frame":     False,
                "logged_in": False,
                "uptime":    0,
                "last_recovery": None,
                "recovery_count": 0,
            },
            "timeline": [],   # last 20 rounds with WIN/LOSS/WAITING
        }
        self._last_round_id: Optional[int] = None

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            import copy
            return copy.deepcopy(self._state)

    def update_collector_status(self, status: Dict[str, Any]) -> None:
        with self._lock:
            c = self._state["collector"]
            c["running"]        = status.get("collectorRunning", False)
            c["browser"]        = status.get("browserConnected", False)
            c["frame"]          = status.get("frameConnected", False)
            c["logged_in"]      = status.get("loggedIn", False)
            c["uptime"]         = status.get("uptime", 0)
            c["last_recovery"]  = status.get("lastRecovery")
            c["recovery_count"] = status.get("recoveryCount", 0)

    def on_new_round(self, round_obj: Dict[str, Any]) -> bool:
        """
        Register a new round. Returns True if this is genuinely new.
        """
        rid = round_obj.get("round_id")
        with self._lock:
            if rid is not None and rid == self._last_round_id:
                return False
            self._last_round_id = rid
            self._state["current_round"] = round_obj
            self._state["stats"]["total_rounds"] = round_obj.get("total_rounds",
                self._state["stats"]["total_rounds"] + 1)
            self._state["stats"]["last_round_time"] = round_obj.get("timestamp")
            return True

    def set_next_prediction(self, pred: Dict[str, Any], for_round: int,
                             latency_ms: Optional[float] = None) -> None:
        with self._lock:
            self._state["next_prediction"]   = pred
            self._state["pending_for_round"] = for_round
            if latency_ms is not None:
                self._state["stats"]["prediction_latency_ms"] = round(latency_ms)
            # Update avg confidence
            conf = pred.get("confidence")
            if conf is not None:
                prev = self._state["stats"]["avg_confidence"]
                n    = self._state["stats"]["total_predictions"] + 1
                self._state["stats"]["avg_confidence"] = (
                    round(conf if prev is None else (prev * (n - 1) + conf) / n, 1)
                )
                self._state["stats"]["total_predictions"] = n

    def resolve_prediction(self, prediction_id: str, for_round: int,
                            actual_multiplier: float, actual_category: str,
                            predicted_category: str) -> Dict[str, Any]:
        """
        Mark a prediction as WIN or LOSS. Updates streak and accuracy.
        Returns the resolution object.
        """
        result = "WIN" if actual_category == predicted_category else "LOSS"
        with self._lock:
            s = self._state["stats"]
            if result == "WIN":
                s["wins"] += 1
                if s["streak_type"] == "WIN":
                    s["current_streak"] += 1
                else:
                    s["streak_type"]    = "WIN"
                    s["current_streak"] = 1
                s["win_streak_best"] = max(s["win_streak_best"], s["current_streak"])
            else:
                s["losses"] += 1
                if s["streak_type"] == "LOSS":
                    s["current_streak"] += 1
                else:
                    s["streak_type"]    = "LOSS"
                    s["current_streak"] = 1
                s["loss_streak_best"] = max(s["loss_streak_best"], s["current_streak"])

            total = s["wins"] + s["losses"]
            s["accuracy_pct"] = round(s["wins"] / total * 100, 1) if total else None

            # Update timeline
            tl = self._state["timeline"]
            # Find and update the matching timeline entry
            for entry in tl:
                if entry.get("round_id") == for_round:
                    entry["result"] = result
                    entry["actual_multiplier"] = actual_multiplier
                    entry["actual_category"]   = actual_category
                    break

        return {
            "prediction_id":    prediction_id,
            "for_round":        for_round,
            "result":           result,
            "actual_multiplier":actual_multiplier,
            "actual_category":  actual_category,
            "predicted_category":predicted_category,
        }

    def add_timeline_entry(self, round_id: int, multiplier: float,
                            category: str, timestamp: str,
                            prediction: Optional[str] = None,
                            confidence: Optional[float] = None) -> None:
        with self._lock:
            tl = self._state["timeline"]
            # Avoid duplicates
            if any(e.get("round_id") == round_id for e in tl):
                return
            tl.append({
                "round_id":   round_id,
                "multiplier": multiplier,
                "category":   category,
                "timestamp":  timestamp,
                "prediction": prediction,
                "confidence": confidence,
                "result":     "WAITING" if prediction else None,
            })
            # Keep last 50
            if len(tl) > 50:
                self._state["timeline"] = tl[-50:]


def get_sync_state() -> SyncState:
    global _sync_state
    if _sync_state is None:
        _sync_state = SyncState()
    return _sync_state


# ── Main pipeline ─────────────────────────────────────────────────────────────

def emit_round(round_data: Dict[str, Any]) -> None:
    """
    Called by the collector (or file watcher) when a new round is detected.
    Runs the full pipeline atomically:
      1. Register round in SyncState
      2. Emit round:new
      3. Generate prediction for NEXT round
      4. Emit prediction:new
      5. Backfill previous prediction (WIN/LOSS)
      6. Emit prediction:resolved if resolved
      7. Emit sync:state (full snapshot)
    """
    try:
        sio   = _get_sio()
        state = get_sync_state()

        # ── 1. Register round ─────────────────────────────────────────────
        from utils import load_round_history, utc_now, multiplier_to_category
        rounds = load_round_history()
        total  = len(rounds)

        # Enrich round_data with category and total
        mult     = round_data.get("multiplier", 0)
        category = multiplier_to_category(mult)
        rid      = round_data.get("round_id") or (rounds[-1]["round_id"] if rounds else total)

        enriched_round = {
            **round_data,
            "round_id":    rid,
            "category":    category,
            "total_rounds":total,
        }

        is_new = state.on_new_round(enriched_round)
        if not is_new:
            log.debug("emit_round: duplicate round_id=%s — skipped", rid)
            return

        # ── 2. Emit round:new ─────────────────────────────────────────────
        sio.emit("round:new", enriched_round)
        log.info("round:new  id=%s  mult=%.2f  cat=%s  total=%d", rid, mult, category, total)

        # ── 3. Add to timeline ────────────────────────────────────────────
        state.add_timeline_entry(
            round_id=rid, multiplier=mult, category=category,
            timestamp=round_data.get("timestamp", utc_now()),
        )

        # ── 4. Generate prediction for NEXT round ─────────────────────────
        if len(rounds) >= 20:
            _predict_next(sio, state, rounds, rid)

        # ── 5. Backfill previous prediction ──────────────────────────────
        _backfill_and_resolve(sio, state)

        # ── 6. Emit full sync:state ───────────────────────────────────────
        sio.emit("sync:state", state.snapshot())

    except Exception as exc:
        log.error("emit_round pipeline error: %s", exc, exc_info=True)


def _predict_next(sio, state: SyncState, rounds: list, current_round_id: int) -> None:
    """Generate prediction for round current_round_id + 1."""
    try:
        from prediction.predictor import get_predictor
        from prediction.risk_management import full_guidance
        from utils import utc_now, append_decision, read_json, DECISIONS_PATH, MIN_CONFIDENCE_TO_STORE

        t0          = time.monotonic()
        multipliers = [r["multiplier"] for r in rounds]
        result      = full_guidance(get_predictor().predict(multipliers))
        latency_ms  = (time.monotonic() - t0) * 1000

        for_round = current_round_id + 1
        import uuid
        prediction_id = str(uuid.uuid4())[:8]

        decision = {
            **result,
            "prediction_id":      prediction_id,
            "for_round":          for_round,          # ← prediction is FOR next round
            "last_round_id":      current_round_id,   # ← based on this round
            "created_at":         utc_now(),
            "source_round_count": len(multipliers),
            "last_multiplier":    multipliers[-1],
            "last_round_ts":      rounds[-1].get("timestamp"),
            "prediction_latency_ms": round(latency_ms),
        }

        # Store (deduplicated by for_round)
        if result["confidence"] >= MIN_CONFIDENCE_TO_STORE:
            existing = read_json(DECISIONS_PATH, [])
            if not isinstance(existing, list):
                existing = []
            already = any(d.get("for_round") == for_round for d in existing[-5:])
            if not already:
                append_decision(decision)

        state.set_next_prediction(decision, for_round, latency_ms)

        # Update timeline entry for the NEXT round (prediction pending)
        state.add_timeline_entry(
            round_id=for_round, multiplier=0, category="UNKNOWN",
            timestamp=utc_now(),
            prediction=result["prediction"],
            confidence=result["confidence"],
        )

        sio.emit("prediction:new", decision)
        log.info("prediction:new  for_round=%d  pred=%s  conf=%.1f  latency=%.0fms",
                 for_round, result["prediction"], result["confidence"], latency_ms)

    except Exception as exc:
        log.error("_predict_next error: %s", exc)


def _backfill_and_resolve(sio, state: SyncState) -> None:
    """
    Backfill actual results for pending predictions.
    Emits prediction:resolved for each newly resolved prediction.
    """
    try:
        from trainer import TrainingService
        from utils import read_json, DECISIONS_PATH

        _trainer = TrainingService()
        updated = _trainer.backfill_actual_results()
        if updated <= 0:
            return

        decisions = read_json(DECISIONS_PATH, [])
        if not isinstance(decisions, list):
            return

        # Find newly resolved decisions (have actual_multiplier, correct field)
        recently_resolved = [
            d for d in decisions[-20:]
            if d.get("actual_multiplier") is not None
            and d.get("correct") is not None
            and d.get("prediction_id")
        ]

        for d in recently_resolved:
            from utils import multiplier_to_category
            actual_cat = d.get("actual_category") or multiplier_to_category(d["actual_multiplier"])
            resolution = state.resolve_prediction(
                prediction_id=d["prediction_id"],
                for_round=d.get("for_round") or d.get("actual_round_id", 0),
                actual_multiplier=d["actual_multiplier"],
                actual_category=actual_cat,
                predicted_category=d["prediction"],
            )
            sio.emit("prediction:resolved", {
                **resolution,
                "recommended_cashout": d.get("recommended_cashout"),
                "confidence":          d.get("confidence"),
            })
            log.info("prediction:resolved  for_round=%s  result=%s  actual=%.2f",
                     resolution["for_round"], resolution["result"], d["actual_multiplier"])

        # Emit updated decisions list
        sio.emit("decisions_updated", {"decisions": decisions[-100:], "updated": updated})

    except Exception as exc:
        log.debug("_backfill_and_resolve: %s", exc)


def emit_decisions_updated() -> None:
    """Legacy helper — kept for compatibility."""
    try:
        from utils import read_json, DECISIONS_PATH
        decisions = read_json(DECISIONS_PATH, [])
        if isinstance(decisions, list):
            _get_sio().emit("decisions_updated", {"decisions": decisions[-100:]})
    except Exception as exc:
        log.debug("emit_decisions_updated: %s", exc)


def emit_system_status(status: Dict[str, Any]) -> None:
    """Called by the collector health monitor to push live status."""
    try:
        state = get_sync_state()
        state.update_collector_status(status)
        _get_sio().emit("system:status", {
            **status,
            "ts": time.time(),
        })
    except Exception as exc:
        log.debug("emit_system_status: %s", exc)


def register_sync_handlers(sio_instance) -> None:
    """Register client→server event handlers."""
    @sio_instance.on("sync:request")
    def on_sync_request(sid, data=None):
        try:
            snap = get_sync_state().snapshot()
            sio_instance.emit("sync:state", snap, to=sid)
        except Exception as exc:
            log.debug("sync:request handler: %s", exc)
