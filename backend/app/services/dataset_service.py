"""Real-history validation, leakage-safe feature engineering and dataset cache."""
from __future__ import annotations

import json
import math
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import Any

import numpy as np
import pandas as pd

WINDOWS = (5, 10, 20, 50, 100)
BUCKETS = {"LOW": "<1.50", "MEDIUM": "1.50-3.99", "HIGH": ">=4.00"}


@dataclass(frozen=True)
class FeatureMetadata:
    name: str
    type: str
    window: int | None
    description: str
    uses_future_data: bool = False


def multiplier_bucket(value: float) -> str:
    if value < 1.5:
        return "LOW"
    if value < 4.0:
        return "MEDIUM"
    return "HIGH"


def _atomic_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    os.replace(tmp, path)


def _json_value(value: Any) -> Any:
    if isinstance(value, (np.integer,)):
        return int(value)
    if isinstance(value, (np.floating,)):
        return None if not math.isfinite(float(value)) else float(value)
    if pd.isna(value):
        return None
    return value


class DatasetService:
    """Builds a chronological dataset whose row N uses only rounds before N."""

    def __init__(self, raw_path: Path, processed_dir: Path | None = None,
                 features_dir: Path | None = None, source_loader=None):
        self.raw_path = Path(raw_path)
        self.source_loader = source_loader
        data_dir = self.raw_path.parent
        self.processed_dir = Path(processed_dir or data_dir / "processed")
        self.features_dir = Path(features_dir or data_dir / "features")
        self.dataset_path = self.processed_dir / "training_dataset.csv"
        self.quality_path = self.processed_dir / "data_quality.json"
        self.quarantine_path = self.processed_dir / "quarantine.json"
        self.status_path = self.processed_dir / "dataset_status.json"
        self.metadata_path = self.features_dir / "metadata.json"
        self._lock = RLock()
        self.clean_rounds = pd.DataFrame()
        self.dataset = pd.DataFrame()
        self.quality: dict[str, Any] = {}
        self.quarantine: list[dict[str, Any]] = []
        self.metadata = self._build_metadata()
        self.last_mode = "not_processed"

    @staticmethod
    def _extract_payload(payload: Any) -> list[Any]:
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("rounds", "history", "data", "records", "items"):
                if isinstance(payload.get(key), list):
                    return payload[key]
        return []

    def load_validate(self) -> tuple[pd.DataFrame, dict[str, Any], list[dict[str, Any]]]:
        if self.source_loader is not None:
            try:
                raw_rows = self.source_loader()
            except Exception as exc:
                return self._empty_rounds(), self._quality(0, 0, 0, [], []), [{"reason": "persistent_history_unavailable", "detail": str(exc)}]
        elif not self.raw_path.exists():
            return self._empty_rounds(), self._quality(0, 0, 0, [], []), [{"reason": "missing_history", "record": None}]
        else:
            try:
                payload = json.loads(self.raw_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                return self._empty_rounds(), self._quality(0, 0, 0, [], []), [{"reason": "corrupt_json", "detail": str(exc)}]
            raw_rows = self._extract_payload(payload)
        valid: list[dict[str, Any]] = []
        quarantine: list[dict[str, Any]] = []
        seen: set[str] = set()
        duplicates = 0
        missing = {"round_id": 0, "timestamp": 0, "multiplier": 0}
        # Parse the column once. Parsing each scalar invokes pandas' format
        # inference thousands of times and delays the live stream.
        parsed_times = pd.to_datetime([
            item.get("timestamp", item.get("time", item.get("ts"))) if isinstance(item, dict) else None
            for item in raw_rows
        ], format="mixed", errors="coerce", utc=True)

        for position, item in enumerate(raw_rows):
            reasons: list[str] = []
            if not isinstance(item, dict):
                quarantine.append({"position": position, "reason": "corrupted_record", "record": item})
                continue
            raw_id = item.get("round_id", item.get("round_index", item.get("id")))
            if raw_id is None or str(raw_id).strip() == "":
                missing["round_id"] += 1
                reasons.append("missing_round_id")
            round_id = str(raw_id).strip() if raw_id is not None else ""
            raw_multiplier = item.get("multiplier", item.get("crashPoint", item.get("value")))
            if raw_multiplier is None:
                missing["multiplier"] += 1
            try:
                cleaned = str(raw_multiplier).strip().lower().replace("x", "").replace(",", "")
                multiplier = float(cleaned)
                if not math.isfinite(multiplier) or multiplier <= 0:
                    raise ValueError
            except (TypeError, ValueError):
                multiplier = math.nan
                reasons.append("invalid_multiplier")
            raw_timestamp = item.get("timestamp", item.get("time", item.get("ts")))
            if not raw_timestamp:
                missing["timestamp"] += 1
                reasons.append("missing_timestamp")
                parsed_timestamp = pd.NaT
            else:
                parsed_timestamp = parsed_times[position]
                if pd.isna(parsed_timestamp):
                    reasons.append("invalid_timestamp")
            if round_id and round_id in seen:
                duplicates += 1
                reasons.append("duplicate_round_id")
            if reasons:
                quarantine.append({"position": position, "reason": reasons, "record": item})
                continue
            seen.add(round_id)
            try:
                order = int(item.get("round_index", raw_id))
            except (TypeError, ValueError):
                order = position + 1
            valid.append({
                "round_id": round_id, "round_index": order,
                "timestamp": parsed_timestamp.isoformat().replace("+00:00", "Z"),
                "timestamp_dt": parsed_timestamp, "multiplier": round(multiplier, 4),
            })

        frame = pd.DataFrame(valid, columns=["round_id", "round_index", "timestamp", "timestamp_dt", "multiplier"])
        if not frame.empty:
            # The collector's numeric round index is the game sequence. A
            # clock correction must not change which outcome follows a round.
            frame = frame.sort_values("round_index", kind="stable").reset_index(drop=True)
        values = frame["multiplier"].tolist() if not frame.empty else []
        quality = self._quality(len(raw_rows), len(frame), duplicates, values, frame.to_dict("records"), missing)
        if len(frame) > 1:
            delta = frame["timestamp_dt"].diff().dt.total_seconds()
            quality["timestamp_inversions"] = int((delta < 0).sum())
            quality["collection_gaps_over_120s"] = int((delta > 120).sum())
            quality["round_index_gaps"] = int((frame["round_index"].diff().fillna(1) != 1).sum())
            breaks = np.flatnonzero((~delta.between(0, 120) | frame["round_index"].diff().ne(1)).to_numpy())
            quality["latest_contiguous_rounds"] = int(len(frame) - breaks[-1])
        else:
            quality["latest_contiguous_rounds"] = len(frame)
        return frame, quality, quarantine

    @staticmethod
    def _empty_rounds() -> pd.DataFrame:
        return pd.DataFrame(columns=["round_id", "round_index", "timestamp", "timestamp_dt", "multiplier"])

    @staticmethod
    def _quality(total: int, valid: int, duplicates: int, values: list[float], rows: list[dict], missing: dict | None = None) -> dict:
        first = rows[0] if rows else None
        last = rows[-1] if rows else None
        count_2x = sum(value >= 2 for value in values)
        return {
            "total_rounds": total, "valid_rounds": valid,
            "invalid_rounds": total - valid - duplicates,
            "duplicates": duplicates,
            "date_range": {"start": first.get("timestamp") if first else None, "end": last.get("timestamp") if last else None},
            "min": min(values) if values else None, "max": max(values) if values else None,
            "mean": float(np.mean(values)) if values else None,
            "median": float(np.median(values)) if values else None,
            "count_2x_plus": count_2x,
            "rate_2x_plus": count_2x / valid if valid else None,
            "missing_values": missing or {"round_id": 0, "timestamp": 0, "multiplier": 0},
            "chronological": True,
        }

    @staticmethod
    def _build_metadata() -> list[FeatureMetadata]:
        items = [FeatureMetadata(f"last_{i}", "numeric", i, f"Multiplier {i} round(s) before the target round") for i in range(1, 6)]
        items += [
            FeatureMetadata("streak_below_1_5", "integer", None, "Consecutive prior rounds below 1.50x"),
            FeatureMetadata("streak_below_2", "integer", None, "Consecutive prior rounds below 2.00x"),
            FeatureMetadata("streak_below_3", "integer", None, "Consecutive prior rounds below 3.00x"),
            FeatureMetadata("sequence_last_10", "categorical_sequence", 10, "LOW/MEDIUM/HIGH buckets for the prior 10 rounds"),
        ]
        items += [FeatureMetadata(f"log_last_{i}", "numeric", i, "Clipped log multiplier from a prior round") for i in range(1, 6)]
        items += [FeatureMetadata(f"prior_2x_lag_{i}", "numeric", i, "Prior round met the 2.00x threshold") for i in range(1, 6)]
        for window in WINDOWS:
            for stat in ("mean", "median", "min", "max", "std", "q25", "q75", "log_mean"):
                items.append(FeatureMetadata(f"{stat}_last_{window}", "numeric", window, f"{stat.title()} of prior {window} multipliers"))
            for name, description in (
                ("below_1_5", "below 1.50x"), ("eq_1_5", "equal to 1.50x"),
                ("eq_2_0", "equal to 2.00x"), ("eq_3_0", "equal to 3.00x"),
                ("eq_5_0", "equal to 5.00x"), ("eq_10_0", "equal to 10.00x"),
                ("2x", "at least 2.00x"), ("below_1_2", "below 1.20x"), ("gte_5", "at least 5.00x"),
            ):
                items.append(FeatureMetadata(f"rate_{name}_last_{window}", "numeric", window, f"Rate {description} in prior {window} rounds"))
        items += [FeatureMetadata("recent_vs_long_2x_rate", "numeric", None, "Prior 10-round minus 100-round 2x rate"),
                  FeatureMetadata("recent_vs_long_log_mean", "numeric", None, "Prior 10-round minus 100-round log mean")]
        for window in (10, 25, 100):
            items += [FeatureMetadata(f"log_std_last_{window}", "numeric", window, "Volatility of clipped log multipliers from prior rounds"),
                      FeatureMetadata(f"alternation_rate_last_{window}", "numeric", window, "Fraction of adjacent prior outcomes changing sides of 2x")]
        items += [FeatureMetadata("log_momentum_1_2", "numeric", 2, "Difference between the two most recent clipped log multipliers")]
        items += [FeatureMetadata("recent_vs_long_volatility", "numeric", None, "Prior 10-round minus 100-round log volatility"),
                  FeatureMetadata("recent_vs_long_2x_rate_5_50", "numeric", None, "Prior 5-round minus 50-round 2x rate")]
        for state in (0, 1):
            items += [FeatureMetadata(f"transition_after_{state}_count_100", "integer", 100,
                                      "Number of prior transitions from this 2x state in the recent window"),
                      FeatureMetadata(f"transition_after_{state}_rate_100", "numeric", 100,
                                      "Smoothed prior 2x frequency after this prior state")]
        for pattern in ("streak", "sequence_2", "sequence_3"):
            items += [FeatureMetadata(f"pattern_{pattern}_count", "integer", None,
                                      "Previously observed, contiguous target rounds matching this past-only Pattern Engine state"),
                      FeatureMetadata(f"pattern_{pattern}_rate", "numeric", None,
                                      "Causally smoothed >=2x rate after prior matching Pattern Engine states")]
        return items

    @staticmethod
    def _pattern_evidence(keys: pd.Series, outcomes: pd.Series, eligible: pd.Series,
                          historical_rate: pd.Series) -> tuple[pd.Series, pd.Series]:
        """For row i, aggregate matching labels j<i only (including at inference)."""
        observations = eligible.astype("int64")
        successes = outcomes.astype("int64") * observations
        count = observations.groupby(keys, sort=False).cumsum() - observations
        hits = successes.groupby(keys, sort=False).cumsum() - successes
        # Thirty pseudo-observations shrink rare patterns toward the causal
        # overall frequency; this is evidence, not a future-aware target encoding.
        rate = (hits + 30 * historical_rate) / (count + 30)
        return count, rate

    @staticmethod
    def _pattern_summary(rounds: pd.DataFrame) -> tuple[dict, int, dict, list[str], Any]:
        """Compact causal state after known rounds; used for fast live updates."""
        totals: dict[str, dict[Any, list[int]]] = {name: {} for name in ("streak", "sequence_2", "sequence_3")}
        positives = 0
        streaks = {1.5: 0, 2.0: 0, 3.0: 0}
        buckets: list[str] = []
        previous = None
        for row in rounds.itertuples(index=False):
            keys = {"streak": min(streaks[2.0], 6),
                    "sequence_2": "|".join(buckets[-2:]),
                    "sequence_3": "|".join(buckets[-3:])}
            if previous is not None:
                seconds = (row.timestamp_dt - previous.timestamp_dt).total_seconds()
                if 0 <= seconds <= 120 and row.round_index - previous.round_index == 1:
                    for name, key in keys.items():
                        pair = totals[name].setdefault(key, [0, 0])
                        pair[0] += 1
                        pair[1] += int(row.multiplier >= 2)
            positives += int(row.multiplier >= 2)
            for threshold in streaks:
                streaks[threshold] = streaks[threshold] + 1 if row.multiplier < threshold else 0
            buckets.append(multiplier_bucket(row.multiplier))
            previous = row
        return totals, positives, streaks, buckets[-3:], previous

    @staticmethod
    def _live_pattern_features(totals: dict, positives: int, streaks: dict,
                               buckets: list[str], history_count: int) -> dict[str, float | int]:
        result: dict[str, float | int] = {
            "streak_below_1_5": streaks[1.5],
            "streak_below_2": streaks[2.0],
            "streak_below_3": streaks[3.0],
        }
        keys = {"streak": min(streaks[2.0], 6),
                "sequence_2": "|".join(buckets[-2:]),
                "sequence_3": "|".join(buckets[-3:])}
        prior_rate = (positives + 10) / (history_count + 20)
        for name, key in keys.items():
            count, hits = totals[name].get(key, [0, 0])
            result[f"pattern_{name}_count"] = count
            result[f"pattern_{name}_rate"] = (hits + 30 * prior_rate) / (count + 30)
        return result

    @staticmethod
    def _streak_before(values: pd.Series, threshold: float) -> pd.Series:
        output, streak = [], 0
        for value in values:
            output.append(streak)
            streak = streak + 1 if value < threshold else 0
        return pd.Series(output, index=values.index, dtype="int64")

    def _engineer(self, rounds: pd.DataFrame) -> pd.DataFrame:
        if rounds.empty:
            return pd.DataFrame()
        values = rounds["multiplier"].astype(float)
        prior = values.shift(1)
        columns: dict[str, Any] = {
            "round_id": rounds["round_id"], "round_index": rounds["round_index"],
            "timestamp": rounds["timestamp"], "target_2x": (values >= 2.0).astype("int8"),
        }
        for lag in range(1, 6):
            columns[f"last_{lag}"] = values.shift(lag)
            columns[f"log_last_{lag}"] = np.log1p(values.shift(lag).clip(upper=100))
        for threshold, name in ((1.5, "streak_below_1_5"), (2.0, "streak_below_2"), (3.0, "streak_below_3")):
            columns[name] = self._streak_before(values, threshold)
        buckets = values.map(multiplier_bucket)
        bucket_values = buckets.tolist()
        columns["sequence_last_10"] = ["|".join(bucket_values[max(0, i - 10):i]) for i in range(len(rounds))]
        for lag in range(1, 6):
            columns[f"prior_2x_lag_{lag}"] = (values.shift(lag) >= 2.0).astype(float)

        for window in WINDOWS:
            rolling = prior.rolling(window, min_periods=window)
            minimum, maximum = rolling.min(), rolling.max()
            columns[f"mean_last_{window}"] = rolling.mean()
            columns[f"median_last_{window}"] = rolling.median()
            columns[f"min_last_{window}"] = minimum
            columns[f"max_last_{window}"] = maximum
            columns[f"std_last_{window}"] = rolling.std(ddof=0)
            columns[f"q25_last_{window}"] = rolling.quantile(.25)
            columns[f"q75_last_{window}"] = rolling.quantile(.75)
            columns[f"log_mean_last_{window}"] = np.log1p(prior.clip(upper=100)).rolling(window, min_periods=window).mean()
            predicates = {
                "below_1_5": prior < 1.5, "eq_1_5": np.isclose(prior, 1.5),
                "eq_2_0": np.isclose(prior, 2.0), "eq_3_0": np.isclose(prior, 3.0),
                "eq_5_0": np.isclose(prior, 5.0), "eq_10_0": np.isclose(prior, 10.0),
                "2x": prior >= 2.0, "below_1_2": prior < 1.2, "gte_5": prior >= 5.0,
            }
            for name, predicate in predicates.items():
                counts = pd.Series(predicate, index=rounds.index).rolling(window, min_periods=window).sum()
                columns[f"rate_{name}_last_{window}"] = counts / window
        columns["recent_vs_long_2x_rate"] = columns["rate_2x_last_10"] - columns["rate_2x_last_100"]
        columns["recent_vs_long_log_mean"] = columns["log_mean_last_10"] - columns["log_mean_last_100"]
        log_values = np.log1p(prior.clip(upper=100))
        binary = (values >= 2).astype("int8")
        switches = (binary != binary.shift(1)).astype(float)
        for window in (10, 25, 100):
            columns[f"log_std_last_{window}"] = log_values.rolling(window, min_periods=window).std(ddof=0)
            # A window of w prior outcomes contains w-1 transitions.
            columns[f"alternation_rate_last_{window}"] = switches.shift(1).rolling(window - 1, min_periods=window - 1).mean()
        columns["log_momentum_1_2"] = columns["log_last_1"] - columns["log_last_2"]
        columns["recent_vs_long_volatility"] = columns["log_std_last_10"] - columns["log_std_last_100"]
        columns["recent_vs_long_2x_rate_5_50"] = columns["rate_2x_last_5"] - columns["rate_2x_last_50"]
        # Each transition ends at i-1 or earlier. The current row's target
        # cannot enter either the numerator or denominator.
        previous_state = binary.shift(2)
        prior_outcome = binary.shift(1)
        for state in (0, 1):
            matched = (previous_state == state).astype(float)
            count = matched.rolling(100, min_periods=1).sum()
            hits = (matched * prior_outcome.fillna(0)).rolling(100, min_periods=1).sum()
            columns[f"transition_after_{state}_count_100"] = count
            columns[f"transition_after_{state}_rate_100"] = (hits + 10) / (count + 20)
        # This mirrors PatternEngine's capped low-streak and LOW/MEDIUM/HIGH
        # sequence definitions, but each historical label is only available to
        # later rows. Do not count outcomes across a collector gap as "next".
        seconds = pd.to_datetime(rounds["timestamp"], utc=True, errors="coerce").diff().dt.total_seconds()
        eligible = ((seconds >= 0) & (seconds <= 120) & (rounds["round_index"].diff() == 1)).fillna(False)
        prior_successes = binary.cumsum() - binary
        historical_rate = (prior_successes + 10) / (np.arange(len(rounds)) + 20)
        pattern_keys = {"streak": columns["streak_below_2"].clip(upper=6)}
        for length in (2, 3):
            pattern_keys[f"sequence_{length}"] = pd.Series(
                ["|".join(bucket_values[max(0, i - length):i]) for i in range(len(rounds))], index=rounds.index)
        for name, keys in pattern_keys.items():
            count, rate = self._pattern_evidence(keys, binary, eligible, historical_rate)
            columns[f"pattern_{name}_count"] = count
            columns[f"pattern_{name}_rate"] = rate
        result = pd.DataFrame(columns)
        return result.iloc[max(WINDOWS):].reset_index(drop=True)

    def build_training_dataset(self, persist: bool = True) -> pd.DataFrame:
        rounds, quality, quarantine = self.load_validate()
        dataset = self._engineer(rounds)
        with self._lock:
            self.clean_rounds, self.dataset = rounds, dataset
            self.quality, self.quarantine = quality, quarantine
            self.last_mode = "full"
        if persist:
            self._persist()
        return dataset.copy()

    def process_incremental(self) -> pd.DataFrame:
        rounds, quality, quarantine = self.load_validate()
        with self._lock:
            old = self.clean_rounds
            prefix_ok = (not old.empty and len(rounds) >= len(old) and
                         rounds.iloc[:len(old)][["round_id", "timestamp", "multiplier"]].reset_index(drop=True).equals(
                             old[["round_id", "timestamp", "multiplier"]].reset_index(drop=True)))
            if prefix_ok and len(rounds) > len(old):
                # Rolling statistics need only their window; causal pattern
                # counts are carried forward from the full observed prefix.
                offset = max(0, len(old) - max(WINDOWS) - 1)
                tail = self._engineer(rounds.iloc[offset:].reset_index(drop=True))
                new_ids = set(rounds.iloc[len(old):]["round_id"])
                appended = tail[tail["round_id"].isin(new_ids)].copy().reset_index(drop=True)
                lookup = {str(value): i for i, value in enumerate(appended["round_id"])}
                totals, positives, streaks, buckets, previous = self._pattern_summary(old)
                for j, row in enumerate(rounds.iloc[len(old):].itertuples(index=False), start=len(old)):
                    features = self._live_pattern_features(totals, positives, streaks, buckets, j)
                    position = lookup.get(str(row.round_id))
                    if position is not None:
                        for name, value in features.items():
                            appended.at[position, name] = value
                    seconds = (row.timestamp_dt - previous.timestamp_dt).total_seconds()
                    if 0 <= seconds <= 120 and row.round_index - previous.round_index == 1:
                        keys = {"streak": min(streaks[2.0], 6),
                                "sequence_2": "|".join(buckets[-2:]),
                                "sequence_3": "|".join(buckets[-3:])}
                        for name, key in keys.items():
                            pair = totals[name].setdefault(key, [0, 0])
                            pair[0] += 1
                            pair[1] += int(row.multiplier >= 2)
                    positives += int(row.multiplier >= 2)
                    for threshold in streaks:
                        streaks[threshold] = streaks[threshold] + 1 if row.multiplier < threshold else 0
                    buckets.append(multiplier_bucket(row.multiplier))
                    buckets = buckets[-3:]
                    previous = row
                self.dataset = pd.concat([self.dataset, appended], ignore_index=True)
                self.last_mode = "incremental"
            elif prefix_ok and len(rounds) == len(old):
                self.last_mode = "unchanged"
            else:
                self.dataset = self._engineer(rounds)
                self.last_mode = "full"
            self.clean_rounds, self.quality, self.quarantine = rounds, quality, quarantine
        self._persist()
        return self.dataset.copy()

    def chronological_split(self, train: float = 0.70, validation: float = 0.15) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
        if train <= 0 or validation < 0 or train + validation >= 1:
            raise ValueError("train and validation fractions must leave a positive test split")
        n = len(self.dataset)
        train_end, validation_end = int(n * train), int(n * (train + validation))
        return self.dataset.iloc[:train_end].copy(), self.dataset.iloc[train_end:validation_end].copy(), self.dataset.iloc[validation_end:].copy()

    def latest_features(self) -> dict[str, Any] | None:
        with self._lock:
            rounds = self.clean_rounds
            if len(rounds) < max(WINDOWS):
                return None
            return self._features_for_next(rounds)

    def _features_for_next(self, rounds: pd.DataFrame) -> dict[str, Any]:
        # Add a placeholder target row. The 100-round context suffices for
        # rolling features; full-prefix state supplies the pattern evidence.
        placeholder = pd.DataFrame([{
            "round_id": "NEXT", "round_index": int(rounds.iloc[-1]["round_index"]) + 1,
            "timestamp": None, "timestamp_dt": pd.NaT, "multiplier": 0.0,
        }])
        context = rounds.iloc[-max(WINDOWS) - 1:]
        row = self._engineer(pd.concat([context, placeholder], ignore_index=True)).iloc[-1]
        features = {key: _json_value(value) for key, value in row.items() if key != "target_2x"}
        totals, positives, streaks, buckets, _ = self._pattern_summary(rounds)
        features.update(self._live_pattern_features(totals, positives, streaks, buckets, len(rounds)))
        return features

    def features_after_round(self, source_round_id: str) -> dict[str, Any] | None:
        """Feature vector for the unknown round immediately after source_round_id."""
        with self._lock:
            matches = self.clean_rounds.index[self.clean_rounds["round_id"].astype(str) == str(source_round_id)]
            if not len(matches):
                return None
            rounds = self.clean_rounds.iloc[: int(matches[0]) + 1].copy()
        if len(rounds) < max(WINDOWS):
            return None
        return self._features_for_next(rounds)

    def status(self) -> dict[str, Any]:
        train, validation, test = self.chronological_split() if len(self.dataset) else ([], [], [])
        return {
            "status": "READY" if len(self.dataset) else "WAITING",
            "source": str(self.raw_path), "processed_path": str(self.dataset_path),
            "feature_rows": len(self.dataset), "feature_count": len(self.metadata),
            "last_processing_mode": self.last_mode,
            "splits": {"train": len(train), "validation": len(validation), "test": len(test)},
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def _persist(self) -> None:
        _atomic_text(self.dataset_path, self.dataset.to_csv(index=False))
        _atomic_text(self.quality_path, json.dumps(self.quality, indent=2, default=str))
        _atomic_text(self.quarantine_path, json.dumps(self.quarantine, indent=2, default=str))
        _atomic_text(self.metadata_path, json.dumps({"buckets": BUCKETS, "features": [asdict(item) for item in self.metadata]}, indent=2))
        _atomic_text(self.status_path, json.dumps(self.status(), indent=2))

    def metadata_payload(self) -> dict[str, Any]:
        return {"buckets": BUCKETS, "features": [asdict(item) for item in self.metadata]}
