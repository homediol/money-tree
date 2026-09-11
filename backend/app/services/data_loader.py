from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pandas as pd

from app.core.logging import get_logger

log = get_logger("DATA")


@dataclass
class DataQualityReport:
    source_path: str
    detected_format: str
    total_records: int
    valid_records: int
    invalid_records: int
    duplicates_removed: int
    first_round_index: int | None
    last_round_index: int | None
    first_timestamp: str | None
    last_timestamp: str | None
    errors: list[str]

    def model_dump(self) -> dict[str, Any]:
        return self.__dict__.copy()


def _extract_rows(payload: Any) -> tuple[list[Any], str]:
    if isinstance(payload, list):
        return payload, "array"
    if isinstance(payload, dict):
        for key in ("rounds", "history", "data", "records", "items"):
            if isinstance(payload.get(key), list):
                return payload[key], f"object.{key}"
    return [], type(payload).__name__


def _extract_multiplier(item: Any) -> float | None:
    raw = item
    if isinstance(item, dict):
        raw = item.get("multiplier", item.get("crashPoint", item.get("value", item.get("x"))))
    if isinstance(raw, str):
        raw = raw.strip().lower().replace("x", "").replace(",", "")
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 1:
        return None
    return round(value, 4)


def _extract_timestamp(item: Any) -> str | None:
    if not isinstance(item, dict):
        return None
    value = item.get("timestamp", item.get("time", item.get("ts")))
    return str(value) if value else None


def _extract_round_index(item: Any, position: int) -> int:
    if isinstance(item, dict):
        raw = item.get("round_index", item.get("round_id", item.get("id")))
        try:
            value = int(raw)
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return position + 1


class RoundHistoryLoader:
    def __init__(self, path: Path):
        self.path = Path(path)

    def load(self) -> tuple[pd.DataFrame, DataQualityReport]:
        errors: list[str] = []
        if not self.path.exists():
            report = DataQualityReport(str(self.path), "missing", 0, 0, 0, 0, None, None, None, None, ["roundhistory.json not found"])
            return self._empty_frame(), report

        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            report = DataQualityReport(str(self.path), "invalid_json", 0, 0, 0, 0, None, None, None, None, [str(exc)])
            return self._empty_frame(), report

        rows, detected = _extract_rows(payload)
        cleaned: list[dict[str, Any]] = []
        invalid = 0
        for pos, item in enumerate(rows):
            multiplier = _extract_multiplier(item)
            if multiplier is None:
                invalid += 1
                continue
            cleaned.append(
                {
                    "round_index": _extract_round_index(item, pos),
                    "multiplier": multiplier,
                    "timestamp": _extract_timestamp(item),
                }
            )

        frame = pd.DataFrame(cleaned, columns=["round_index", "multiplier", "timestamp"])
        duplicates = 0
        if not frame.empty:
            before = len(frame)
            frame = frame.drop_duplicates(subset=["round_index", "timestamp", "multiplier"], keep="last")
            duplicates = before - len(frame)
            frame["timestamp_dt"] = pd.to_datetime(frame["timestamp"], errors="coerce", utc=True)
            frame = frame.sort_values(["round_index", "timestamp_dt"], na_position="last").reset_index(drop=True)
            frame["target"] = (frame["multiplier"] >= 2.0).astype(int)
        else:
            frame = self._empty_frame()

        report = DataQualityReport(
            source_path=str(self.path),
            detected_format=detected,
            total_records=len(rows),
            valid_records=len(frame),
            invalid_records=invalid,
            duplicates_removed=duplicates,
            first_round_index=int(frame["round_index"].iloc[0]) if not frame.empty else None,
            last_round_index=int(frame["round_index"].iloc[-1]) if not frame.empty else None,
            first_timestamp=str(frame["timestamp"].iloc[0]) if not frame.empty else None,
            last_timestamp=str(frame["timestamp"].iloc[-1]) if not frame.empty else None,
            errors=errors,
        )
        log.info("Loaded %s valid rounds from %s", report.valid_records, self.path)
        return frame, report

    @staticmethod
    def _empty_frame() -> pd.DataFrame:
        return pd.DataFrame(columns=["round_index", "multiplier", "timestamp", "timestamp_dt", "target"])

