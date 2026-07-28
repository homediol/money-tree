"""
Generic, dependency-free helpers shared across the codebase.

Everything here is pure, side-effect free and unit-testable.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np


# ---------------------------------------------------------------------------
# Time
# ---------------------------------------------------------------------------
def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def epoch_seconds() -> float:
    return time.time()


# ---------------------------------------------------------------------------
# Identifiers
# ---------------------------------------------------------------------------
def short_uuid(length: int = 8) -> str:
    return uuid.uuid4().hex[:length]


def deterministic_hash(payload: Any) -> str:
    """SHA-256 of a JSON-serialised payload — for dataset/model versioning."""
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------
class _NumpyJSONEncoder(json.JSONEncoder):
    def default(self, o: Any) -> Any:  # noqa: D401
        if isinstance(o, (np.integer,)):
            return int(o)
        if isinstance(o, (np.floating,)):
            f = float(o)
            if math.isnan(f) or math.isinf(f):
                return None
            return f
        if isinstance(o, np.ndarray):
            return o.tolist()
        if isinstance(o, (datetime,)):
            return o.isoformat()
        if isinstance(o, Path):
            return str(o)
        return super().default(o)


def safe_json_dumps(obj: Any, **kwargs: Any) -> str:
    return json.dumps(obj, cls=_NumpyJSONEncoder, **kwargs)


def safe_json_loads(payload: str) -> Any:
    try:
        return json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------
def safe_float(x: Any, default: float = 0.0) -> float:
    """Convert to float, replacing NaN/Inf with `default`."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return default
    if math.isnan(f) or math.isinf(f):
        return default
    return f


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def rolling_window(seq: Sequence[Any], window: int) -> List[Sequence[Any]]:
    if window <= 0:
        raise ValueError("window must be positive")
    if window > len(seq):
        return []
    return [seq[i : i + window] for i in range(len(seq) - window + 1)]


def entropy(probs: Sequence[float]) -> float:
    """Shannon entropy (base 2) of a discrete probability distribution."""
    total = 0.0
    for p in probs:
        if p > 0:
            total -= p * math.log2(p)
    return total


# ---------------------------------------------------------------------------
# Categorisation
# ---------------------------------------------------------------------------
CATEGORY_PATTERN = re.compile(r"^\[?\s*([0-9]*\.?[0-9]+)\s*\]?$")


def parse_multiplier(token: Any) -> Optional[float]:
    if token is None:
        return None
    if isinstance(token, (int, float)):
        f = float(token)
        return f if f >= 1.0 else None
    if isinstance(token, str):
        m = CATEGORY_PATTERN.match(token.strip())
        if m:
            f = float(m.group(1))
            return f if f >= 1.0 else None
        m = re.search(r"([0-9]*\.?[0-9]+)", token)
        if m:
            f = float(m.group(1))
            return f if f >= 1.0 else None
    return None


def category_from_multiplier(value: float, boundaries: Sequence[float]) -> int:
    """Return the index of the bin `value` falls into."""
    for i, b in enumerate(boundaries):
        if value < b:
            return i
    return len(boundaries)


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------
def ensure_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    return p


def file_size_mb(p: Path) -> float:
    if not p.exists():
        return 0.0
    return p.stat().st_size / (1024 * 1024)


# ---------------------------------------------------------------------------
# Decorators
# ---------------------------------------------------------------------------
def timed(logger=None):
    """Decorator: logs execution time."""
    def decorator(fn):
        def wrapper(*args, **kwargs):
            t0 = time.perf_counter()
            try:
                return fn(*args, **kwargs)
            finally:
                dt = (time.perf_counter() - t0) * 1000
                if logger is not None:
                    logger.info(f"{fn.__name__} executed in {dt:.1f} ms")
        return wrapper
    return decorator


def batched(iterable: Iterable, n: int):
    """Yield successive `n`-sized chunks."""
    batch: List[Any] = []
    for item in iterable:
        batch.append(item)
        if len(batch) == n:
            yield batch
            batch = []
    if batch:
        yield batch


__all__ = [
    "utc_now", "utc_now_iso", "epoch_seconds",
    "short_uuid", "deterministic_hash",
    "safe_json_dumps", "safe_json_loads", "safe_float", "clamp",
    "rolling_window", "entropy",
    "parse_multiplier", "category_from_multiplier",
    "ensure_dir", "file_size_mb",
    "timed", "batched",
]

