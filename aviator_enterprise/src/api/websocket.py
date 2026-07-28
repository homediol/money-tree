"""
WebSocket — metrics push
========================

Pushes dashboard health / performance snapshots to connected clients on a
configurable interval.  There is NO live game feed and NO automatic
prediction loop here.  Predictions are generated only when the user clicks
"Run Prediction" in the dashboard.
"""
from __future__ import annotations

import asyncio
import json
import time
from typing import Any, Dict

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from config.settings import settings
from src.core.logger import get_logger

router = APIRouter()
logger = get_logger(__name__)


@router.websocket("/ws/metrics")
async def ws_metrics(websocket: WebSocket) -> None:
    """
    Push a lightweight metrics snapshot every ``dashboard_refresh_ms`` ms.

    Payload shape::

        {
          "type":    "metrics",
          "ts":      <epoch float>,
          "counts":  { total, resolved, pending, correct, incorrect },
          "rolling": { accuracy, f1_macro, ... },
          "health":  { cpu_percent, memory_percent, health_score, ... }
        }
    """
    await websocket.accept()
    services = websocket.app.state.services
    interval = settings.dashboard_refresh_ms / 1000.0

    try:
        while True:
            try:
                payload: Dict[str, Any] = {
                    "type":    "metrics",
                    "ts":      time.time(),
                    "counts":  services.performance_tracker.counts(),
                    "rolling": services.performance_tracker.rolling_metrics(50),
                    "health":  services.health.report().to_dict(),
                }
                await websocket.send_text(json.dumps(payload, default=str))
            except Exception as exc:
                logger.debug("WS metrics push error: %s", exc)
                await websocket.send_text(
                    json.dumps({"type": "error", "message": str(exc)})
                )
            await asyncio.sleep(interval)
    except WebSocketDisconnect:
        return
