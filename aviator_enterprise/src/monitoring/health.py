"""
System Health Monitor
=====================

Reports CPU, memory, disk, GPU, inference latency and an overall
health score.  The health score is a weighted combination of:
  * model availability  (25%)
  * rolling accuracy     (30%)
  * drift score          (15%)
  * resource headroom    (20%)
  * log error rate       (10%)
"""
from __future__ import annotations

import os
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from typing import Any, Deque, Dict, List, Optional

import psutil

from config.settings import settings
from src.core.helpers import utc_now_iso
from src.core.logger import get_logger

logger = get_logger(__name__)


@dataclass
class HealthReport:
    timestamp: str
    health_score: float
    components: Dict[str, float]
    cpu_percent: float
    memory_percent: float
    disk_percent: float
    gpu_percent: Optional[float] = None
    inference_latency_ms: float = 0.0
    error_rate: float = 0.0
    uptime_seconds: float = 0.0
    details: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class HealthMonitor:
    def __init__(self) -> None:
        self._boot = time.time()
        self.latency_buffer: Deque[float] = deque(maxlen=200)
        self.error_buffer: Deque[bool] = deque(maxlen=200)
        self.logger = get_logger(self.__class__.__name__)

    def record_latency(self, ms: float) -> None:
        self.latency_buffer.append(ms)

    def record_error(self, is_error: bool) -> None:
        self.error_buffer.append(is_error)

    # ---------------------------------------------------------- report
    def report(
        self,
        *,
        model_loaded: bool = True,
        rolling_accuracy: float = 0.0,
        drift_score: float = 0.0,
    ) -> HealthReport:
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent
        disk = psutil.disk_usage(str(settings.artifacts_dir)).percent
        gpu = self._gpu_usage()
        latency = float(sum(self.latency_buffer) / max(len(self.latency_buffer), 1))
        err_rate = float(sum(self.error_buffer) / max(len(self.error_buffer), 1)) if self.error_buffer else 0.0

        comp = {
            "model_availability": 1.0 if model_loaded else 0.0,
            "rolling_accuracy": max(0.0, min(rolling_accuracy, 1.0)),
            "drift": max(0.0, 1.0 - drift_score),
            "resource_headroom": max(0.0, 1.0 - max(cpu, mem) / 100.0),
            "error_rate": max(0.0, 1.0 - err_rate),
        }
        score = (
            0.25 * comp["model_availability"]
            + 0.30 * comp["rolling_accuracy"]
            + 0.15 * comp["drift"]
            + 0.20 * comp["resource_headroom"]
            + 0.10 * comp["error_rate"]
        ) * 100.0

        return HealthReport(
            timestamp=utc_now_iso(),
            health_score=float(score),
            components={k: float(v) for k, v in comp.items()},
            cpu_percent=cpu,
            memory_percent=mem,
            disk_percent=disk,
            gpu_percent=gpu,
            inference_latency_ms=latency,
            error_rate=err_rate,
            uptime_seconds=time.time() - self._boot,
            details={"latency_buffer_size": len(self.latency_buffer)},
        )

    def _gpu_usage(self) -> Optional[float]:
        try:
            import torch
            if torch.cuda.is_available():
                return float(torch.cuda.utilization())
        except Exception:
            return None
        return None


__all__ = ["HealthMonitor", "HealthReport"]

