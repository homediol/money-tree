"""Deterministic, simulation-only chronological backtesting."""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from app.profiles import get_risk_profile
from app.services.dataset_service import DatasetService


@dataclass(frozen=True)
class BacktestConfig:
    profile: str = "PROFILE_A"
    starting_balance: float = 1000.0
    goal_balance: float | None = None
    min_history: int = 100
    train_window: int | None = None
    validation_window: int = 0
    test_window: int | None = None
    probability_threshold: float = 0.55
    stake: int = 100
    rolling: bool = False

    def normalized(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["profile"] = str(self.profile).upper()
        payload["starting_balance"] = float(self.starting_balance)
        payload["goal_balance"] = None if self.goal_balance is None else float(self.goal_balance)
        payload["stake"] = int(self.stake)
        return payload


class BacktestEngine:
    VERSION = "part13-v1"

    def __init__(self, data_path: Path, *, repository=None):
        self.data_path = Path(data_path)
        self.repository = repository
        self._stop = False

    def stop(self) -> None:
        self._stop = True

    @staticmethod
    def _probabilities(values: list[float], target: float = 2.0) -> tuple[float, float, float]:
        if not values:
            return 0.0, 0.0, 0.0
        all_rate = sum(v >= target for v in values) / len(values)
        recent = values[-50:]
        pattern = sum(v >= target for v in recent) / len(recent)
        ml = sum(v >= target for v in values[-100:]) / min(100, len(values))
        return all_rate, pattern, ml

    @staticmethod
    def _classification_metrics(probabilities: list[float], actuals: list[int]) -> dict[str, Any]:
        n = len(actuals)
        if not n:
            return {"n": 0, "accuracy": None, "precision": None, "recall": None,
                    "brier_score": None, "log_loss": None, "calibration": []}
        preds = [int(p >= .5) for p in probabilities]
        tp = sum(p == 1 and y == 1 for p, y in zip(preds, actuals))
        fp = sum(p == 1 and y == 0 for p, y in zip(preds, actuals))
        fn = sum(p == 0 and y == 1 for p, y in zip(preds, actuals))
        eps = 1e-12
        brier = sum((p - y) ** 2 for p, y in zip(probabilities, actuals)) / n
        loss = -sum(y * math.log(max(eps, p)) + (1 - y) * math.log(max(eps, 1 - p))
                    for p, y in zip(probabilities, actuals)) / n
        bins = []
        for low in np.arange(0, 1, .1):
            selected = [(p, y) for p, y in zip(probabilities, actuals) if low <= p < low + .1]
            if selected:
                bins.append({"lower": round(float(low), 1), "upper": round(float(low + .1), 1),
                             "count": len(selected), "predicted": sum(p for p, _ in selected) / len(selected),
                             "actual": sum(y for _, y in selected) / len(selected)})
        return {"n": n, "accuracy": sum(p == y for p, y in zip(preds, actuals)) / n,
                "precision": tp / (tp + fp) if tp + fp else None,
                "recall": tp / (tp + fn) if tp + fn else None,
                "brier_score": brier, "log_loss": loss, "calibration": bins}

    @staticmethod
    def _period_metrics(rows: list[dict[str, Any]], period: str) -> dict[str, Any]:
        subset = [r for r in rows if r["period"] == period]
        return {"period": period, "rounds": len(subset), "bets": sum(r["bet"] for r in subset),
                "wins": sum(r["win"] for r in subset), "pnl": round(sum(r["pnl"] for r in subset), 2)}

    def run(self, config: BacktestConfig | dict[str, Any] | None = None) -> dict[str, Any]:
        self._stop = False
        cfg = config if isinstance(config, BacktestConfig) else BacktestConfig(**(config or {}))
        profile = get_risk_profile(cfg.profile)
        if cfg.starting_balance <= 0 or cfg.stake < profile.minimum_bet:
            raise ValueError("starting_balance must be positive and stake must meet profile minimum")
        if cfg.stake > profile.maximum_bet:
            raise ValueError("stake exceeds profile maximum")
        service = DatasetService(self.data_path)
        rounds, quality, quarantine = service.load_validate()
        values = [float(v) for v in rounds["multiplier"].tolist()]
        dataset_hash = hashlib.sha256(self.data_path.read_bytes()).hexdigest()
        normalized = cfg.normalized()
        run_id = hashlib.sha256(json.dumps({"version": self.VERSION, "dataset": dataset_hash,
                                            "config": normalized}, sort_keys=True).encode()).hexdigest()[:24]
        balance = float(cfg.starting_balance)
        peak = balance
        max_drawdown = 0.0
        streak = 0
        max_streak = 0
        rows: list[dict[str, Any]] = []
        probabilities: list[float] = []
        actuals: list[int] = []
        seen_targets: set[str] = set()
        blocks: dict[str, int] = {}
        stopped_reason = None
        for index in range(max(1, cfg.min_history), len(values)):
            if self._stop:
                stopped_reason = "stopped"
                break
            target_row = rounds.iloc[index]
            target_id = str(target_row["round_id"])
            if target_id in seen_targets:
                blocks["duplicate_target"] = blocks.get("duplicate_target", 0) + 1
                continue
            seen_targets.add(target_id)
            prior = values[:index]
            train_start = max(0, index - cfg.train_window) if cfg.rolling and cfg.train_window else 0
            training_values = prior[train_start:]
            historical, pattern, ml = self._probabilities(training_values, profile.cashout if profile.key == "PROFILE_B" else 2.0)
            probability = (historical + pattern + ml) / 3
            actual = int(values[index] >= profile.cashout)
            probabilities.append(probability)
            actuals.append(actual)
            period = "test"
            if cfg.test_window and index < len(values) - cfg.test_window:
                period = "validation"
            if cfg.validation_window and index < max(cfg.min_history, len(values) - (cfg.validation_window + (cfg.test_window or 0))):
                period = "train"
            decision = probability >= cfg.probability_threshold and len(prior[train_start:]) >= cfg.min_history
            reason = "qualified" if decision else "probability_or_history_gate"
            bet = 0
            win = 0
            pnl = 0.0
            if cfg.goal_balance is not None and balance >= cfg.goal_balance:
                blocks["goal_reached"] = blocks.get("goal_reached", 0) + 1
                decision, reason = False, "goal_reached"
            elif balance - cfg.starting_balance <= -profile.maximum_session_loss:
                blocks["loss_limit"] = blocks.get("loss_limit", 0) + 1
                decision, reason = False, "loss_limit"
            elif streak >= profile.maximum_consecutive_losses:
                blocks["consecutive_loss_limit"] = blocks.get("consecutive_loss_limit", 0) + 1
                decision, reason = False, "consecutive_loss_limit"
            if decision:
                bet = 1
                win = actual
                pnl = cfg.stake * (profile.cashout - 1) if win else -cfg.stake
                balance = round(balance + pnl, 2)
                streak = 0 if win else streak + 1
                max_streak = max(max_streak, streak)
                peak = max(peak, balance)
                max_drawdown = max(max_drawdown, peak - balance)
            current_drawdown = round(max(0.0, peak - balance), 2)
            rows.append({"round_id": target_id, "index": index, "period": period,
                         "probability": probability, "actual": actual, "decision": decision,
                         "bet": bet, "win": win, "stake": cfg.stake if bet else 0,
                         "pnl": pnl, "balance": balance, "drawdown": current_drawdown, "reason": reason,
                         "source_round_id": str(rounds.iloc[index - 1]["round_id"])})
        bets = sum(r["bet"] for r in rows)
        wins = sum(r["win"] for r in rows)
        pnl = round(balance - cfg.starting_balance, 2)
        result = {"run_id": run_id, "version": self.VERSION, "mode": "HISTORICAL_SIMULATION",
                  "created_at": datetime.now(timezone.utc).isoformat(), "config": normalized,
                  "dataset": {"path": str(self.data_path), "sha256": dataset_hash,
                              "quality": quality, "quarantined": len(quarantine)},
                  "reproducible": True, "stopped_reason": stopped_reason,
                  "summary": {"rounds": len(rows), "bets": bets, "wins": wins,
                              "losses": bets - wins, "pnl": pnl,
                              "roi": pnl / (cfg.starting_balance or 1),
                              "ending_balance": balance, "max_drawdown": max_drawdown,
                              "max_consecutive_losses": max_streak,
                              "win_rate": wins / bets if bets else None,
                              "risk_blocks": blocks},
                  "prediction_metrics": self._classification_metrics(probabilities, actuals),
                  "periods": [self._period_metrics(rows, p) for p in ("train", "validation", "test")],
                  "baselines": {"no_bet": {"bets": 0, "pnl": 0, "roi": 0},
                                "historical_frequency": {"probability": sum(actuals) / len(actuals) if actuals else None},
                                "pattern_only": {"description": "recent-50 historical frequency"},
                                "ml_only": {"description": "recent-100 historical frequency"},
                                "full_system": {"description": "conservative average with production profile limits"}},
                  "equity_curve": [{"round_id": r["round_id"], "balance": r["balance"],
                                    "drawdown": r["drawdown"], "period": r["period"]} for r in rows],
                  "trades": rows,
                  "uncertainty": {"sample_size": bets, "note": "Historical sample uncertainty; no future guarantee."}}
        if self.repository:
            self.repository.save_backtest(result)
        return result
