from __future__ import annotations

import json
from pathlib import Path
from datetime import datetime, timezone

from fastapi import APIRouter, Request

router = APIRouter(prefix="/api/backend", tags=["backend-stability"])


def _parse_utc(value):
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc) if parsed.tzinfo is None else parsed.astimezone(timezone.utc)
    except (TypeError, ValueError):
        return None


def _node_collector_snapshot(source: dict, supervisor: dict, now: datetime | None = None) -> dict:
    """Normalize the separately supervised Node collector for backend health APIs."""
    now = now or datetime.now(timezone.utc)
    heartbeat_at = source.get("heartbeatAt") or supervisor.get("heartbeat_at")
    collected_at = source.get("lastSuccessfulCollection") or supervisor.get("last_persisted_at")
    heartbeat_time = _parse_utc(heartbeat_at)
    collected_time = _parse_utc(collected_at)
    heartbeat_age = (now - heartbeat_time).total_seconds() if heartbeat_time else None
    collection_age = (now - collected_time).total_seconds() if collected_time else None
    heartbeat_state = ("UNKNOWN" if heartbeat_age is None else
                       "FRESH" if 0 <= heartbeat_age <= 20 else "STALE")
    collection_fresh = collection_age is not None and 0 <= collection_age <= 120

    process_running = source.get("collectorRunning")
    if not isinstance(process_running, bool):
        process_running = (supervisor.get("collector_process_status") == "RUNNING"
                           and supervisor.get("process_verified") is True)
    logged_in = source.get("loggedIn")
    if not isinstance(logged_in, bool) and isinstance(supervisor.get("logged_in"), bool):
        logged_in = supervisor["logged_in"]
    auth_required = source.get("authRequired") is True or supervisor.get("auth_required") is True

    runtime_status = "HEALTHY"
    reason = None
    if process_running is not True:
        runtime_status, reason = "STOPPED", "collector process is not verified running"
    elif auth_required:
        runtime_status, reason = "AUTH_REQUIRED", "collector reports authentication or verification required"
    elif not collection_fresh:
        runtime_status, reason = "STALE", "no fresh persisted round within 120 seconds"
    elif heartbeat_state != "FRESH":
        runtime_status, reason = "STALE", "collector heartbeat is stale or unavailable"
    elif logged_in is not True:
        runtime_status, reason = "AUTH_UNVERIFIED", "authenticated session is not verified"
    elif (source.get("health") != "HEALTHY" or source.get("state") != "COLLECTING"
          or source.get("browserConnected") is not True
          or source.get("pageConnected") is not True
          or source.get("frameConnected") is not True):
        runtime_status, reason = "DEGRADED", "collector, browser, page, or Aviator frame is not ready"

    last_error = source.get("lastError")
    if not isinstance(last_error, (dict, str)):
        last_error = supervisor.get("last_error")
    network = supervisor.get("network") if isinstance(supervisor.get("network"), dict) else {}
    return {
        "source": "data/bot/status.json",
        "runtime_status": runtime_status,
        "collector_process_status": "RUNNING" if process_running is True else
            "STOPPED" if process_running is False else "UNKNOWN",
        "collector_running": process_running,
        "health": source.get("health") or supervisor.get("status"),
        "state": source.get("state"),
        "heartbeat_at": heartbeat_at,
        "heartbeat_age_seconds": heartbeat_age,
        "heartbeat_state": heartbeat_state,
        "last_successful_collection": collected_at,
        "collection_age_seconds": collection_age,
        "collection_fresh": collection_fresh,
        "logged_in": logged_in if isinstance(logged_in, bool) else None,
        "authentication_status": "AUTHENTICATED" if logged_in is True and not auth_required else
            "AUTH_REQUIRED" if auth_required else "UNVERIFIED",
        "auth_required": auth_required,
        "browser_connected": source.get("browserConnected") is True or supervisor.get("browser_connected") is True,
        "history_page_connected": source.get("pageConnected") is True or supervisor.get("history_page_connected") is True,
        "frame_connected": source.get("frameConnected") is True or supervisor.get("frame_connected") is True,
        "last_observed_round_id": source.get("lastObservedRoundId") or supervisor.get("last_observed_round"),
        "last_persisted_round_id": source.get("lastRoundId") or supervisor.get("last_persisted_round"),
        "last_error": last_error,
        "network_status": network.get("status"),
        "reason": reason or source.get("reason") or supervisor.get("reason"),
        "supervisor_status": supervisor.get("status"),
        "supervisor_snapshot_at": supervisor.get("observed_at") or supervisor.get("updated_at"),
    }


def _read_node_collector_snapshot() -> dict:
    root = Path(__file__).resolve().parents[3]
    try:
        source = json.loads((root / "data/bot/status.json").read_text(encoding="utf-8"))
        if not isinstance(source, dict):
            source = {}
    except (OSError, json.JSONDecodeError):
        source = {}
    try:
        supervisor = json.loads((root / "data/bot/browser-reliability.json").read_text(encoding="utf-8"))
        if not isinstance(supervisor, dict):
            supervisor = {}
    except (OSError, json.JSONDecodeError):
        supervisor = {}
    return _node_collector_snapshot(source, supervisor)


@router.get("/capabilities")
async def capabilities(request: Request) -> dict:
    """Report runtime features that must be active before the dashboard is safe to operate."""
    orchestrator = getattr(request.app.state, "orchestrator", None)
    tasks = getattr(orchestrator, "tasks", ()) if orchestrator else ()
    balance_task = next((task for task in tasks if task.get_name() == "platform-balance"), None)
    balance_monitor_running = bool(balance_task and not balance_task.done())
    history = getattr(request.app.state, "history_collector", None)
    rules_engine = getattr(request.app.state, "rules_engine", None)
    reconciliation = getattr(request.app.state, "reconciliation", None)
    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    financial_stop_recovery = bool(
        rules_engine and callable(getattr(rules_engine, "recover_financial_state", None))
        and repository and callable(getattr(repository, "list_reconciled_rule_executions", None))
        and callable(getattr(repository, "has_open_rule_session_executions", None)))
    receipt_to_rules = bool(reconciliation and rules_engine
        and getattr(reconciliation, "rules_engine", None) is rules_engine
        and callable(getattr(repository, "record_rule_execution_reconciliation", None)))
    return {
        "ok": True,
        "service": "winner-predict-backend",
        "capabilities": {
            "read_only_platform_balance_monitor": balance_monitor_running,
            "history_collector_status": history is not None,
            "rule_financial_stop_recovery": financial_stop_recovery,
            "receipt_reconciliation_to_rule_session": receipt_to_rules,
        },
    }


@router.get("/stability")
async def stability(request: Request) -> dict:
    """Return the persisted lifecycle and isolated worker health snapshot."""
    lifecycle = getattr(request.app.state, "lifecycle", None)
    payload = lifecycle.status() if lifecycle else {
        "status": "STARTING", "pid": None, "ppid": None,
    }
    history = getattr(request.app.state, "history_collector", None)
    payload["collector"] = history.health_status() if history else {"status": "STARTING"}
    payload["collector_current"] = _read_node_collector_snapshot()
    rules = getattr(request.app.state, "rules_engine", None)
    payload["rules_engine"] = rules.status() if rules else {"status": "NOT_CONFIGURED"}
    repository = getattr(getattr(request.app.state, "wp", None), "repository", None)
    if repository is None:
        payload["database"] = {"status": "STARTING"}
    else:
        try:
            with repository.connect() as conn:
                conn.execute("SELECT 1").fetchone()
            payload["database"] = {"status": "OK"}
        except Exception as exc:
            payload["database"] = {"status": "ERROR", "reason": str(exc)}
    system_health = getattr(request.app.state, "system_health", None)
    if system_health is None:
        payload["browser"] = {"status": "STARTING"}
    else:
        try:
            browser = system_health.snapshot()["components"]["browser"]
            payload["browser"] = {"status": browser["state"], "reason": browser["last_error"]}
        except Exception as exc:
            payload["browser"] = {"status": "ERROR", "reason": str(exc)}
    reliability_path = Path(__file__).resolve().parents[3] / "data" / "bot" / "browser-reliability.json"
    try:
        browser_reliability = json.loads(reliability_path.read_text(encoding="utf-8"))
        updated = datetime.fromisoformat(browser_reliability["updated_at"])
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=timezone.utc)
        if (datetime.now(timezone.utc) - updated).total_seconds() > 15:
            browser_reliability["snapshot_stale"] = True
            browser_reliability["state"] = "DEGRADED"
            browser_reliability["reason"] = "Supervisor heartbeat is stale"
        payload["browser_reliability"] = browser_reliability
    except (OSError, ValueError, KeyError, TypeError):
        payload["browser_reliability"] = {"state": "UNKNOWN", "reason": "Supervisor snapshot unavailable"}
    return {"ok": True, "stability": payload}
