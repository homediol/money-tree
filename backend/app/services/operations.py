"""Long-running operational telemetry and fail-closed incident handling."""
from __future__ import annotations

import json, os, shutil, sqlite3, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any


class OperationsManager:
    def __init__(self, app, *, retention_days: int = 90):
        self.app = app
        self.retention_days = max(7, int(retention_days))
        self.started_at = time.time()
        self.last_snapshot: dict[str, Any] | None = None
        self.incidents: list[dict[str, Any]] = []
        self.max_incidents = 500

    def _resource(self) -> dict[str, Any]:
        result = {"cpu_percent": None, "memory_percent": None, "disk_free_bytes": None}
        try:
            import psutil
            result.update(cpu_percent=psutil.cpu_percent(interval=None), memory_percent=psutil.virtual_memory().percent,
                          disk_free_bytes=psutil.disk_usage(str(Path.cwd())).free)
        except Exception as exc:
            result["error"] = f"resource metrics unavailable: {exc}"
        return result

    def snapshot(self) -> dict[str, Any]:
        health = getattr(self.app.state, "system_health", None)
        status = health.refresh() if health else {"state": "UNKNOWN", "reason": "health monitor unavailable"}
        repo = getattr(self.app.state.wp, "repository", None)
        db_bytes = None
        try: db_bytes = repo.database_path.stat().st_size
        except Exception: pass
        executions = repo.open_executions() if repo else []
        pipeline = {name: value.get("state", "UNKNOWN") for name, value in status.get("components", {}).items()}
        snap = {"recorded_at": datetime.now(timezone.utc).isoformat(), "uptime_s": round(time.time()-self.started_at, 3),
                "health": status, "resources": self._resource(), "database_bytes": db_bytes,
                "open_executions": len(executions), "pipeline": pipeline,
                "browser": getattr(self.app.state.betting, "status", lambda: {})().get("browser_status")}
        self.last_snapshot = snap
        return snap

    async def evaluate(self) -> dict[str, Any]:
        snap = self.snapshot(); reasons = []
        if snap["open_executions"]:
            unresolved = [x for x in self.app.state.wp.repository.open_executions() if x.get("status") in {"UNKNOWN", "RECONCILIATION_PENDING"}]
            if unresolved: reasons.append("unresolved executions require reconciliation")
        health = snap["health"]
        if health.get("state") in {"CRITICAL", "PAUSED"}: reasons.append(health.get("reason") or "critical health state")
        incident = None
        if reasons:
            incident = {"incident_id": f"INC-{int(time.time()*1000)}", "severity": "CRITICAL", "reasons": reasons, "recorded_at": snap["recorded_at"]}
            self.incidents.append(incident); self.incidents = self.incidents[-self.max_incidents:]
            live = getattr(self.app.state, "live", None)
            if live and live.live_active:
                await live.pause("operations_safety_pause")
            broadcaster = getattr(self.app.state, "manager", None)
            if broadcaster: await broadcaster.broadcast({"type": "operations:alert", **incident})
        return {"ok": not reasons, "snapshot": snap, "incident": incident}

    def report(self, period: str = "daily") -> dict[str, Any]:
        now = datetime.now(timezone.utc); start = {"session": now-timedelta(hours=24), "daily": now-timedelta(days=1), "weekly": now-timedelta(days=7), "monthly": now-timedelta(days=30)}.get(period, now-timedelta(days=1))
        repo = self.app.state.wp.repository
        rows = [x for x in repo.list_executions(1000) if str(x.get("created_at", "")) >= start.isoformat()]
        wins = sum(1 for x in rows if str(x.get("status", "")).upper() in {"WON", "CASHED_OUT", "RECONCILED"} and (x.get("profit_loss") or 0) > 0)
        return {"period": period, "from": start.isoformat(), "to": now.isoformat(), "executions": len(rows), "wins": wins,
                "losses": sum(1 for x in rows if (x.get("profit_loss") or 0) < 0), "unknown": sum(1 for x in rows if str(x.get("status", "")).upper() in {"UNKNOWN", "RECONCILIATION_PENDING"}),
                "incidents": list(self.incidents), "health": self.last_snapshot or self.snapshot(),
                "statement": "Observed operational data only; no estimated results."}

    def backup(self, destination: str) -> dict[str, Any]:
        src = Path(self.app.state.wp.repository.database_path); dest = Path(destination)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if dest.resolve() == src.resolve(): raise ValueError("backup destination must differ from database")
        source = sqlite3.connect(src); target = sqlite3.connect(dest)
        try: source.backup(target)
        finally: target.close(); source.close()
        with sqlite3.connect(dest) as conn: integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok": raise RuntimeError(f"backup integrity check failed: {integrity}")
        return {"path": str(dest), "bytes": dest.stat().st_size, "integrity": integrity, "created_at": datetime.now(timezone.utc).isoformat()}

    def restore(self, source: str, *, confirmation: str) -> dict[str, Any]:
        if confirmation != "RESTORE DATABASE":
            raise ValueError("explicit RESTORE DATABASE confirmation required")
        src = Path(source); dest = Path(self.app.state.wp.repository.database_path)
        if not src.exists() or src.resolve() == dest.resolve(): raise ValueError("invalid restore source")
        with sqlite3.connect(src) as conn:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if integrity != "ok": raise ValueError(f"restore integrity check failed: {integrity}")
        tmp = dest.with_suffix(dest.suffix + ".restore.tmp")
        shutil.copy2(src, tmp); os.replace(tmp, dest)
        return {"path": str(dest), "integrity": integrity, "restored_at": datetime.now(timezone.utc).isoformat()}

    def cleanup(self) -> dict[str, int]:
        cutoff = (datetime.now(timezone.utc)-timedelta(days=self.retention_days)).isoformat(); repo = self.app.state.wp.repository; deleted = {}
        with repo.connect() as conn:
            for table, column in (("system_audit_log", "recorded_at"), ("execution_events", "recorded_at")):
                cur = conn.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,)); deleted[table] = cur.rowcount
        return deleted
