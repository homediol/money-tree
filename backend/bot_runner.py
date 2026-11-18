"""Compatibility status adapter for the Orbit-owned Round History collector.

This module intentionally cannot launch a browser collector. The legacy
Playwright Round History runner has been removed; ``npm run collector`` owns
collection through the Orbit Data Watch API.
"""
import json
from pathlib import Path
from typing import Any, Dict

ROOT_DIR = Path(__file__).resolve().parent.parent
STATUS_PATH = ROOT_DIR / "data" / "bot" / "status.json"
LOG_PATH = ROOT_DIR / "data" / "bot" / "collector.log"


def _read_status() -> Dict[str, Any]:
    try:
        value = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def start(phone: str = "", password: str = "", headless: bool = False) -> Dict[str, Any]:
    """Reject the retired browser start route without persisting credentials."""
    status = _read_status()
    return {
        "success": False,
        "error": "round_history_uses_orbit_data_watch_api",
        "status": status.get("state", "STOPPED"),
        "source": status.get("dataSource", "ORBIT_DATA_WATCH_API"),
    }


def stop() -> Dict[str, Any]:
    """The Orbit collector is owned by its OS lock runner, never this API."""
    status = _read_status()
    if status.get("collectorRunning"):
        return {
            "success": False,
            "error": "collector_is_supervisor_managed",
            "status": status.get("state", "COLLECTING"),
        }
    return {"success": True, "message": "No legacy browser collector is running", "status": "idle"}


def request_manual_start() -> Dict[str, Any]:
    return {
        "success": False,
        "error": "round_history_uses_orbit_data_watch_api",
        "status": _read_status().get("state", "STOPPED"),
    }


def get_status() -> Dict[str, Any]:
    status = _read_status()
    running = status.get("collectorRunning") is True
    status["running"] = running
    status["pid_alive"] = running
    status["display_status"] = status.get("state", "STOPPED").lower()
    status.setdefault("dataSource", "ORBIT_DATA_WATCH_API")
    return status


def get_logs(tail: int = 100) -> Dict[str, Any]:
    try:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        return {"stdout": "\n".join(lines[-max(1, min(1000, tail)):]), "stderr": ""}
    except OSError:
        return {"stdout": "", "stderr": ""}
