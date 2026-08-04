import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
ARTIFACT_DIR = Path(__file__).resolve().parent / "artifacts"
ROUND_HISTORY_PATH = DATA_DIR / "roundhistory.json"
DECISIONS_PATH     = DATA_DIR / "decisions.json"
LOG_PATH = Path(__file__).resolve().parent / "aviator-api.log"
METADATA_PATH = ARTIFACT_DIR / "metadata.json"

# Per-file write locks — prevents race conditions when multiple threads write simultaneously
_write_locks: Dict[str, threading.Lock] = {}
_write_locks_lock = threading.Lock()

def _get_write_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _write_locks_lock:
        if key not in _write_locks:
            _write_locks[key] = threading.Lock()
        return _write_locks[key]

# 5-bin classification for better prediction granularity
CATEGORIES = ["VERY_LOW", "LOW", "MEDIUM", "HIGH", "VERY_HIGH"]
CATEGORY_RANGES = {
    "VERY_LOW":  (1.00, 1.50),
    "LOW":       (1.50, 2.00),
    "MEDIUM":    (2.00, 5.00),
    "HIGH":      (5.00, 15.0),
    "VERY_HIGH": (15.0, 999.0),
}

# Single source of truth for confidence thresholds
MIN_CONFIDENCE = 70.0              # TF model: below this = low_confidence
MIN_CONFIDENCE_TO_STORE = 15.0     # store ALL predictions above 15% — need 1000+ for accurate metrics
MIN_CONFIDENCE_STATISTICAL = 22.0  # statistical_ensemble realistic ceiling

# Confidence bands for BET quality
CONF_BAND_STRONG  = 55.0   # 55-100% = Strong BET
CONF_BAND_BET     = 40.0   # 40-54%  = BET
CONF_BAND_WEAK    = 28.0   # 28-39%  = Weak BET
# Below 28% = SKIP


def setup_logging() -> None:
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH),
            logging.StreamHandler(),
        ],
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_data_files() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    if not ROUND_HISTORY_PATH.exists():
        sample = [
            {"round_id": i + 1, "multiplier": float(v), "timestamp": utc_now()}
            for i, v in enumerate(
                [
                    1.12, 1.34, 2.18, 1.06, 5.42, 1.49, 3.28, 1.22, 8.71, 2.93,
                    1.01, 1.77, 4.25, 1.38, 12.4, 2.11, 1.52, 1.09, 6.85, 3.67,
                    1.24, 1.44, 2.72, 1.15, 9.31, 1.89, 3.05, 1.18, 1.63, 7.26,
                    1.31, 2.45, 1.07, 4.92, 1.56, 14.2, 2.32, 1.28, 3.71, 1.03,
                    6.11, 1.81, 2.64, 1.41, 1.17, 10.8, 3.12, 1.35, 2.02, 5.73,
                    1.21, 1.69, 4.48, 1.11, 7.94, 2.84, 1.47, 1.25, 3.51, 11.6,
                ]
            )
        ]
        write_json(ROUND_HISTORY_PATH, sample)
    if not DECISIONS_PATH.exists():
        write_json(DECISIONS_PATH, [])


def read_json(path: Path, default: Any) -> Any:
    try:
        if not path.exists():
            return default
        content = path.read_text(encoding="utf-8").strip()
        if not content:
            return default
        return json.loads(content)
    except (json.JSONDecodeError, OSError):
        logging.exception("Invalid JSON in %s", path)
        return default


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = _get_write_lock(path)
    with lock:
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        try:
            tmp_path.write_text(
                json.dumps(payload, indent=2), encoding="utf-8"
            )
            tmp_path.replace(path)
        except OSError:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise


def load_round_history() -> List[Dict[str, Any]]:
    ensure_data_files()
    raw = read_json(ROUND_HISTORY_PATH, [])
    if isinstance(raw, dict):
        raw = raw.get("rounds", raw.get("history", []))

    rows: List[Dict[str, Any]] = []
    for index, item in enumerate(raw):
        if isinstance(item, (int, float)):
            rows.append({"round_id": index + 1, "multiplier": float(item), "timestamp": None})
        elif isinstance(item, dict):
            value = item.get("multiplier", item.get("crashPoint", item.get("value")))
            if value is None:
                continue
            rows.append(
                {
                    "round_id": item.get("round_id", item.get("round_index", item.get("id", index + 1))),
                    "multiplier": float(value),
                    "timestamp": item.get("timestamp"),
                }
            )

    cleaned = []
    for row in rows:
        multiplier = row.get("multiplier")
        if multiplier is None or multiplier < 1.0:
            continue
        cleaned.append({**row, "category": multiplier_to_category(multiplier)})
    return cleaned


def multiplier_to_category(multiplier: float) -> str:
    if multiplier < 1.50:
        return "VERY_LOW"
    if multiplier < 2.00:
        return "LOW"
    if multiplier < 5.00:
        return "MEDIUM"
    if multiplier < 15.0:
        return "HIGH"
    return "VERY_HIGH"


# Recommended cashout: conservative target based on category + confidence.
# Formula: base × (1 + confidence_bonus) — always below the expected crash point.
def category_to_recommended_cashout(category: str, confidence: float) -> float:
    conf = max(0.0, min(confidence, 100.0)) / 100.0
    targets = {
        "VERY_LOW":  (1.10, 1.30),   # base, max
        "LOW":       (1.30, 1.70),
        "MEDIUM":    (1.70, 2.80),
        "HIGH":      (2.80, 5.50),
        "VERY_HIGH": (5.50, 12.0),
    }
    base, top = targets.get(category, (1.20, 1.80))
    return round(base + (top - base) * conf, 2)


def risk_level(category: str, confidence: float) -> str:
    """Risk to the PLAYER (not the house): higher multiplier target = higher risk."""
    if category in ("HIGH", "VERY_HIGH"):
        if confidence >= 75:
            return "MEDIUM"
        return "HIGH"
    if category == "MEDIUM":
        return "MEDIUM" if confidence < 70 else "LOW"
    # VERY_LOW / LOW — safe targets, low risk
    return "LOW"


def append_decision(decision: Dict[str, Any]) -> None:
    decisions = read_json(DECISIONS_PATH, [])
    if not isinstance(decisions, list):
        decisions = []
    # Backfill for_round if missing (legacy decisions)
    if "for_round" not in decision and decision.get("last_round_id") is not None:
        decision["for_round"] = decision["last_round_id"] + 1
    decisions.append(decision)
    write_json(DECISIONS_PATH, decisions[-250:])




