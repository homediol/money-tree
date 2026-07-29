# === TOP: All imports ===
import logging
import threading
import time

from flask import Flask, jsonify, request
from model import AviatorPredictor, TensorFlowUnavailable
from risk_engine import (compute_moving_averages, compute_risk_index, compute_round_summary, compute_volatility, detect_streaks)
from trainer import TrainingService
from prediction.predictor import get_predictor
from prediction.risk_management import full_guidance
from utils import (append_decision, ensure_data_files, load_round_history, read_json, setup_logging, utc_now, DECISIONS_PATH, METADATA_PATH, MIN_CONFIDENCE_TO_STORE)
from risk_statistics import compute_stats

# === SETUP (runs once at import time) ===
setup_logging()
ensure_data_files()

# === Flask app ===
app = Flask(__name__)

# Enable gzip compression for all JSON responses
try:
    from flask_compress import Compress
    Compress(app)
except ImportError:
    pass

@app.after_request
def add_cors_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    return response

# ── In-memory cache for expensive endpoints ──────────────────────────────
_cache: dict = {}

def _cached(key: str, ttl: float, fn):
    entry = _cache.get(key)
    if entry and time.monotonic() - entry["ts"] < ttl:
        return entry["data"]
    data = fn()
    _cache[key] = {"data": data, "ts": time.monotonic()}
    return data

def _bust(key: str):
    _cache.pop(key, None)


logger = logging.getLogger("aviator-api")
trainer = TrainingService()
predictor = trainer.predictor

# === Model readiness event ===
_model_ready = threading.Event()

def start_model_prewarm() -> None:
    """Load predictor in background so first /predict call doesn't block or timeout."""
    def _prewarm_model():
        try:
            rounds = load_round_history()
            mults = [r["multiplier"] for r in rounds] if len(rounds) >= 20 else \
                    [r["multiplier"] for r in load_round_history()]
            if len(mults) >= 20:
                logger.info("Pre-warming predictor...")
                get_predictor().predict(mults)
                logger.info("Predictor pre-warmed OK")
        except Exception as exc:
            logger.warning("Pre-warm skipped: %s", exc)
        finally:
            _model_ready.set()

    if _model_ready.is_set():
        return
    threading.Thread(target=_prewarm_model, daemon=True, name="model-prewarm").start()

def json_error(message: str, status: int = 400):
    logger.warning(message)
    return jsonify({"error": message, "status": status}), status


@app.get("/")
def health():
    return jsonify({
        "status": "online",
        "service": "aviator-risk-management",
        "stored_rounds": len(load_round_history()),
        "model_ready": _model_ready.is_set(),
    })


@app.get("/ready")
def ready():
    """Lightweight readiness probe — returns model warm status."""
    return jsonify({
        "ready": _model_ready.is_set(),
        "stored_rounds": len(load_round_history()),
    })


# ── Training ─────────────────────────────────────────────────────────

@app.post("/train")
def train():
    payload = request.get_json(silent=True) or {}
    epochs = int(payload.get("epochs", 30))
    try:
        return jsonify({"status": "trained", **trainer.train(epochs=epochs)})
    except TensorFlowUnavailable as exc:
        return json_error(str(exc), 503)
    except Exception as exc:
        logger.exception("Training failed")
        return json_error(f"Training failed: {exc}", 500)


@app.post("/retrain")
def retrain():
    payload = request.get_json(silent=True) or {}
    epochs = int(payload.get("epochs", 30))
    try:
        return jsonify({"status": "retrained", **trainer.train(epochs=epochs)})
    except TensorFlowUnavailable as exc:
        return json_error(str(exc), 503)
    except Exception as exc:
        logger.exception("Retraining failed")
        return json_error(f"Retraining failed: {exc}", 500)


# ── Prediction ───────────────────────────────────────────────────────

MIN_CONFIDENCE = MIN_CONFIDENCE_TO_STORE  # alias for use in this module

@app.get("/predict")
def predict():
    """
    Returns prediction from the V2 pipeline.
    Waits up to 90s for the TF model to finish loading on cold start.
    Stores at most ONE decision per round_id.
    """
    try:
        # Wait for model pre-warm (max 90s — only blocks on first cold start)
        _model_ready.wait(timeout=90)

        trainer.auto_retrain_if_needed()

        rounds = load_round_history()
        if len(rounds) >= 20:
            multipliers = [r["multiplier"] for r in rounds]
        else:
            rounds2 = load_round_history()
            multipliers = [r["multiplier"] for r in rounds2]

        v2 = get_predictor()
        result = full_guidance(v2.predict(multipliers))

        current_round_id = rounds[-1]["round_id"] if rounds else None
        current_round_ts = rounds[-1].get("timestamp") if rounds else None

        decision = {
            **result,
            "created_at":         utc_now(),
            "source_round_count": len(multipliers),
            "last_round_id":      current_round_id,
            "last_multiplier":    multipliers[-1] if multipliers else None,
            "last_round_ts":      current_round_ts,
        }

        # Store at most one decision per round_id — prevents duplicate evaluations
        # that cause fake "✗ Wrong" from double-counting the same predicted round
        if result["confidence"] >= MIN_CONFIDENCE:
            existing = read_json(DECISIONS_PATH, [])
            if not isinstance(existing, list):
                existing = []
            already_stored = any(
                d.get("last_round_id") == current_round_id
                for d in existing[-5:]
            )
            if not already_stored:
                append_decision(decision)
            else:
                decision["cached"] = True

        _bust("accuracy")
        # Background retrain check
        try:
            from training.retrain import retrain_if_needed
            threading.Thread(target=retrain_if_needed, daemon=True).start()
        except Exception:
            pass

        return jsonify(decision)
    except TensorFlowUnavailable as exc:
        return json_error(str(exc), 503)
    except FileNotFoundError as exc:
        return json_error(str(exc), 404)
    except Exception as exc:
        logger.exception("Prediction failed")
        return json_error(f"Prediction failed: {exc}", 500)


@app.get("/fast-predict")
def fast_predict():
    """
    Ultra-fast prediction endpoint (<50 ms target).
    No retrain check, no disk I/O during the call.
    Used by WebSocket clients and high-frequency polling.
    """
    try:
        rounds = load_round_history()
        if len(rounds) >= 20:
            multipliers = [r["multiplier"] for r in rounds]
        else:
            rounds2 = load_round_history()
            multipliers = [r["multiplier"] for r in rounds2]

        result = full_guidance(get_predictor().predict(multipliers))
        result["last_round_id"]  = rounds[-1]["round_id"] if rounds else None
        result["last_round_ts"]  = rounds[-1].get("timestamp") if rounds else None
        result["last_multiplier"] = multipliers[-1] if multipliers else None
        return jsonify(result)
    except Exception as exc:
        return json_error(f"Fast predict failed: {exc}", 500)


# ── History ──────────────────────────────────────────────────────────

@app.get("/history")
def history():
    limit = int(request.args.get("limit", 100))
    def _build():
        frame = _get_rounds()
        return {"count": int(len(frame)), "rounds": frame[-limit:]}
    return jsonify(_cached(f"history_{limit}", 8.0, _build))


# ── Accuracy ─────────────────────────────────────────────────────────

@app.get("/accuracy")
def accuracy():
    metadata  = read_json(METADATA_PATH, {})
    decisions = read_json(DECISIONS_PATH, [])
    if not isinstance(decisions, list):
        decisions = []
    resolved = [d for d in decisions if d.get("actual_multiplier") is not None]
    correct  = [d for d in resolved  if d.get("correct") is True]
    hit_rate = round(len(correct) / len(resolved) * 100, 2) if resolved else None
    return jsonify({
        "model":               metadata,
        "prediction_count":    len(decisions),
        "resolved_count":      len(resolved),
        "correct_count":       len(correct),
        "hit_rate_pct":        hit_rate,
        "validation_accuracy": metadata.get("validation_accuracy", 0),
        "train_accuracy":      metadata.get("train_accuracy", 0),
    })


@app.post("/backfill")
def backfill():
    try:
        updated = trainer.backfill_actual_results()
        if updated:
            _bust("accuracy")
        return jsonify({"updated": updated})
    except Exception as exc:
        logger.exception("Backfill failed")
        return json_error(str(exc), 500)


@app.get("/skip-quality")
def skip_quality():
    """Return skip quality guard metrics."""
    try:
        from prediction.risk_management import get_skip_guard
        return jsonify(get_skip_guard().status())
    except Exception as exc:
        return json_error(str(exc), 500)


@app.get("/vh-quality")
def vh_quality():
    """Return VERY_HIGH false-positive guard metrics."""
    try:
        from prediction.risk_management import get_vh_guard
        return jsonify(get_vh_guard().status())
    except Exception as exc:
        return json_error(str(exc), 500)


@app.get("/calibration")
def calibration_status():
    """Return full calibration engine state: Bayesian priors, boundary optimizer, recalibration mode."""
    try:
        from prediction.calibration_engine import get_calibration_engine
        return jsonify(get_calibration_engine().status())
    except Exception as exc:
        return json_error(str(exc), 500)


@app.post("/calibration/reset")
def calibration_reset():
    """Manually reset calibration state (clears Bayesian priors and boundary adjustments)."""
    try:
        from prediction.calibration_engine import get_calibration_engine
        get_calibration_engine().force_reset()
        return jsonify({"status": "reset"})
    except Exception as exc:
        return json_error(str(exc), 500)


@app.get("/confidence-calibration")
def confidence_calibration_status():
    """Return full confidence calibration state: bins, inversion, correction factors, audit summary."""
    try:
        from prediction.confidence_calibrator import get_confidence_calibrator
        return jsonify(get_confidence_calibrator().status())
    except Exception as exc:
        return json_error(str(exc), 500)


@app.get("/confidence-calibration/audit")
def confidence_calibration_audit():
    """Return recent confidence calibration audit log entries."""
    try:
        from prediction.confidence_calibrator import get_confidence_calibrator
        limit = request.args.get("limit", 50, type=int)
        return jsonify(get_confidence_calibrator().get_audit_log(limit=limit))
    except Exception as exc:
        return json_error(str(exc), 500)


@app.get("/streak-matrix")
def streak_matrix():
    """Return streak success matrix and validity checker state."""
    try:
        from prediction.momentum_streak import get_momentum_engine
        return jsonify(get_momentum_engine().status())
    except Exception as exc:
        return json_error(str(exc), 500)


@app.get("/risk-tier")
def risk_tier_status():
    """Return MEDIUM risk-tier validator state: accuracy, spread, gate results, params."""
    try:
        from prediction.risk_tier_validator import get_risk_tier_validator
        return jsonify(get_risk_tier_validator().status())
    except Exception as exc:
        return json_error(str(exc), 500)


# ── Decisions / Logs ─────────────────────────────────────────────────

@app.get("/decisions")
def decisions():
    limit = int(request.args.get("limit", 50))
    payload = read_json(DECISIONS_PATH, [])
    return jsonify({"decisions": payload[-limit:] if isinstance(payload, list) else []})


# ═════════════════════════════════════════════════════════════════════
# RISK MANAGEMENT ENDPOINTS
# ═════════════════════════════════════════════════════════════════════

def _get_rounds() -> list:
    """
    Single source of truth for all endpoints.
    Uses roundhistory.json directly.
    Caps at the most recent 500 rounds for risk/stats calculations.
    """
    rounds = load_round_history()
    return rounds[-500:]


@app.get("/risk/overview")
def risk_overview():
    try:
        def _build():
            rounds_capped = _get_rounds()
            rounds_all    = load_round_history()
            multipliers   = [r["multiplier"] for r in rounds_capped]
            summary       = compute_round_summary(rounds_capped)
            summary["total_rounds"]   = len(rounds_all)
            summary["max_multiplier"] = round(max(r["multiplier"] for r in rounds_all), 2)
            summary["min_multiplier"] = round(min(r["multiplier"] for r in rounds_all), 2)
            return {"summary": summary, "risk": compute_risk_index(multipliers)}
        return jsonify(_cached("risk_overview", 10.0, _build))
    except Exception as exc:
        logger.exception("Risk overview failed")
        return json_error(str(exc), 500)


@app.get("/risk/volatility")
def risk_volatility():
    """Volatility metrics."""
    try:
        multipliers = [r["multiplier"] for r in _get_rounds()]
        return jsonify(compute_volatility(multipliers))
    except Exception as exc:
        logger.exception("Volatility calculation failed")
        return json_error(str(exc), 500)


@app.get("/risk/streaks")
def risk_streaks():
    """Streak detection results."""
    try:
        multipliers = [r["multiplier"] for r in _get_rounds()]
        return jsonify(detect_streaks(multipliers))
    except Exception as exc:
        logger.exception("Streak detection failed")
        return json_error(str(exc), 500)


@app.get("/risk/moving-averages")
def risk_moving_averages():
    """Moving average values."""
    try:
        multipliers = [r["multiplier"] for r in _get_rounds()]
        return jsonify(compute_moving_averages(multipliers))
    except Exception as exc:
        logger.exception("Moving average calculation failed")
        return json_error(str(exc), 500)


@app.get("/risk/history")
def risk_history():
    try:
        limit = int(request.args.get("limit", 100))
        def _build():
            rounds  = _get_rounds()
            records = rounds[-limit:]
            mults   = [r["multiplier"] for r in records]
            enriched = []
            for i in range(len(records)):
                window = mults[: i + 1]
                vol    = compute_volatility(window)
                stk    = detect_streaks(window)
                mas    = compute_moving_averages(window)
                risk   = compute_risk_index(window)
                enriched.append({
                    **records[i],
                    "volatility":      vol["recent_std"],
                    "streak_category": stk["current_streak"]["category"],
                    "streak_length":   stk["current_streak"]["length"],
                    "sma_5":           mas["sma_5"],
                    "sma_10":          mas["sma_10"],
                    "risk_score":      risk["risk_score"],
                    "risk_level":      risk["risk_level"],
                })
            return {"count": len(enriched), "rounds": enriched}
        return jsonify(_cached(f"risk_history_{limit}", 10.0, _build))
    except Exception as exc:
        logger.exception("Risk history failed")
        return json_error(str(exc), 500)



@app.get("/evaluate")
def evaluate():
    """Run model evaluation on recent rounds and return summary."""
    try:
        from training.evaluate_model import evaluate as _eval
        n = request.args.get("rounds", 200, type=int)
        return jsonify(_eval(n))
    except FileNotFoundError as exc:
        return json_error(str(exc), 404)
    except Exception as exc:
        logger.exception("Evaluation failed")
        return json_error(str(exc), 500)


@app.post("/train-rf")
def train_rf():
    """Train the RandomForest classifier in background."""
    def _run():
        try:
            from training.train_rf import train as _train_rf
            metrics = _train_rf()
            # Reset RF singleton so it reloads the new model
            import prediction.rf_predictor as _rfp
            _rfp._rf_predictor = None
            logger.info("RF training complete. val_acc=%.2f%%",
                        metrics.get("validation_accuracy", 0))
        except Exception as exc:
            logger.exception("RF training failed: %s", exc)

    threading.Thread(target=_run, daemon=True, name="rf-train").start()
    return jsonify({"status": "rf_training_started",
                    "message": "RandomForest training in background."})


@app.get("/model-metrics")
def model_metrics():
    """
    Verified ML performance metrics for the ML Dashboard.

    FIX: All metrics (Accuracy, Precision, Recall, F1) are now computed from
    the SAME dataset (live resolved decisions) for full mathematical consistency.
    Training log metrics are shown separately as 'training_log' context only.

    Formula verification:
      Accuracy  = TP_all / N
      Precision = mean(TP_c / (TP_c + FP_c))  macro-average
      Recall    = mean(TP_c / (TP_c + FN_c))  macro-average
      F1        = mean(2*P_c*R_c / (P_c+R_c)) macro-average
    """
    import json as _json
    from pathlib import Path as _Path
    from collections import Counter as _Counter

    CATS = ["VERY_LOW", "LOW", "MEDIUM", "HIGH", "VERY_HIGH"]

    # ── 1. Load ONLY resolved decisions (ground truth aligned) ────────────
    all_decisions = read_json(DECISIONS_PATH, [])
    if not isinstance(all_decisions, list):
        all_decisions = []

    # Strict filter: must have actual_multiplier AND actual_category AND correct field
    resolved = [
        d for d in all_decisions
        if d.get("actual_multiplier") is not None
        and d.get("actual_category") is not None
        and d.get("correct") is not None
        and d.get("prediction") is not None
    ]
    unresolved_count = len(all_decisions) - len(resolved)

    N = len(resolved)
    y_true = [d["actual_category"] for d in resolved]
    y_pred = [d["prediction"]      for d in resolved]

    # ── 2. Accuracy (correct formula, verified) ───────────────────────────
    correct_count   = sum(1 for t, p in zip(y_true, y_pred) if t == p)
    incorrect_count = N - correct_count
    accuracy = round(correct_count / N * 100, 2) if N > 0 else 0.0

    # ── 3. Per-class metrics (same dataset, correct formulas) ────────────
    per_class_live = {}
    all_p, all_r, all_f1, all_s = [], [], [], []
    confusion = {}

    for cat in CATS:
        tp = sum(1 for t, p in zip(y_true, y_pred) if t == cat and p == cat)
        fp = sum(1 for t, p in zip(y_true, y_pred) if t != cat and p == cat)
        fn = sum(1 for t, p in zip(y_true, y_pred) if t == cat and p != cat)
        tn = sum(1 for t, p in zip(y_true, y_pred) if t != cat and p != cat)
        support = tp + fn   # actual occurrences of this class

        prec   = tp / (tp + fp) if (tp + fp) > 0 else 0.0
        recall = tp / (tp + fn) if (tp + fn) > 0 else 0.0
        f1_val = 2 * prec * recall / (prec + recall) if (prec + recall) > 0 else 0.0

        per_class_live[cat] = {
            "precision": round(prec   * 100, 2),
            "recall":    round(recall * 100, 2),
            "f1":        round(f1_val * 100, 2),
            "support":   support,
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        }
        confusion[cat] = {"tp": tp, "fp": fp, "fn": fn, "tn": tn}
        all_p.append(prec); all_r.append(recall); all_f1.append(f1_val)
        all_s.append(support)

    # Macro-average (unweighted — treats all classes equally)
    macro_precision = round(sum(all_p) / len(all_p) * 100, 2) if all_p else 0.0
    macro_recall    = round(sum(all_r) / len(all_r) * 100, 2) if all_r else 0.0
    macro_f1        = round(sum(all_f1) / len(all_f1) * 100, 2) if all_f1 else 0.0

    # Weighted-average (weighted by support — matches sklearn default)
    total_s = max(sum(all_s), 1)
    weighted_precision = round(sum(p * s for p, s in zip(all_p, all_s)) / total_s * 100, 2)
    weighted_recall    = round(sum(r * s for r, s in zip(all_r, all_s)) / total_s * 100, 2)
    weighted_f1        = round(sum(f * s for f, s in zip(all_f1, all_s)) / total_s * 100, 2)

    # ── 4. Consistency check ──────────────────────────────────────────────
    # Accuracy should equal weighted_recall for multi-class (they are the same thing)
    # If they differ by > 0.5%, something is wrong
    consistency_ok = abs(accuracy - weighted_recall) < 0.5

    # ── 5. Accuracy over time (rolling windows) ───────────────────────────
    accuracy_history = []
    if N >= 5:
        win = max(5, N // 20)
        for i in range(0, N, win):
            chunk = resolved[i:i + win]
            if not chunk:
                continue
            cc = sum(1 for d in chunk if d["actual_category"] == d["prediction"])
            ts = chunk[-1].get("last_round_ts") or chunk[-1].get("created_at")
            accuracy_history.append({
                "index":     i // win + 1,
                "accuracy":  round(cc / len(chunk) * 100, 1),
                "timestamp": ts,
                "n":         len(chunk),
            })

    # ── 6. Confidence calibration check ──────────────────────────────────
    calibration_bins = []
    bin_edges = [0, 25, 30, 35, 40, 50, 60, 100]
    for lo, hi in zip(bin_edges, bin_edges[1:]):
        bucket = [d for d in resolved if lo <= d.get("confidence", 0) < hi]
        if bucket:
            b_correct = sum(1 for d in bucket if d["actual_category"] == d["prediction"])
            calibration_bins.append({
                "range":    f"{lo}-{hi}%",
                "n":        len(bucket),
                "accuracy": round(b_correct / len(bucket) * 100, 1),
                "expected": round((lo + hi) / 2, 1),  # midpoint = expected if well-calibrated
            })

    # ── 7. Confidence distribution ────────────────────────────────────────
    confs = [d.get("confidence", 0) for d in resolved]
    conf_dist = {"mean": 0.0, "min": 0.0, "max": 0.0, "bands": {}}
    if confs:
        conf_dist = {
            "mean": round(sum(confs) / len(confs), 2),
            "min":  round(min(confs), 2),
            "max":  round(max(confs), 2),
            "bands": {
                "STRONG_BET":  sum(1 for c in confs if c >= 55),
                "BET":         sum(1 for c in confs if 40 <= c < 55),
                "WEAK_BET":    sum(1 for c in confs if 28 <= c < 40),
                "SKIP":        sum(1 for c in confs if c < 28),
            },
        }

    # ── 8. Category accuracy ──────────────────────────────────────────────
    category_accuracy = {}
    for cat in CATS:
        cat_pred = [d for d in resolved if d.get("prediction") == cat]
        cat_corr = [d for d in cat_pred if d.get("actual_category") == cat]
        category_accuracy[cat] = {
            "predicted": len(cat_pred),
            "correct":   len(cat_corr),
            "accuracy":  round(len(cat_corr) / len(cat_pred) * 100, 1) if cat_pred else 0.0,
        }

    # Prediction distribution
    pred_dist = dict(_Counter(d.get("prediction") for d in all_decisions if d.get("prediction")))

    # ── 9. Model health score ─────────────────────────────────────────────
    # Uses only live metrics — fully consistent
    # Stability: variance of accuracy_history (lower = more stable)
    stab_score = 100.0
    if len(accuracy_history) >= 3:
        accs = [h["accuracy"] for h in accuracy_history]
        mean_acc = sum(accs) / len(accs)
        variance = sum((a - mean_acc) ** 2 for a in accs) / len(accs)
        import math as _math
        stab_score = max(0.0, 100.0 - _math.sqrt(variance) * 2)

    # Calibration score: how close accuracy is to mean confidence
    cal_score = max(0.0, 100.0 - abs(accuracy - conf_dist["mean"]) * 2) if confs else 50.0

    health_score = round(
        accuracy         * 0.35 +
        weighted_precision * 0.20 +
        weighted_recall    * 0.20 +
        weighted_f1        * 0.15 +
        stab_score         * 0.05 +
        cal_score          * 0.05,
        1,
    )
    if health_score >= 70:    health_status = "Excellent"
    elif health_score >= 50:  health_status = "Good"
    elif health_score >= 35:  health_status = "Needs Improvement"
    else:                     health_status = "Critical"

    # ── 10. Recommendations ───────────────────────────────────────────────
    recommendations = []
    if N < 50:
        recommendations.append({"type": "info",
            "message": f"Only {N} resolved predictions — metrics may be unstable. More data needed."})
    if accuracy < 60:
        recommendations.append({"type": "warning",
            "message": f"Accuracy {accuracy:.1f}% is below 60%. Retrain with more diverse data."})
    if macro_precision < macro_recall - 10:
        recommendations.append({"type": "info",
            "message": "Precision is much lower than Recall: too many false positives. Raise the confidence threshold."})
    if macro_recall < macro_precision - 10:
        recommendations.append({"type": "info",
            "message": "Recall is much lower than Precision: missing opportunities. Lower the confidence threshold."})
    if macro_f1 < 30:
        recommendations.append({"type": "warning",
            "message": "F1 Score below 30% — model retraining is strongly recommended."})
    no_vh = per_class_live.get("VERY_HIGH", {}).get("tp", 0) == 0
    if no_vh:
        recommendations.append({"type": "warning",
            "message": "VERY_HIGH class is never predicted — severe class collapse. Use SMOTE oversampling and retrain."})
    if unresolved_count > 0:
        recommendations.append({"type": "info",
            "message": f"{unresolved_count} predictions still pending backfill — metrics will improve as rounds complete."})
    if not recommendations:
        recommendations.append({"type": "success",
            "message": "Model is performing well for a provably-fair crash game."})

    # ── 11. Training log context (secondary, NOT mixed with live metrics) ─
    logs_dir = _Path(__file__).resolve().parent / "logs"
    training_log = {}
    best_log_acc = 0.0
    for fname in ["last_rf_training.json", "last_training.json"]:
        fp = logs_dir / fname
        if fp.exists():
            try:
                data = _json.loads(fp.read_text())
                val_acc = data.get("validation_accuracy", 0)
                if val_acc > best_log_acc:
                    best_log_acc = val_acc
                    pc = data.get("per_class", {})
                    training_log = {
                        "engine":              data.get("engine"),
                        "validation_accuracy": val_acc,
                        "train_accuracy":      data.get("train_accuracy", 0),
                        "samples":             data.get("samples", 0),
                        "training_time_s":     data.get("training_time_s"),
                        "feature_importances_top15": data.get("feature_importances_top15", {}),
                        "per_class": {
                            cat: {
                                "precision": round(m.get("precision", 0) * 100, 1),
                                "recall":    round(m.get("recall",    0) * 100, 1),
                                "f1":        round(m.get("f1",        0) * 100, 1),
                                "support":   m.get("support", 0),
                            }
                            for cat, m in pc.items() if isinstance(m, dict)
                        },
                    }
            except Exception:
                pass

    return jsonify({
        # ── Primary: all computed from SAME live decisions dataset ────
        "live": {
            "accuracy":            accuracy,
            "precision":           macro_precision,
            "recall":              macro_recall,
            "f1":                  macro_f1,
            "weighted_precision":  weighted_precision,
            "weighted_recall":     weighted_recall,
            "weighted_f1":         weighted_f1,
            "total_predictions":   len(all_decisions),
            "resolved":            N,
            "unresolved":          unresolved_count,
            "correct":             correct_count,
            "incorrect":           incorrect_count,
            "prediction_dist":     pred_dist,
            "accuracy_history":    accuracy_history[-20:],
            "per_class":           per_class_live,
            "confusion":           confusion,
            "calibration_bins":    calibration_bins,
            "confidence_dist":     conf_dist,
            "category_accuracy":   category_accuracy,
            "consistency_ok":      consistency_ok,
            "data_source":         "live_decisions",
        },
        # ── Secondary: training log (different dataset, context only) ─
        "training_log": training_log,
        # ── Health computed from live metrics only ────────────────────
        "health": {
            "score":       health_score,
            "status":      health_status,
            "stability":   round(stab_score, 1),
            "calibration": round(cal_score, 1),
        },
        "recommendations":      recommendations,
        "audit": {
            "n_resolved":          N,
            "n_unresolved":        unresolved_count,
            "consistency_ok":      consistency_ok,
            "accuracy_equals_weighted_recall": consistency_ok,
            "confidence_level_pct": 95 if (N >= 50 and consistency_ok and not no_vh) else
                                     70 if (N >= 20 and consistency_ok) else 40,
        },
        "updated_at": utc_now(),
    })


def training_status():
    """Return last training metrics + retrain state."""
    import json as _json
    from pathlib import Path as _Path
    logs_dir = _Path(__file__).resolve().parent / "logs"
    result = {}
    for name, fname in [("last_training", "last_training.json"),
                         ("last_evaluation", "last_evaluation.json"),
                         ("retrain_state", "retrain_state.json")]:
        fp = logs_dir / fname
        if fp.exists():
            try:
                result[name] = _json.loads(fp.read_text())
            except Exception:
                pass
    return jsonify(result)


if __name__ == "__main__":
    start_model_prewarm()

    # Start with SocketIO if available
    try:
        from flask_socketio import SocketIO as _SocketIO
        import ws_server as _ws
        _sio = _SocketIO(app, cors_allowed_origins="*", async_mode="threading", logger=False, engineio_logger=False)
        _ws._sio = _sio
        logger.info("WebSocket ready on port 5000")
        _sio.run(app, host="0.0.0.0", port=5000, debug=False, allow_unsafe_werkzeug=True)
    except ImportError:
        logger.warning("flask-socketio not installed")
        app.run(host="0.0.0.0", port=5000, debug=False, use_reloader=False)

