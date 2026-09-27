"""Idempotent, fail-closed disaster recovery coordinator."""
from __future__ import annotations

import hashlib, json, os, shutil, sqlite3, tempfile, time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class DisasterRecoveryManager:
    def __init__(self, app, operations=None):
        self.app = app
        self.operations = operations
        self.mode = "SAFE_MODE"
        self.last_recovery: dict[str, Any] | None = None
        self.incidents: list[dict[str, Any]] = []

    @property
    def repo(self): return self.app.state.wp.repository

    def _checksum(self, path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""): h.update(chunk)
        return h.hexdigest()

    def integrity_check(self) -> dict[str, Any]:
        try:
            with self.repo.connect() as conn:
                if getattr(self.repo, "database_url", None):
                    conn.execute("SELECT 1").fetchone()
                    result = "ok"
                else:
                    result = conn.execute("PRAGMA integrity_check").fetchone()[0]
            return {"ok": result == "ok", "integrity": result, "checked_at": datetime.now(timezone.utc).isoformat()}
        except Exception as exc:
            return {"ok": False, "integrity": "UNKNOWN", "error": str(exc), "checked_at": datetime.now(timezone.utc).isoformat()}

    def backup_verified(self, destination: str) -> dict[str, Any]:
        if not self.operations: raise RuntimeError("operations service unavailable")
        result = self.operations.backup(destination)
        result["sha256"] = self._checksum(Path(destination))
        result["verified_copy"] = self.test_copy_restore(destination)
        return result

    def test_copy_restore(self, source: str) -> dict[str, Any]:
        src = Path(source)
        if not src.exists(): raise FileNotFoundError(source)
        with tempfile.TemporaryDirectory(prefix="winner-restore-") as tmp:
            copy = Path(tmp) / "restore.sqlite"; shutil.copy2(src, copy)
            with sqlite3.connect(copy) as conn: check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        return {"ok": check == "ok", "integrity": check, "source_sha256": self._checksum(src)}

    async def recover(self, reason: str = "startup") -> dict[str, Any]:
        self.mode = "SAFE_MODE"
        live = getattr(self.app.state, "live", None)
        if live and live.live_active:
            await live.pause(f"recovery:{reason}")
        integrity = self.integrity_check()
        open_before = self.repo.open_executions()
        reconciled = 0
        try:
            known = {str(x.get("round_id")): x for x in self.app.state.history_collector.rows()}
            for execution in open_before:
                row = known.get(str(execution.get("target_round_id")))
                if row and getattr(self.app.state, "reconciliation", None):
                    await self.app.state.reconciliation.reconcile_round(row); reconciled += 1
        except Exception as exc:
            integrity["reconciliation_error"] = str(exc)
        open_after = self.repo.open_executions()
        health = getattr(self.app.state, "system_health", None)
        health_snapshot = health.refresh() if health else {"state": "UNKNOWN"}
        history_status = getattr(self.app.state, "history_collector", None).status() if getattr(self.app.state, "history_collector", None) else {}
        history_ready = bool(history_status.get("running")) and str(history_status.get("status", "")).upper() in {"HEALTHY", "COLLECTING"}
        safe = bool(integrity.get("ok")) and history_ready and not any(str(x.get("status")).upper() in {"UNKNOWN", "RECONCILIATION_PENDING"} for x in open_after)
        self.mode = "READY" if safe and health_snapshot.get("state") == "HEALTHY" else "SAFE_MODE"
        self.last_recovery = {"ok": safe, "mode": self.mode, "reason": reason, "integrity": integrity,
                              "open_before": len(open_before), "reconciled": reconciled, "open_after": len(open_after),
                              "health": health_snapshot, "completed_at": datetime.now(timezone.utc).isoformat()}
        if not safe: self.incidents.append({"type": "RECOVERY_BLOCKED", "reason": reason, "recorded_at": self.last_recovery["completed_at"]})
        return self.last_recovery

    def atomic_write(self, path: str, value: Any) -> dict[str, Any]:
        target = Path(path); target.parent.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                if isinstance(value, (dict, list)): json.dump(value, fh, ensure_ascii=False, indent=2)
                else: fh.write(str(value))
                fh.flush(); os.fsync(fh.fileno())
            os.replace(temp, target)
        finally:
            if os.path.exists(temp): os.unlink(temp)
        return {"ok": True, "path": str(target), "sha256": self._checksum(target)}

    def status(self) -> dict[str, Any]:
        return {"mode": self.mode, "last_recovery": self.last_recovery, "incidents": self.incidents[-100:],
                "open_executions": len(self.repo.open_executions())}
