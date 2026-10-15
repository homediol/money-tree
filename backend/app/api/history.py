import asyncio
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse

from app.api.round_distribution import _require_https_or_loopback
from app.history_admin import (HistoryAdminError, clear_round_history, export_manifest,
                               export_round_history, history_admin_status, verify_round_export)

router = APIRouter(prefix="/api/history", tags=["history"])
_CLEAR_LOCK = asyncio.Lock()
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_MAINTENANCE_PATH = _PROJECT_ROOT / "data" / "bot" / "history-maintenance.json"
_MAINTENANCE_ACK_PATH = _PROJECT_ROOT / "data" / "bot" / "history-maintenance-ack.json"


def _history_admin_error(error: HistoryAdminError):
    raise HTTPException(error.status_code, detail={"error": error.code, "message": str(error), **error.details}) from error


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json_atomic(path: Path, payload: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    temp.write_text(json.dumps(payload, separators=(",", ":")), encoding="utf-8")
    os.replace(temp, path)


async def _wait_for_collector_ack(operation_id: str, wanted: set[str], timeout: float = 25.0):
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        ack = await asyncio.to_thread(_read_json, _MAINTENANCE_ACK_PATH)
        if ack and ack.get("operation_id") == operation_id and ack.get("status") in wanted:
            return ack
        if ack and ack.get("operation_id") == operation_id and ack.get("status") in {"EXPIRED", "FAILED"}:
            return ack
        await asyncio.sleep(0.25)
    return None


def _collector_runtime_evidence(request: Request) -> dict:
    manager = request.app.state.history_collector
    health = manager.health_status(persist=False)
    node = _read_json(manager.status_path) or {}
    storage = node.get("storage") or {}
    delivery = storage.get("round_delivery") or {}
    return {
        **health,
        "pid": node.get("pid"),
        "pending_queue_count": int(storage.get("backlog_size") or 0) + int(delivery.get("backlog_size") or 0),
        "round_delivery_busy": delivery.get("worker_busy") is True,
    }


async def _resume_collector(operation_id: str, *, reset_history_epoch: bool = False,
                            history_epoch: int | None = None):
    control = _read_json(_MAINTENANCE_PATH) or {}
    if control.get("operation_id") != operation_id:
        return None
    control.update({"status": "RESUME_REQUESTED", "reset_history_epoch": reset_history_epoch,
                    "history_epoch": history_epoch, "resumed_requested_at": datetime.now(timezone.utc).isoformat()})
    await asyncio.to_thread(_write_json_atomic, _MAINTENANCE_PATH, control)
    return await _wait_for_collector_ack(operation_id, {"RESUMED"}, timeout=12.0)


@router.get("")
def history(request: Request, limit: int = 250):
    limit = min(max(limit, 1), 5000)
    repository = request.app.state.wp.repository
    if repository.database_url:
        # The collector can continue writing after AppState's startup
        # snapshot. Read PostgreSQL directly so this page stays current.
        rows = repository.load_recent_rounds(limit)
        count = repository.round_history_status()["count"]
    else:
        all_rows = request.app.state.history_collector.rows()
        count = len(all_rows)
        rows = all_rows[-limit:]
    return {
        "count": count,
        "returned": len(rows),
        "rounds": rows,
    }


@router.get("/status")
def collector_status(request: Request):
    return request.app.state.history_collector.status()


@router.get("/latest")
def latest(request: Request):
    rows = request.app.state.history_collector.rows()
    return {"latest": rows[-1] if rows else None, "previous": rows[-2] if len(rows) > 1 else None}


@router.get("/recent")
def recent(request: Request, limit: int = 25):
    rows = request.app.state.history_collector.rows()
    limit = min(max(limit, 1), 5000)
    return {"count": len(rows), "returned": min(limit, len(rows)), "rounds": rows[-limit:]}


@router.get("/stats")
def stats(request: Request):
    return request.app.state.history_collector.stats()


@router.get("/admin-status")
def history_admin_dashboard_status(request: Request):
    _require_https_or_loopback(request)
    repository = request.app.state.wp.repository
    collector = _collector_runtime_evidence(request)
    try:
        return history_admin_status(repository, request.app.state.settings.data_path, collector_status=collector)
    except Exception as exc:
        raise HTTPException(503, detail={"error": "HISTORY_ADMIN_STATUS_UNAVAILABLE"}) from exc


@router.post("/exports")
def create_history_export(request: Request, body: dict):
    _require_https_or_loopback(request)
    try:
        return export_round_history(request.app.state.wp.repository,
                                    request.app.state.settings.data_path, body.get("format", "json"))
    except HistoryAdminError as exc:
        _history_admin_error(exc)


@router.get("/exports/{export_id}")
def read_history_export_manifest(request: Request, export_id: str):
    _require_https_or_loopback(request)
    try:
        return export_manifest(request.app.state.wp.repository, export_id)
    except HistoryAdminError as exc:
        _history_admin_error(exc)


@router.post("/exports/{export_id}/verify")
def verify_history_export(request: Request, export_id: str):
    _require_https_or_loopback(request)
    try:
        return verify_round_export(request.app.state.wp.repository,
                                  request.app.state.settings.data_path, export_id)
    except HistoryAdminError as exc:
        _history_admin_error(exc)


@router.get("/exports/{export_id}/download")
def download_history_export(request: Request, export_id: str):
    _require_https_or_loopback(request)
    try:
        manifest = export_manifest(request.app.state.wp.repository, export_id)
        path = (Path(request.app.state.settings.data_path).resolve().parent / "history_exports"
                / f"round-history-{export_id}.{manifest['format']}")
        if not path.is_file():
            raise HistoryAdminError("EXPORT_ARTIFACT_MISSING", "export artifact is not accessible", status_code=404)
        return FileResponse(path, filename=path.name,
                            media_type="application/json" if manifest["format"] == "json" else "text/csv")
    except HistoryAdminError as exc:
        _history_admin_error(exc)


@router.post("/clear")
async def clear_history(request: Request, body: dict):
    _require_https_or_loopback(request)
    if _CLEAR_LOCK.locked():
        raise HTTPException(409, detail={"error": "CLEAR_ALREADY_IN_PROGRESS"})
    async with _CLEAR_LOCK:
        mode = getattr(request.app.state.betting_mode, "mode", "UNKNOWN")
        if mode == "LIVE_REAL" or getattr(request.app.state.live, "live_active", False):
            raise HTTPException(409, detail={"error": "LIVE_SESSION_MUST_BE_STOPPED"})
        export_id = str(body.get("export_id") or "")
        try:
            export_check = await asyncio.to_thread(verify_round_export,
                request.app.state.wp.repository, request.app.state.settings.data_path, export_id)
            if not export_check.get("ok"):
                raise HistoryAdminError("VERIFIED_EXPORT_REQUIRED", "export artifact did not pass a fresh checksum/count/structure verification")
            collector = _collector_runtime_evidence(request)
            if (collector.get("collector_process_status") != "RUNNING"
                    or collector.get("heartbeat_state") != "FRESH"
                    or not isinstance(collector.get("pid"), int)):
                raise HistoryAdminError("COLLECTOR_BARRIER_UNAVAILABLE", "a fresh running collector process is required for the write barrier")
            operation_id = str(uuid.uuid4())
            control = {
                "operation_id": operation_id, "status": "PAUSE_REQUESTED",
                "requested_at": datetime.now(timezone.utc).isoformat(),
                "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(),
                "collector_pid": collector["pid"],
            }
            await asyncio.to_thread(_write_json_atomic, _MAINTENANCE_PATH, control)
            ack = await _wait_for_collector_ack(operation_id, {"PAUSED"})
            if not ack or ack.get("status") != "PAUSED" or ack.get("pid") != collector.get("pid"):
                raise HistoryAdminError("COLLECTOR_BARRIER_NOT_ACKNOWLEDGED", "collector did not confirm a drained write barrier",
                                        details={"collector_pid": collector.get("pid"), "acknowledgement": ack})
            if int(ack.get("pending_rounds") or 0) != 0:
                raise HistoryAdminError("COLLECTOR_QUEUE_NOT_DRAINED", "collector still has queued observations",
                                        details={"pending_rounds": ack.get("pending_rounds")})
            if _collector_runtime_evidence(request).get("pending_queue_count", 0) != 0:
                raise HistoryAdminError("COLLECTOR_QUEUE_NOT_DRAINED", "collector's durable observation outbox is not empty")
            # Re-read the artifact after the writer barrier, immediately before
            # the transactional delete, so a stale/tampered file cannot reuse
            # an earlier dashboard verification.
            export_check = await asyncio.to_thread(verify_round_export,
                request.app.state.wp.repository, request.app.state.settings.data_path, export_id)
            if not export_check.get("ok"):
                raise HistoryAdminError("VERIFIED_EXPORT_REQUIRED", "export failed its final checksum/count/structure verification")
            clear_result = await asyncio.to_thread(clear_round_history,
                request.app.state.wp.repository,
                export_id=export_id,
                expected_count=body.get("record_count"),
                confirmation_count=body.get("confirmation_count"),
                final_confirmation=body.get("final_confirmation"),
                actor=collector.get("pid"),
            )
            distribution = await asyncio.to_thread(request.app.state.wp.repository.round_distribution_snapshot)
            await request.app.state.round_distribution.reset_history_epoch(
                distribution["stream_id"], clear_result["history_epoch"])
            try:
                await asyncio.to_thread(request.app.state.wp.reload)
                request.app.state.rules_engine.reset_history_epoch(clear_result["history_epoch"])
            except Exception as exc:
                clear_result["rules_engine_reset"] = "RECOVERY_REQUIRED"
                clear_result["rules_engine_reset_error"] = type(exc).__name__
            resume_ack = await _resume_collector(operation_id, reset_history_epoch=True,
                                                 history_epoch=clear_result["history_epoch"])
            clear_result["collector_resume"] = "VERIFIED" if resume_ack and resume_ack.get("status") == "RESUMED" else "UNVERIFIED"
            if clear_result["collector_resume"] != "VERIFIED":
                raise HTTPException(503, detail={"error": "COLLECTOR_RESUME_UNVERIFIED", "clear_result": clear_result})
            return clear_result
        except HistoryAdminError as exc:
            if "operation_id" in locals():
                await _resume_collector(operation_id)
            _history_admin_error(exc)
        except HTTPException:
            raise
        except Exception as exc:
            if "operation_id" in locals():
                await _resume_collector(operation_id)
            raise HTTPException(503, detail={"error": "HISTORY_CLEAR_FAILED", "reason": type(exc).__name__}) from exc


@router.post("/start")
async def start(request: Request):
    return await request.app.state.history_collector.start()


@router.post("/stop")
async def stop(request: Request):
    if request.app.state.history_collector.external:
        raise HTTPException(status_code=409, detail="collector_is_supervisor_managed")
    return await request.app.state.history_collector.stop()
