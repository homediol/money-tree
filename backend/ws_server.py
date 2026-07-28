"""
ws_server.py
============
Flask-SocketIO WebSocket server.

Events pushed to clients:
  "round_complete"  — emitted after every completed round (from history)
  "prediction"      — fresh prediction + stored decision
  "decisions_updated" — backfill complete, Actual column updated
"""

from __future__ import annotations

import logging
from typing import Any, Dict

log = logging.getLogger("ws-server")

_sio = None


def _get_sio():
    if _sio is None:
        raise RuntimeError("SocketIO not initialized — start the server via run.py")
    return _sio


def emit_round(round_data: Dict[str, Any]) -> None:
    """
    Called after a new round is detected in history.
    Emits round_complete, runs prediction, stores decision, runs backfill.
    """
    try:
        sio = _get_sio()
        sio.emit("round_complete", round_data)

        from utils import load_round_history, utc_now, append_decision, read_json, DECISIONS_PATH, MIN_CONFIDENCE_TO_STORE
        from prediction.predictor import get_predictor
        from prediction.risk_management import full_guidance

        rounds = load_round_history()
        multipliers = [r["multiplier"] for r in rounds] if len(rounds) >= 20 else []

        if len(multipliers) < 20:
            return

        result = full_guidance(get_predictor().predict(multipliers))
        current_round_id = rounds[-1]["round_id"]
        decision = {
            **result,
            "created_at":         utc_now(),
            "source_round_count": len(multipliers),
            "last_round_id":      current_round_id,
            "last_multiplier":    multipliers[-1],
            "last_round_ts":      rounds[-1].get("timestamp"),
        }

        if result["confidence"] >= MIN_CONFIDENCE_TO_STORE:
            existing = read_json(DECISIONS_PATH, [])
            if not isinstance(existing, list):
                existing = []
            already = any(d.get("last_round_id") == current_round_id for d in existing[-5:])
            if not already:
                append_decision(decision)

        sio.emit("prediction", decision)

        try:
            from app import trainer
            updated = trainer.backfill_actual_results()
            if updated > 0:
                updated_decisions = read_json(DECISIONS_PATH, [])
                if isinstance(updated_decisions, list):
                    sio.emit("decisions_updated", {
                        "decisions": updated_decisions[-100:],
                        "updated":   updated,
                    })
        except Exception as exc:
            log.debug("Real-time backfill: %s", exc)

    except Exception as exc:
        log.error("emit_round error: %s", exc)


def emit_decisions_updated() -> None:
    try:
        from utils import read_json, DECISIONS_PATH
        decisions = read_json(DECISIONS_PATH, [])
        if isinstance(decisions, list):
            _get_sio().emit("decisions_updated", {"decisions": decisions[-100:]})
    except Exception as exc:
        log.debug("emit_decisions_updated error: %s", exc)
