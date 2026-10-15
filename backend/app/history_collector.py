"""Backend-owned lifecycle and read model for the Playwright history collector."""
from __future__ import annotations

import asyncio
import json
import math
import os
import signal
import shutil
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

from app.core.logging import get_logger

log = get_logger("HISTORY")
Broadcaster = Callable[[dict], Awaitable[None]]
ROOT = Path(__file__).resolve().parents[2]
CASHOUT_INITIALIZATION_UNKNOWN = {
    "target": "2.00", "auto_bet": "UNKNOWN", "status": "CASHOUT_UNAVAILABLE",
    "panel_1_cashout": "CASHOUT_UNAVAILABLE", "panel_2_cashout": "CASHOUT_UNAVAILABLE",
    "panels": [], "updated_at": None,
    "error_reason": "COLLECTOR_CASHOUT_INITIALIZATION_NOT_REPORTED",
}


class HistoryCollectorManager:
    """Own one Node collector subprocess; the Node side owns BrowserManager."""

    STATES = {"STARTING", "CONNECTING", "AUTH_REQUIRED", "WAITING_FOR_AVIATOR", "COLLECTING",
              "OFFLINE", "RECOVERING", "STORAGE_UNAVAILABLE", "STOPPING", "STALE", "UNKNOWN"}

    def __init__(self, data_path: Path, broadcaster: Broadcaster | None = None, round_loader=None,
                 repository=None):
        self.data_path = Path(data_path)
        self.round_loader = round_loader
        self.repository = repository
        self.status_path = ROOT / "data" / "bot" / "status.json"
        self.external_pid_path = ROOT / "data" / "bot" / "collector-supervisor.json"
        # The Node collector is a separately launched process. API lifecycle
        # hooks only observe its heartbeat; backend startup/restart must never
        # start, stop, or own its browser.
        self.external = True
        self.entrypoint = ROOT / "bot" / "roundhistory-collector.js"
        self.broadcaster = broadcaster
        self.process: asyncio.subprocess.Process | None = None
        self.state = "STOPPED"
        self.started_at: str | None = None
        self.last_error: str | None = None
        self._paused = False
        self._watch_task: asyncio.Task | None = None
        self._ensure_file()

    def _ensure_file(self) -> None:
        if self.data_path.exists():
            return
        self.data_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.data_path.with_suffix(self.data_path.suffix + ".tmp")
        tmp.write_text("[]\n", encoding="utf-8")
        os.replace(tmp, self.data_path)

    async def _emit(self, name: str, **payload) -> None:
        if self.broadcaster:
            await self.broadcaster({"type": name, **payload})

    async def start(self) -> dict:
        if self.external:
            # The stack supervisor owns the collector independently of the
            # API. Backend restarts must not terminate browser observation.
            return self.status()
        if self.process and self.process.returncode is None:
            if self._paused:
                await self.resume()
            return self.status()
        self.state = "CONNECTING"
        self.last_error = None
        self.started_at = datetime.now(timezone.utc).isoformat()
        try:
            # Uvicorn is often launched from a service/IDE without an
            # interactive shell, so nvm's node binary is not necessarily on
            # PATH. Resolve it once here instead of silently losing the
            # browser/history process at startup.
            node = os.environ.get("NODE_BINARY") or shutil.which("node")
            if not node:
                candidates = sorted(Path.home().glob(".nvm/versions/node/*/bin/node"))
                node = str(candidates[-1]) if candidates else None
            if not node:
                raise FileNotFoundError("node executable not found; set NODE_BINARY")
            self.process = await asyncio.create_subprocess_exec(
                node, str(self.entrypoint), cwd=str(ROOT / "bot"),
                stdout=sys.stdout, stderr=sys.stderr,
            )
            self._watch_task = asyncio.create_task(self._watch())
            await self._emit("history:connecting", status=self.status())
        except Exception as exc:
            self.state = "ERROR"
            self.last_error = str(exc)
            log.exception("collector start failed")
            await self._emit("history:error", error=self.last_error)
        return self.status()

    async def _watch(self) -> None:
        assert self.process is not None
        code = await self.process.wait()
        if self.state != "STOPPED":
            self.state = "ERROR" if code else "STOPPED"
            self.last_error = None if code == 0 else f"collector exited with code {code}"
            await self._emit("history:error" if code else "history:stopped", status=self.status())

    async def stop(self) -> dict:
        if self.external:
            return self.status()
        proc = self.process
        self.state = "STOPPED"
        self._paused = False
        if proc and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 8)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        self.process = None
        await self._emit("history:stopped", status=self.status())
        return self.status()

    async def pause(self) -> dict:
        if self.process and self.process.returncode is None and not self._paused:
            self.process.send_signal(signal.SIGSTOP)
            self._paused = True
            self.state = "WAITING"
            await self._emit("history:waiting", status=self.status())
        return self.status()

    async def resume(self) -> dict:
        if self.process and self.process.returncode is None and self._paused:
            self.process.send_signal(signal.SIGCONT)
            self._paused = False
            self.state = "CONNECTING"
            await self._emit("history:connecting", status=self.status())
        return self.status()

    def _node_status(self) -> dict:
        try:
            value = json.loads(self.status_path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def _external_process(self) -> tuple[bool | None, str | None]:
        """Return True/False only when process ownership is verified.

        None means the status file or OS process evidence was insufficient. In
        particular, a permission error or an unreadable /proc entry must not be
        translated into a definite STOPPED diagnosis.
        """
        try:
            value = self._node_status()
            pid = int(value.get("pid") or 0)
            heartbeat = value.get("heartbeatAt")
            if value.get("collectorRunning") is False:
                return False, heartbeat
            if value.get("collectorRunning") is not True or pid <= 1:
                return None, heartbeat
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                # The backend may run in another PID namespace. A missing PID
                # is not proof of exit while the collector heartbeat is fresh.
                return None, heartbeat
            except PermissionError:
                return None, heartbeat
            if sys.platform.startswith("linux"):
                try:
                    command = Path(f"/proc/{pid}/cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
                except FileNotFoundError:
                    return None, heartbeat
                except PermissionError:
                    return None, heartbeat
                if not command.strip():
                    return None, heartbeat
                if "roundhistory-collector.js" not in command:
                    return False, heartbeat
            return True, heartbeat
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None, None

    def health_status(self, *, persist: bool = True) -> dict:
        """Cheap collector health for frequent readiness updates.

        The full status endpoint also parses the complete history file. That
        work is unnecessary when readiness only needs the process and sidecar
        health state.
        """
        node = self._node_status()
        running = bool(self.process and self.process.returncode is None)
        process_status = "RUNNING" if running else "STOPPED"
        if self.external:
            process_evidence, _external_heartbeat = self._external_process()
            running = process_evidence
            process_status = ("RUNNING" if process_evidence is True else
                              "STOPPED" if process_evidence is False else "UNKNOWN")
        state = self.state
        status_is_current = False
        try:
            status_is_current = not self.started_at or self.status_path.stat().st_mtime >= datetime.fromisoformat(self.started_at).timestamp()
        except OSError:
            pass
        heartbeat = node.get("heartbeatAt")
        heartbeat_age = None
        if heartbeat:
            try:
                heartbeat_age = max(0.0, time.time() - datetime.fromisoformat(str(heartbeat).replace("Z", "+00:00")).timestamp())
            except (TypeError, ValueError, OverflowError):
                heartbeat_age = None
        heartbeat_stale_after = float(os.environ.get("COLLECTOR_HEARTBEAT_STALE_SECONDS", "20"))
        heartbeat_state = "UNKNOWN" if heartbeat_age is None else "STALE" if heartbeat_age > heartbeat_stale_after else "FRESH"
        # Browser topology and betting-control values are point-in-time
        # observations. A stopped collector or dead heartbeat cannot vouch
        # for the browser last recorded in its sidecar.
        observation_current = process_status == "RUNNING" and heartbeat_state == "FRESH"
        game_phase = "UNKNOWN"
        game_phase_observed_at = None
        game_phase_evidence = {}
        game_phase_age = None
        deadline_evidence = {"status": "UNKNOWN", "deadline_at": None,
                             "reason": "EXPLICIT_BETTING_DEADLINE_NOT_OBSERVED"}
        if observation_current:
            phase_value = str(node.get("gamePhase") or "UNKNOWN").upper()
            phase_time = node.get("gamePhaseObservedAt")
            try:
                parsed_phase_time = datetime.fromisoformat(str(phase_time).replace("Z", "+00:00"))
                if parsed_phase_time.tzinfo is None:
                    raise ValueError("timezone required")
                game_phase_age = (datetime.now(timezone.utc) - parsed_phase_time.astimezone(timezone.utc)).total_seconds()
            except (TypeError, ValueError, OverflowError):
                game_phase_age = None
            if phase_value in {"BETTING_OPEN", "BETTING_CLOSED", "UNKNOWN"} and game_phase_age is not None and -30 <= game_phase_age <= 15:
                game_phase = phase_value
                game_phase_observed_at = phase_time
                game_phase_evidence = node.get("gamePhaseEvidence") if isinstance(node.get("gamePhaseEvidence"), dict) else {}
                raw_deadline = game_phase_evidence.get("deadline_at")
                raw_countdown = game_phase_evidence.get("countdown_seconds")
                try:
                    parsed_deadline = datetime.fromisoformat(str(raw_deadline).replace("Z", "+00:00"))
                    if parsed_deadline.tzinfo is None:
                        raise ValueError("timezone required")
                    remaining = (parsed_deadline.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds()
                except (TypeError, ValueError, OverflowError):
                    remaining = None
                if (game_phase == "BETTING_OPEN" and isinstance(raw_countdown, (int, float))
                        and not isinstance(raw_countdown, bool) and remaining is not None
                        and -2 <= remaining <= 300):
                    deadline_evidence = {"status": "VERIFIED", "deadline_at": parsed_deadline.astimezone(timezone.utc).isoformat(),
                        "remaining_seconds": round(max(0.0, remaining), 3), "observed_at": phase_time,
                        "source": "AVIATOR_EXPLICIT_BETTING_PHASE_DOM"}
                else:
                    deadline_evidence = {"status": "UNKNOWN", "deadline_at": None,
                        "reason": "EXPLICIT_BETTING_DEADLINE_NOT_OBSERVED"}
        panel_observations = []
        if observation_current:
            now = datetime.now(timezone.utc)
            raw_panels = node.get("panelObservations") or []
            by_panel = {str(row.get("panel_id")): dict(row) for row in raw_panels
                        if isinstance(row, dict) and row.get("panel_id") in {"PANEL_1", "PANEL_2"}}
            for panel_id in ("PANEL_1", "PANEL_2"):
                row = by_panel.get(panel_id)
                if row:
                    try:
                        stamp = datetime.fromisoformat(str(row.get("observed_at") or "").replace("Z", "+00:00"))
                        if stamp.tzinfo is None:
                            raise ValueError("timezone required")
                        age = (now - stamp.astimezone(timezone.utc)).total_seconds()
                    except (TypeError, ValueError, OverflowError):
                        age = None
                    if age is None or age < -30 or age > 15:
                        row["state"] = "UNKNOWN"
                        row["pending_bet_state"] = "UNKNOWN"
                        row["confidence"] = 0.0
                        evidence = row.get("evidence") if isinstance(row.get("evidence"), dict) else {}
                        row["evidence"] = {**evidence, "reason": "STALE_OR_INVALID_PANEL_OBSERVATION"}
                    else:
                        row["age_seconds"] = round(max(0.0, age), 3)
                else:
                    row = {"panel_id": panel_id, "slot": int(panel_id[-1]) - 1,
                           "state": "UNKNOWN", "button_text": None, "displayed_stake": None,
                           "currency": None, "cashout_amount": None, "observed_at": None,
                           "source": "COLLECTOR_PANEL_STATE_OBSERVER", "evidence": {"reason": "NO_OBSERVATION"},
                           "confidence": 0.0, "frame_identity": None, "pending_bet_state": "UNKNOWN"}
                panel_observations.append(row)
        browser_pages = node.get("browser_pages") or {}
        cashout_initialization = node.get("cashout_initialization") or {
            **CASHOUT_INITIALIZATION_UNKNOWN,
        }
        bet_amount_initialization = node.get("bet_amount_initialization") or {
            "status": "STAKE_UNAVAILABLE", "execution_panel": None, "requested_stake": None,
            "requested_stake_currency": None, "platform_currency": None,
            "observed_value": None, "updated_at": None,
            "error_reason": "COLLECTOR_STAKE_INITIALIZATION_NOT_REPORTED",
        }
        betting_ui_configuration = node.get("betting_ui_configuration") or {
            "status": "UNKNOWN", "updated_at": None,
            "error_reason": "COLLECTOR_BETTING_UI_CONFIGURATION_NOT_REPORTED",
        }
        authentication_state = node.get("authenticationState")
        if authentication_state not in {"AUTHENTICATED", "AUTH_REQUIRED", "UNKNOWN"}:
            authentication_state = "UNKNOWN"
        if not observation_current:
            browser_pages = {
                "browser_count": None, "context_count": None, "page_count": None,
                "aviator_page_count": None, "aviator_target_id": None,
                "aviator_page_state": "UNKNOWN", "game_frame_count": None,
                "game_frame_state": "UNKNOWN", "page_role": "SHARED_AVIATOR",
                "history_target_id": None, "betting_target_id": None,
                "topology_verified": False,
                "error_reason": "COLLECTOR_OBSERVATION_NOT_CURRENT",
            }
            cashout_initialization = {
                **CASHOUT_INITIALIZATION_UNKNOWN,
                "error_reason": "COLLECTOR_OBSERVATION_NOT_CURRENT",
            }
            bet_amount_initialization = {
                "status": "STAKE_UNAVAILABLE", "execution_panel": None, "requested_stake": None,
                "requested_stake_currency": None, "platform_currency": None,
                "observed_value": None, "updated_at": None,
                "error_reason": "COLLECTOR_OBSERVATION_NOT_CURRENT",
            }
            betting_ui_configuration = {
                "status": "UNKNOWN", "updated_at": None,
                "error_reason": "COLLECTOR_OBSERVATION_NOT_CURRENT",
            }
            authentication_state = "UNKNOWN"
        if process_status == "STOPPED":
            state = ("STOPPED" if node.get("collectorRunning") is False or heartbeat_state != "STALE"
                     else "STALE")
        elif process_status == "UNKNOWN":
            if heartbeat_state == "FRESH" and status_is_current:
                candidate = str(node.get("state") or node.get("health") or "UNKNOWN").upper()
                if candidate == "HEALTHY":
                    candidate = "COLLECTING"
                state = candidate if candidate in self.STATES else "UNKNOWN"
            else:
                state = "STALE" if heartbeat_state == "STALE" else "UNKNOWN"
        elif running and heartbeat_state == "FRESH" and status_is_current:
            candidate = str(node.get("state") or node.get("health") or "UNKNOWN").upper()
            if candidate in {"HEALTHY", "WAITING", "WAITING_FOR_NETWORK", "DISCONNECTED", "ERROR", "STOPPED"}:
                candidate = {"HEALTHY": "COLLECTING", "WAITING": "WAITING_FOR_AVIATOR",
                             "WAITING_FOR_NETWORK": "OFFLINE", "DISCONNECTED": "RECOVERING",
                             "ERROR": "RECOVERING", "STOPPED": "STOPPING"}.get(candidate, candidate)
            state = candidate if candidate in self.STATES else "UNKNOWN"
        elif heartbeat_age is not None and heartbeat_age > heartbeat_stale_after:
            state = "STALE"
        elif heartbeat_state == "UNKNOWN":
            state = "UNKNOWN"
        snapshot = {
            "status": state, "running": running, "collector_process_status": process_status,
            "process_verified": process_status != "UNKNOWN", "paused": self._paused,
            "started_at": self.started_at, "last_error": self.last_error,
            "auth_required": bool(node.get("authRequired", False)),
            "auth_required_reason": node.get("authRequiredReason"),
            "logged_in": node.get("loggedIn") if observation_current and isinstance(node.get("loggedIn"), bool) else None,
            "authentication_state": authentication_state,
            "login_status": ("AUTH_REQUIRED" if node.get("authRequired") else
                "VERIFIED" if authentication_state == "AUTHENTICATED" else "UNKNOWN"),
            "last_update": node.get("lastRoundTime"),
            "browser_connected": observation_current and node.get("browserConnected") is True,
            "site_status": node.get("siteStatus", "UNKNOWN") if observation_current else "UNKNOWN",
            "frame_connected": observation_current and node.get("frameConnected") is True,
            "recovery_count": int(node.get("recoveryCount", 0) or 0),
            "last_observed_round": node.get("lastObservedRoundId"),
            "last_observed_at": node.get("lastObservedAt"),
            "last_persisted_round": node.get("lastRoundId"),
            "last_persisted_at": node.get("lastRoundTime"),
            "heartbeat_at": heartbeat,
            "heartbeat_age_seconds": heartbeat_age,
            "heartbeat_state": heartbeat_state,
            "network": node.get("network", {"status": "UNKNOWN"}),
            "backend": node.get("backend", {"status": "UNKNOWN"}),
            "storage": node.get("storage", {"status": "UNKNOWN"}),
            "cashout_initialization": cashout_initialization,
            "bet_amount_initialization": bet_amount_initialization,
            "betting_ui_configuration": betting_ui_configuration,
            "panel_observations": panel_observations,
            "betting_window": {"state": game_phase, "observed_at": game_phase_observed_at,
                               "evidence": game_phase_evidence},
            "target_deadline_evidence": deadline_evidence,
            "authentication_state": authentication_state,
            "browser_pages": browser_pages,
            "backlog_size": int((node.get("storage") or {}).get("backlog_size", 0) or 0),
            "last_recovery": node.get("lastRecovery"),
            "last_error": node.get("lastError") or self.last_error,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "data_source": "collector_observation",
        }
        if process_status == "STOPPED":
            snapshot["reason"] = node.get("lastError") or "history collector process is stopped"
        elif heartbeat_state == "STALE":
            snapshot["reason"] = "collector process exists but its heartbeat is stale"
        elif heartbeat_state == "UNKNOWN":
            snapshot["reason"] = "collector heartbeat is unavailable; live collection cannot be verified"
        elif state not in {"COLLECTING", "HEALTHY"}:
            snapshot["reason"] = (node.get("lastError") or state.lower().replace("_", " "))
        else:
            snapshot["reason"] = None
        snapshot["process_reason"] = (
            "PID could not be verified from this backend process namespace; collector heartbeat is fresh"
            if process_status == "UNKNOWN" and heartbeat_state == "FRESH" else
            "OS process ownership could not be verified" if process_status == "UNKNOWN" else None
        )
        if self.repository is not None and persist:
            try:
                self.repository.save_application_state("collector_status", snapshot, snapshot["observed_at"])
                # Return the exact persisted snapshot used by dashboard clients.
                return self.repository.load_application_state("collector_status") or snapshot
            except Exception:
                snapshot["persistence_state"] = "UNAVAILABLE"
        return snapshot

    def status(self) -> dict:
        health = self.health_status()
        if self.repository is not None and self.repository.database_url:
            summary = self.repository.round_history_status()
            count, latest, previous = summary["count"], summary["latest"], summary["previous"]
        else:
            rows = self.rows()
            count = len(rows)
            latest = rows[-1] if rows else None
            previous = rows[-2] if len(rows) > 1 else None
        return {
            **health,
            "count": count, "latest": latest, "previous": previous,
            "last_update": latest.get("timestamp") if latest else None,
        }

    def rows(self) -> list[dict]:
        if self.round_loader is not None:
            return self.round_loader()
        try:
            payload = json.loads(self.data_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        if isinstance(payload, dict):
            payload = payload.get("rounds", payload.get("history", []))
        if not isinstance(payload, list):
            return []
        result, seen = [], set()
        for pos, raw in enumerate(payload):
            if not isinstance(raw, dict):
                raw = {"multiplier": raw}
            try:
                multiplier = float(str(raw.get("multiplier", "")).lower().replace("x", "").strip())
            except (TypeError, ValueError):
                continue
            if not math.isfinite(multiplier) or multiplier <= 0:
                continue
            round_id = str(raw.get("round_id", raw.get("round_index", raw.get("id", pos + 1))))
            if round_id in seen:
                continue
            seen.add(round_id)
            result.append({
                "round_id": round_id,
                "round_index": int(raw.get("round_index", pos + 1)),
                "timestamp": raw.get("timestamp"),
                "multiplier": round(multiplier, 4),
            })
        return sorted(result, key=lambda row: row["round_index"])

    def stats(self) -> dict:
        values = [r["multiplier"] for r in self.rows()]
        return {
            "total": len(values),
            "min": min(values) if values else None,
            "max": max(values) if values else None,
            "mean": statistics.fmean(values) if values else None,
            "median": statistics.median(values) if values else None,
            "threshold_counts": {
                "below_1_5x": sum(v < 1.5 for v in values),
                "at_least_2x": sum(v >= 2 for v in values),
                "at_least_5x": sum(v >= 5 for v in values),
                "at_least_10x": sum(v >= 10 for v in values),
            },
        }
