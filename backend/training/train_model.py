"""
training/train_model.py
========================
True sequence LSTM classifier for Aviator crash prediction.

Architecture (per your specification):
    Input  → shape (20, 8)  — 20 rounds × 8 per-round features
    LSTM(128)
    Dropout(0.3)
    Dense(64, relu)
    Dropout(0.3)
    Dense(5, softmax)         ← 5 classes: VERY_LOW / LOW / MEDIUM / HIGH / VERY_HIGH

Why (20, 8) instead of (20, 1):
    Each round contributes 8 features beyond just the raw multiplier:
      [log_mult, category_onehot(5), streak_position(norm), relative_to_mean]
    This gives the LSTM richer per-timestep context without the information
    loss of collapsing 72 flat features into a fake sequence.

The flat 72-feature vector is still computed (for the predictor's scaler)
but the LSTM receives the proper (20, 8) shaped sequence.

Run directly:
    cd backend && python -m training.train_model

Saves:
    models/model.keras      — best checkpoint
    models/scaler.pkl       — StandardScaler for sequence features
    logs/last_training.json — training metrics
"""

from __future__ import annotations

import json
import logging
import pickle
import sys
import time
from pathlib import Path
from typing import Dict, List, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

log = logging.getLogger(__name__)

ROOT       = Path(__file__).resolve().parent.parent.parent
MODELS_DIR = ROOT / "models"
LOGS_DIR   = Path(__file__).resolve().parent.parent / "logs"
MODELS_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

MODEL_PATH   = MODELS_DIR / "model.keras"
SCALER_PATH  = MODELS_DIR / "scaler.pkl"
ENCODER_PATH = MODELS_DIR / "label_encoder.pkl"

CATEGORIES  = ["VERY_LOW", "LOW", "MEDIUM", "HIGH", "VERY_HIGH"]
NUM_CLASSES = len(CATEGORIES)
SEQ_LEN     = 20   # sequence length (rounds per sample)
SEQ_FEATS   = 8    # per-timestep features
FEATURE_DIM = 82   # flat feature vector dim (kept for scaler/predictor compat)

# Thresholds matching utils.py
_THRESHOLDS = [1.50, 2.00, 5.00, 15.0]


# ── Per-timestep sequence feature builder ─────────────────────────────────

import math as _math


def _log_scale(v: float) -> float:
    return _math.log(min(max(float(v), 1.0), 100.0)) / _math.log(100.0)


def _category_idx(v: float) -> int:
    for i, t in enumerate(_THRESHOLDS):
        if v < t:
            return i
    return len(_THRESHOLDS)


def build_sequence(window: List[float]) -> List[List[float]]:
    """
    Convert a 20-round window into a (20, 8) sequence tensor.

    Per-timestep features (8):
      [0] log_multiplier          log-scaled value
      [1] cat_very_low            one-hot: is VERY_LOW?
      [2] cat_low                 one-hot: is LOW?
      [3] cat_medium              one-hot: is MEDIUM?
      [4] cat_high                one-hot: is HIGH?
      [5] cat_very_high           one-hot: is VERY_HIGH?
      [6] rel_to_window_mean      (val - mean) / std  (z-score, clipped ±3)
      [7] streak_position         normalised position in current streak (0-1)
    """
    import statistics as _stats

    n   = len(window)
    if n == 0:
        return [[0.0] * SEQ_FEATS]

    # Window-level stats for z-score
    mean = _stats.mean(window)
    std  = _stats.pstdev(window) or 1.0

    # Build streak position: for each round, how many consecutive same-cat
    # rounds have occurred up to and including this position (normalised)
    cats = [_category_idx(v) for v in window]
    streak_positions = []
    streak_len = 0
    streak_cat = None
    for cat in cats:
        if cat == streak_cat:
            streak_len += 1
        else:
            streak_len = 1
            streak_cat = cat
        streak_positions.append(streak_len)
    max_streak = max(streak_positions) or 1

    seq = []
    for i, v in enumerate(window):
        cat = cats[i]
        one_hot = [1.0 if j == cat else 0.0 for j in range(NUM_CLASSES)]
        z_score = max(-3.0, min(3.0, (v - mean) / std)) / 3.0  # clip to [-1,1]
        streak_pos = streak_positions[i] / max_streak

        timestep = [_log_scale(v)] + one_hot + [z_score, streak_pos]
        assert len(timestep) == SEQ_FEATS
        seq.append(timestep)

    return seq


def build_sequence_matrix(
    multipliers: List[float],
    seq_len: int = SEQ_LEN,
) -> Tuple[List[List[List[float]]], List[int]]:
    """
    Slide a window and produce (N, 20, 8) shaped training data.

    Returns
    -------
    X : list of (seq_len, SEQ_FEATS) sequences
    y : list of int category labels for the *next* round
    """
    X, y = [], []
    for i in range(seq_len, len(multipliers)):
        window = multipliers[i - seq_len: i]
        X.append(build_sequence(window))
        y.append(_category_idx(multipliers[i]))
    return X, y


# ── Model architecture ────────────────────────────────────────────────────

def build_model(
    seq_len: int = SEQ_LEN,
    seq_feats: int = SEQ_FEATS,
    num_classes: int = NUM_CLASSES,
) -> "keras.Model":
    """
    True sequence LSTM.

    Input  : (batch, seq_len=20, seq_feats=8)
    LSTM(128)        — learns temporal patterns across 20 rounds
    Dropout(0.3)
    Dense(64, relu)  — non-linear mixing
    Dropout(0.3)
    Dense(5, softmax)
    """
    try:
        from tensorflow import keras
    except ImportError as exc:
        raise RuntimeError(
            "TensorFlow not installed. Run: pip install tensorflow>=2.15"
        ) from exc

    inputs = keras.Input(shape=(seq_len, seq_feats), name="sequence_input")

    # LSTM — single layer, stateless (reset between samples)
    x = keras.layers.LSTM(128, name="lstm")(inputs)
    x = keras.layers.Dropout(0.3, name="drop1")(x)

    # Dense head
    x = keras.layers.Dense(64, activation="relu", name="dense1")(x)
    x = keras.layers.Dropout(0.3, name="drop2")(x)

    outputs = keras.layers.Dense(
        num_classes, activation="softmax", name="output"
    )(x)

    model = keras.Model(inputs=inputs, outputs=outputs, name="aviator_lstm_seq")
    model.compile(
        optimizer=keras.optimizers.Adam(learning_rate=0.001),
        loss=keras.losses.SparseCategoricalCrossentropy(),
        metrics=["accuracy"],
    )
    model.summary(print_fn=log.info)
    return model


# ── Sequence scaler ───────────────────────────────────────────────────────

def _fit_seq_scaler(X_train_np):
    """
    Fit a StandardScaler on the flattened sequence features.
    Saves to SCALER_PATH for inference.
    """
    from sklearn.preprocessing import StandardScaler
    import numpy as np

    # Reshape (N, 20, 8) → (N, 160) for scaler, then back
    n, s, f = X_train_np.shape
    flat     = X_train_np.reshape(n, s * f)
    scaler   = StandardScaler()
    flat_s   = scaler.fit_transform(flat)
    X_scaled = flat_s.reshape(n, s, f).astype("float32")

    with open(SCALER_PATH, "wb") as fh:
        pickle.dump(scaler, fh)
    log.info("Sequence scaler saved → %s", SCALER_PATH)
    return scaler, X_scaled


def _apply_seq_scaler(scaler, X_np):
    """Apply a fitted scaler to a (N, 20, 8) array. Returns NaN-safe output."""
    import numpy as np
    n, s, f = X_np.shape
    flat    = X_np.reshape(n, s * f)
    # Replace any NaN/Inf in input before scaling
    flat    = np.nan_to_num(flat, nan=0.0, posinf=1.0, neginf=-1.0)
    flat_s  = scaler.transform(flat)
    # Guard output too
    flat_s  = np.nan_to_num(flat_s, nan=0.0, posinf=3.0, neginf=-3.0)
    return flat_s.reshape(n, s, f).astype("float32")


# ── Training entry point ──────────────────────────────────────────────────

def _setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(LOGS_DIR / "training.log"),
        ],
    )


def train(
    epochs: int = 100,
    batch_size: int = 256,
    oversample: bool = True,
    seq_len: int = SEQ_LEN,
) -> Dict:
    """
    Full training run with the true-sequence LSTM.

    1. Load rounds via scalable DatasetLoader
    2. Build (N, 20, 8) sequences
    3. Oversample minority classes
    4. StandardScaler fit on train, apply to val
    5. Train with EarlyStopping + ReduceLROnPlateau + ModelCheckpoint
    6. Evaluate and save metrics

    Returns a metrics dict.
    """
    _setup_logging()

    import numpy as np
    import random as _rnd
    from collections import Counter

    # ── 1. Load multipliers ────────────────────────────────────────────
    from training.dataset_loader import DatasetLoader
    log.info("Loading rounds via scalable DatasetLoader…")
    loader = DatasetLoader()
    loader.load()

    multipliers = [r["multiplier"] for r in loader._rounds]
    log.info("Rounds available: %d", len(multipliers))

    if len(multipliers) < seq_len + 10:
        raise ValueError(f"Need at least {seq_len + 10} rounds, got {len(multipliers)}.")

    # ── 2. Build (N, 20, 8) sequence matrix ───────────────────────────
    log.info("Building sequence matrix (seq_len=%d, seq_feats=%d)…", seq_len, SEQ_FEATS)
    t0 = time.time()
    X_raw, y_raw = build_sequence_matrix(multipliers, seq_len=seq_len)
    log.info("Sequences: %d in %.1fs", len(X_raw), time.time() - t0)

    # Distribution
    counts_raw = Counter(y_raw)
    counts = {CATEGORIES[i]: counts_raw.get(i, 0) for i in range(NUM_CLASSES)}
    log.info("Class distribution: %s", counts)

    # ── 3. Oversample minority classes ────────────────────────────────
    if oversample:
        majority = max(counts_raw.values())
        target   = int(majority * 0.5)
        X_os, y_os = list(X_raw), list(y_raw)
        for cls_idx in range(NUM_CLASSES):
            current = counts_raw.get(cls_idx, 0)
            needed  = max(0, target - current)
            if needed == 0:
                continue
            samples = [(X_raw[i], y_raw[i]) for i in range(len(y_raw))
                       if y_raw[i] == cls_idx]
            if not samples:
                continue
            cycle = samples * (needed // len(samples) + 1)
            for xv, yv in cycle[:needed]:
                X_os.append(xv)
                y_os.append(yv)
            log.info("Oversampled %-10s: %d → %d",
                     CATEGORIES[cls_idx], current, current + needed)
        X_raw, y_raw = X_os, y_os

    # Shuffle + split
    combined = list(zip(X_raw, y_raw))
    _rnd.shuffle(combined)
    X_shuf, y_shuf = zip(*combined)

    split = max(1, int(len(X_shuf) * 0.85))
    X_tr_list, X_v_list = X_shuf[:split], X_shuf[split:]
    y_tr_arr,  y_v_arr  = np.array(y_shuf[:split], dtype="int64"), \
                          np.array(y_shuf[split:],  dtype="int64")

    X_tr_np = np.array(X_tr_list, dtype="float32")   # (N, 20, 8)
    X_v_np  = np.array(X_v_list,  dtype="float32")

    # ── 4. Scale ──────────────────────────────────────────────────────
    scaler, X_tr_np = _fit_seq_scaler(X_tr_np)
    X_v_np          = _apply_seq_scaler(scaler, X_v_np)

    # Class weights
    n_total = len(y_tr_arr)
    class_weights = {
        i: n_total / (NUM_CLASSES * max(int((y_tr_arr == i).sum()), 1))
        for i in range(NUM_CLASSES)
    }
    log.info("Class weights: %s",
             {CATEGORIES[i]: round(w, 3) for i, w in class_weights.items()})

    # ── 5. Build + train model ─────────────────────────────────────────
    try:
        from tensorflow import keras
    except ImportError as exc:
        raise RuntimeError("TensorFlow not installed.") from exc

    model = build_model(seq_len=seq_len, seq_feats=SEQ_FEATS, num_classes=NUM_CLASSES)

    effective_batch = min(batch_size, max(64, len(X_tr_np) // 100))
    log.info("Effective batch size: %d", effective_batch)

    callbacks = [
        keras.callbacks.EarlyStopping(
            monitor="val_accuracy",
            patience=12,
            restore_best_weights=True,
            verbose=1,
        ),
        keras.callbacks.ReduceLROnPlateau(
            monitor="val_loss",
            factor=0.5,
            patience=6,
            min_lr=1e-6,
            verbose=1,
        ),
        keras.callbacks.ModelCheckpoint(
            str(MODEL_PATH),
            monitor="val_accuracy",
            save_best_only=True,
            verbose=1,
        ),
    ]

    log.info(
        "Training  epochs=%d  batch=%d  train=%d  val=%d",
        epochs, effective_batch, len(X_tr_np), len(X_v_np),
    )
    t_train = time.time()

    history = model.fit(
        X_tr_np, y_tr_arr,
        validation_data=(X_v_np, y_v_arr),
        epochs=epochs,
        batch_size=effective_batch,
        class_weight=class_weights,
        callbacks=callbacks,
        verbose=1,
    )

    elapsed = round(time.time() - t_train, 1)
    log.info("Training finished in %.1fs", elapsed)

    # ── 6. Reload best + evaluate ──────────────────────────────────────
    model = keras.models.load_model(str(MODEL_PATH), safe_mode=False)

    train_eval = model.evaluate(
        X_tr_np, y_tr_arr, verbose=0, batch_size=effective_batch
    )
    train_acc = round(float(train_eval[1]) * 100, 2)

    metrics = _evaluate(model, X_v_np, y_v_arr, counts)
    metrics.update({
        "train_accuracy":  train_acc,
        "training_time_s": elapsed,
        "epochs_run":      len(history.history["accuracy"]),
        "engine":          "tensorflow_lstm_seq",
        "samples":         int(len(X_tr_np)),
        "seq_len":         seq_len,
        "seq_feats":       SEQ_FEATS,
    })

    with open(LOGS_DIR / "last_training.json", "w") as fh:
        json.dump(metrics, fh, indent=2)

    log.info("model.keras saved → %s", MODEL_PATH)
    return metrics


# ── Evaluation ────────────────────────────────────────────────────────────

def _evaluate(model, X_val, y_val, counts: Dict) -> Dict:
    import numpy as np
    from collections import Counter

    y_pred_probs = model.predict(X_val, verbose=0)
    y_pred       = np.argmax(y_pred_probs, axis=1)

    # Bias
    dist           = Counter(y_pred.tolist())
    dominant       = max(dist, key=dist.get)
    dominant_ratio = dist[dominant] / max(len(y_pred), 1)
    bias_detected  = dominant_ratio > 0.70
    if bias_detected:
        log.warning(
            "⚠  Bias detected: class '%s' = %.1f%% of predictions.",
            CATEGORIES[dominant], dominant_ratio * 100,
        )

    accuracy = float(np.mean(y_pred == y_val)) * 100

    per_class = {}
    for i, cat in enumerate(CATEGORIES):
        tp = int(np.sum((y_pred == i) & (y_val == i)))
        fp = int(np.sum((y_pred == i) & (y_val != i)))
        fn = int(np.sum((y_pred != i) & (y_val == i)))
        prec   = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1     = (2 * prec * recall / (prec + recall)) if (prec + recall) else 0.0
        per_class[cat] = {
            "precision": round(prec, 4),
            "recall":    round(recall, 4),
            "f1":        round(f1, 4),
            "support":   counts.get(cat, 0),
        }

    conf_matrix = np.zeros((NUM_CLASSES, NUM_CLASSES), dtype=int)
    for true, pred in zip(y_val, y_pred):
        conf_matrix[true][pred] += 1

    log.info("Validation accuracy: %.2f%%", accuracy)
    log.info("%-12s  %9s  %9s  %9s  %8s",
             "Category", "Precision", "Recall", "F1", "Support")
    for cat, m in per_class.items():
        log.info("%-12s  %9.4f  %9.4f  %9.4f  %8d",
                 cat, m["precision"], m["recall"], m["f1"], m["support"])
    log.info("Confusion matrix (rows=actual, cols=predicted):")
    log.info("         " + "  ".join(f"{c[:5]:>5}" for c in CATEGORIES))
    for i, row in enumerate(conf_matrix.tolist()):
        log.info("%-9s %s", CATEGORIES[i][:9], "  ".join(f"{v:5d}" for v in row))

    return {
        "validation_accuracy": round(accuracy, 2),
        "per_class":           per_class,
        "confusion_matrix":    conf_matrix.tolist(),
        "bias_detected":       bias_detected,
    }


# ── CLI ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    metrics = train()
    print(json.dumps(metrics, indent=2))
