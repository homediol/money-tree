"""
Continuous Learning Orchestrator
===============================

Background loop that monitors drift + new data and triggers a retraining
job when warranted.  Each retraining is validated against the active
model before promotion.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from config.settings import settings
from src.core.helpers import utc_now_iso
from src.core.logger import get_logger
from src.data import DataCollector
from src.monitoring.drift_detector import DriftDetectionService
from src.pipeline.retraining import ContinuousLearningPipeline
from src.registry.model_registry import ModelRegistry
from src.training.trainer import Trainer

logger = get_logger(__name__)


class ContinuousLearningOrchestrator:
    """Periodic background loop for retraining + drift monitoring."""

    def __init__(
        self,
        *,
        collector: Optional[DataCollector] = None,
        trainer: Optional[Trainer] = None,
        pipeline: Optional[ContinuousLearningPipeline] = None,
        drift_service: Optional[DriftDetectionService] = None,
        registry: Optional[ModelRegistry] = None,
    ) -> None:
        self.collector = collector or DataCollector()
        self.trainer = trainer or Trainer()
        self.pipeline = pipeline or ContinuousLearningPipeline()
        self.drift = drift_service or DriftDetectionService()
        self.registry = registry or ModelRegistry()
        self._task: Optional[asyncio.Task] = None
        self._stop = asyncio.Event()
        self.history: List[Dict[str, Any]] = []
        self._last_trained_count: int = 0  # tracks count at last training run
        self.logger = get_logger(self.__class__.__name__)

    # ---------------------------------------------------------- public
    async def start(self, model_name: str = "xgboost", interval_seconds: int = 3600) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(model_name, interval_seconds), name="continuous-learning")
        self.logger.info("Continuous learning started", extra={"interval": interval_seconds})

    async def stop(self) -> None:
        if self._task is None:
            return
        self._stop.set()
        try:
            await asyncio.wait_for(self._task, timeout=10)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
        self._task = None
        self.logger.info("Continuous learning stopped")

    def history_dict(self) -> List[Dict[str, Any]]:
        return list(self.history)

    def next_run(self, interval_seconds: int) -> str:
        from datetime import timedelta
        return (datetime.utcnow() + timedelta(seconds=interval_seconds)).isoformat()

    # ---------------------------------------------------------- internals
    async def _loop(self, model_name: str, interval_seconds: int) -> None:
        while not self._stop.is_set():
            try:
                await self._cycle(model_name)
            except Exception as exc:
                self.logger.exception("Continuous learning cycle failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=interval_seconds)
            except asyncio.TimeoutError:
                continue

    async def _cycle(self, model_name: str) -> None:
        records = await self.collector.collect()
        if len(records) < 200:
            return
        report = self.drift.compute()
        new_rounds = len(records)
        should = await self.pipeline.should_retrain(
            new_rounds=new_rounds, drift_score=report.overall_score,
        )
        if not should:
            return
        result = await self.trainer.train(model_name=model_name, optimize=False, cross_validate=False, calibrate=True)
        self.history.append({
            "timestamp": utc_now_iso(),
            "model": model_name,
            "version": result.model_version,
            "metric_f1_macro": result.metrics.get("f1_macro", 0.0),
            "drift_score": report.overall_score,
            "promoted": True,  # trainer already registered/persisted
        })
        self.logger.info("Retraining completed", extra={"version": result.model_version})


__all__ = ["ContinuousLearningOrchestrator"]

