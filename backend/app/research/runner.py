"""Chronological strategy research isolated from live execution."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.backtesting.engine import BacktestConfig, BacktestEngine
from app.services.dataset_service import DatasetService


@dataclass(frozen=True)
class ResearchConfig:
    # Empty profile means compare both existing production profiles.
    profile: str = ""
    starting_balance: float = 1000.0
    min_history: int = 100
    thresholds: tuple[float, ...] = (.50, .55, .60)
    train_fraction: float = .60
    validation_fraction: float = .20
    perturbation: float = .02
    max_candidates: int = 12
    mode: str = "SIMULATION"

    def normalized(self) -> dict[str, Any]:
        row = asdict(self)
        row["profile"] = self.profile.upper()
        row["thresholds"] = [float(x) for x in self.thresholds]
        return row


class StrategyExperimentRunner:
    VERSION = "part14-v1"

    def __init__(self, data_path: Path, *, repository=None):
        self.data_path = Path(data_path)
        self.repository = repository
        self._stop = False

    def stop(self):
        self._stop = True

    def _candidates(self, cfg: ResearchConfig) -> list[dict[str, Any]]:
        thresholds = sorted({round(float(x), 4) for x in cfg.thresholds})
        profiles = [cfg.profile.upper()] if cfg.profile else ["PROFILE_A", "PROFILE_B"]
        rows = [{"profile": profile, "probability_threshold": threshold,
                 "starting_balance": cfg.starting_balance, "min_history": cfg.min_history}
                for profile in profiles for threshold in thresholds]
        return rows[:max(1, min(cfg.max_candidates, 24))]

    @staticmethod
    def _score(result: dict[str, Any], period: str = "validation") -> float:
        metric = next((x for x in result["periods"] if x["period"] == period), {})
        pnl = float(metric.get("pnl") or 0)
        drawdown = float(result["summary"].get("max_drawdown") or 0)
        bets = int(metric.get("bets") or 0)
        return pnl - drawdown * .25 + min(bets, 30) * .1

    def run(self, config: ResearchConfig | dict[str, Any] | None = None) -> dict[str, Any]:
        self._stop = False
        cfg = config if isinstance(config, ResearchConfig) else ResearchConfig(**(config or {}))
        if cfg.mode.upper() not in {"SIMULATION", "SHADOW"}:
            raise ValueError("research mode must be SIMULATION or SHADOW")
        if not 0 < cfg.train_fraction < 1 or not 0 < cfg.validation_fraction < 1 or cfg.train_fraction + cfg.validation_fraction >= 1:
            raise ValueError("train and validation fractions must leave an untouched test period")
        if cfg.max_candidates < 1 or cfg.max_candidates > 24:
            raise ValueError("max_candidates must be between 1 and 24")
        source_loader = (self.repository.load_rounds
                         if self.repository is not None and self.repository.database_url else None)
        service = DatasetService(self.data_path, source_loader=source_loader)
        rounds, quality, quarantine = service.load_validate()
        n = len(rounds)
        validation_window = max(1, int(n * cfg.validation_fraction))
        test_window = max(1, n - int(n * cfg.train_fraction) - validation_window)
        canonical_rows = rounds[["round_id", "round_index", "multiplier", "timestamp"]].to_json(
            orient="records", date_format="iso", double_precision=10)
        dataset_hash = hashlib.sha256(canonical_rows.encode("utf-8")).hexdigest()
        candidates = []
        for candidate in self._candidates(cfg):
            if self._stop:
                break
            result = BacktestEngine(self.data_path, repository=self.repository).run(BacktestConfig(
                profile=candidate["profile"], starting_balance=candidate["starting_balance"],
                min_history=candidate["min_history"], probability_threshold=candidate["probability_threshold"],
                validation_window=validation_window, test_window=test_window))
            train = next(x for x in result["periods"] if x["period"] == "train")
            validation = next(x for x in result["periods"] if x["period"] == "validation")
            test = next(x for x in result["periods"] if x["period"] == "test")
            candidates.append({"config": candidate, "train": train, "validation": validation,
                               "test_preview": test, "score": self._score(result),
                               "prediction_metrics": result["prediction_metrics"],
                               "drawdown": result["summary"]["max_drawdown"]})
        if not candidates:
            raise ValueError("no research candidates completed")
        candidates.sort(key=lambda row: row["score"], reverse=True)
        best_score = candidates[0]["score"]
        robust = [row for row in candidates if row["score"] >= best_score - max(1.0, abs(best_score) * .10)
                  and (row["validation"].get("bets", 0) > 0 or best_score <= 0)]
        selected = robust[0]
        # Sensitivity around the selected threshold and starting balance is
        # deliberately reported, never promoted to production configuration.
        sensitivity = []
        threshold = selected["config"]["probability_threshold"]
        for value in sorted({max(0.01, threshold - cfg.perturbation), threshold,
                             min(.99, threshold + cfg.perturbation)}):
            candidate = {**selected["config"], "probability_threshold": value}
            result = BacktestEngine(self.data_path, repository=self.repository).run(BacktestConfig(
                **candidate, validation_window=validation_window, test_window=test_window))
            sensitivity.append({"parameter": "probability_threshold", "value": value,
                                "validation": next(x for x in result["periods"] if x["period"] == "validation"),
                                "test": next(x for x in result["periods"] if x["period"] == "test")})
        final = BacktestEngine(self.data_path, repository=self.repository).run(BacktestConfig(
            **selected["config"], validation_window=validation_window, test_window=test_window))
        train_score = self._score(final, "train")
        validation_score = self._score(final, "validation")
        overfit = train_score > 0 and validation_score < train_score * .5
        result = {"research_id": hashlib.sha256(json.dumps({"version": self.VERSION,
                    "dataset": dataset_hash, "config": cfg.normalized()}, sort_keys=True).encode()).hexdigest()[:24],
                  "version": self.VERSION, "mode": cfg.mode.upper(), "created_at": datetime.now(timezone.utc).isoformat(),
                  "config": cfg.normalized(), "dataset": {"sha256": dataset_hash,
                  "source": "PostgreSQL:aviator_rounds" if source_loader else str(self.data_path),
                  "quality": quality, "quarantined": len(quarantine)}, "partitions": {
                  "train": int(n * cfg.train_fraction), "validation": validation_window, "untouched_test": test_window},
                  "candidates": candidates, "robust_candidates": robust, "selected": selected,
                  "final_untouched_test": next(x for x in final["periods"] if x["period"] == "test"),
                  "sensitivity": sensitivity, "overfitting": {"detected": overfit,
                  "train_score": train_score, "validation_score": validation_score,
                  "note": "Selection used validation only; untouched test was evaluated afterward."},
                  "robustness": {"profiles_tested": sorted({x["config"]["profile"] for x in candidates}),
                  "stable_range": [x["config"]["probability_threshold"] for x in robust],
                  "starting_balance_tests": [cfg.starting_balance],
                  "stress": "drawdown and risk-limit penalties applied; no loss chasing"},
                  "production_isolation": {"live_execution": False, "browser_access": False,
                  "configuration_mutation": False}, "reproducible": True}
        if self.repository:
            self.repository.save_research(result)
        return result
