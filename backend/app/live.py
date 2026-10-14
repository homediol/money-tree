"""Explicit, fail-closed production activation and final live execution gate."""
from __future__ import annotations

import asyncio
import json
import uuid
from datetime import datetime, timezone
from typing import Any

from app.betting.schemas import SessionStartRequest


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class LiveActivationManager:
    MODES = {"OBSERVING", "SHADOW", "LIVE_READY", "LIVE_ACTIVE", "PAUSED", "STOPPED", "EMERGENCY_STOP", "ERROR"}

    def __init__(self, app=None):
        self.app = app
        self.mode = "OBSERVING"
        self.live_active = False
        self.explicit_live_authorization = False
        self.automatic_betting = False
        self.session: dict[str, Any] | None = None
        self.last_readiness: dict[str, Any] | None = None
        self.last_cashout_refresh: dict[str, Any] | None = None
        self.reason: str | None = None
        self._lock = asyncio.Lock()

    @property
    def repository(self):
        return getattr(getattr(self.app, "state", None), "wp", None).repository

    async def _audit(self, event: str, payload: dict[str, Any] | None = None) -> None:
        row = {"event": event, "mode": self.mode, "recorded_at": now(), **(payload or {})}
        try:
            with self.repository.connect() as conn:
                conn.execute("INSERT INTO system_audit_log(event_type,entity_id,recorded_at,payload) VALUES(?,?,?,?)",
                             (f"LIVE_{event.upper()}", (self.session or {}).get("session_id"), row["recorded_at"], json.dumps(row)))
        except Exception:
            pass
        broadcaster = getattr(getattr(self.app, "state", None), "manager", None)
        if broadcaster:
            await broadcaster.broadcast({"type": f"live:{event.lower()}", **row})

    async def production_readiness_check(self, platform_observation: dict[str, Any] | None = None,
                                        execution_configuration: dict[str, Any] | None = None) -> dict[str, Any]:
        checks: dict[str, Any] = {}
        reasons: list[str] = []
        app = self.app
        if app is None:
            reasons.append("application unavailable")
            return {"ready": False, "state": "BLOCKED", "reasons": reasons, "checks": checks}
        wp = getattr(app.state, "wp", None)
        repo = getattr(wp, "repository", None)
        try:
            with repo.connect() as conn:
                conn.execute("SELECT 1").fetchone()
            checks["database"] = "HEALTHY"
        except Exception as exc:
            checks["database"] = "ERROR"; reasons.append(f"database unavailable: {exc}")
        health = getattr(app.state, "system_health", None)
        if health:
            try:
                if platform_observation:
                    health.record_browser_observation(platform_observation)
                snap = health.refresh()
                checks["system_health"] = snap.get("state")
                if snap.get("state") not in {"HEALTHY"}:
                    reasons.extend([f"system health: {x}" for x in snap.get("can_bet", {}).get("reasons", []) or [snap.get("reason") or "unhealthy"]])
            except Exception as exc:
                checks["system_health"] = "ERROR"; reasons.append(f"health check failed: {exc}")
        settings = getattr(getattr(app.state, "betting", None), "settings", None)
        if not settings or not bool(getattr(settings, "allow_real_placement", False)):
            checks["live_executor"] = "DISABLED"; reasons.append("live placement is disabled by configuration")
        else:
            checks["live_executor"] = "ENABLED"
        betting_status = getattr(app.state, "betting", None).status() if getattr(app.state, "betting", None) else {}
        use_observation = (platform_observation is not None
                           or health is not None and not betting_status.get("automatic_enabled"))
        observation = (health.browser_observation() if use_observation and health
                       else platform_observation or {})
        browser_status = observation.get("browser_status") or betting_status.get("browser_status")
        ui_ready = observation.get("ui_ready") if use_observation else betting_status.get("last_ui_ready")
        balance = observation.get("balance") if use_observation else betting_status.get("current_balance")
        browser_ready = (browser_status in {"CONNECTED", "READY"} and bool(ui_ready) and balance is not None
                         and (not use_observation or bool(observation.get("verified"))))
        checks["betting_page"] = "HEALTHY" if browser_ready else "NOT_READY"
        if bool(getattr(settings, "allow_real_placement", False)) and not browser_ready:
            reasons.append("betting page is not connected and UI-ready")
        authentication_verified = (observation.get("verified") is True
                                  and observation.get("authentication_state") == "AUTHENTICATED")
        checks["platform_authentication"] = "VERIFIED" if authentication_verified else "UNVERIFIED"
        if not authentication_verified:
            reasons.append("authenticated Aviator betting session has not been verified")
        cashout = observation.get("cashout_configuration") or {}
        cashout_panels = observation.get("cashout_panels") or []
        both_cashout_panels_configured = (len(cashout_panels) == 2 and all(
            panel.get("configuration_valid") is True
            and panel.get("auto_bet_enabled") is False
            and panel.get("auto_tab_selected") is True
            for panel in cashout_panels
        ))
        both_cashout_acknowledged = (both_cashout_panels_configured and all(
            panel.get("platform_acknowledged") is True for panel in cashout_panels
        ))
        if both_cashout_acknowledged:
            checks["auto_cashout"] = "VERIFIED"
        elif both_cashout_panels_configured or cashout.get("configuration_valid") is True:
            checks["auto_cashout"] = "CONFIGURED_UNACKNOWLEDGED"
        else:
            checks["auto_cashout"] = "UNVERIFIED"
        if checks["auto_cashout"] != "VERIFIED":
            failures = [failure for panel in cashout_panels
                        for failure in panel.get("failures", [])]
            failures.extend(cashout.get("failures") or [])
            if "PLATFORM_CASHOUT_ACKNOWLEDGEMENT_UNVERIFIED" in failures:
                reason = "auto-cashout 2.00x is displayed and enabled but platform acknowledgement is unverified"
                if reason not in reasons:
                    reasons.append(reason)
            else:
                reasons.append("both auto-cashout panels are not freshly confirmed at 2.00x with Auto Bet OFF")
        controls = observation.get("betting_controls") or {}
        panel_states = controls.get("panels") or []
        stake_inputs = controls.get("stake_inputs") or []
        bet_buttons = controls.get("bet_buttons") or []
        controls_ready = (controls.get("snapshot_ok") is True
                          and len(panel_states) == 2 and len(stake_inputs) == 2
                          and len(bet_buttons) == 2
                          and all(panel.get("pending_bet_state") == "CLEAR"
                                  and panel.get("button_visible") is True
                                  and panel.get("stake_input_visible") is True
                                  for panel in panel_states))
        checks["betting_controls"] = "READY" if controls_ready else "UNAVAILABLE"
        if not controls_ready:
            reasons.append("both betting panels, stake fields, Bet buttons, and clear bet states are not freshly verified")
        selected_panel = (execution_configuration or {}).get("execution_panel")
        if type(selected_panel) is int and selected_panel in (0, 1):
            checks["selected_panel"] = "VERIFIED" if controls_ready else "UNAVAILABLE"
            if not controls_ready:
                reasons.append("selected execution panel is not currently verified")
        else:
            checks["selected_panel"] = "UNSELECTED"
            reasons.append("LIVE execution panel must be explicitly selected")
        selected_stake = (execution_configuration or {}).get("stake_amount")
        if type(selected_stake) is int and selected_stake > 0:
            checks["stake"] = "VERIFIED_CONFIGURATION" if controls_ready else "UNAVAILABLE"
        else:
            checks["stake"] = "UNSELECTED"
            reasons.append("LIVE stake must be explicitly selected")
        selected_currency = (execution_configuration or {}).get("currency")
        current_currency = observation.get("currency")
        if isinstance(selected_currency, str) and selected_currency and selected_currency == current_currency:
            checks["stake_currency"] = "MATCHES_VERIFIED_ACCOUNT"
        else:
            checks["stake_currency"] = "UNVERIFIED_OR_MISMATCHED"
            reasons.append("LIVE stake currency must match the verified platform account currency")
        # Read-only DOM evidence can verify visible configuration, but it does
        # not prove the platform accepted it or accepted a bet. Keep execution
        # and target identity blocked until those independent receipts exist.
        checks["platform_execution"] = "UNVERIFIED"
        reasons.append("platform acceptance, round and cashout controls are unverified")
        checks["target_identity"] = "UNVERIFIED"
        reasons.append("the next target has no verified provider round identity before execution")
        checks["cashout_monitor"] = "AVAILABLE" if getattr(app.state, "reconciliation", None) else "UNAVAILABLE"
        if checks["cashout_monitor"] == "UNAVAILABLE": reasons.append("cashout/result reconciliation unavailable")
        history = getattr(app.state, "history_collector", None)
        try:
            hs = history.status() if history else {}
            latest = hs.get("latest") or {}
            fresh = bool(hs.get("running")) and str(hs.get("status", "")).upper() in {"HEALTHY", "COLLECTING"} and latest.get("round_id") is not None
            checks["history"] = "HEALTHY" if fresh else "STALE"
            if not fresh: reasons.append("history is stale or not collecting")
        except Exception as exc:
            checks["history"] = "ERROR"; reasons.append(f"history check failed: {exc}")
        for name in ("risk", "reconciliation", "orchestrator"):
            ok = getattr(app.state, name, None) is not None
            checks[name] = "HEALTHY" if ok else "UNAVAILABLE"
            if not ok: reasons.append(f"{name} unavailable")
        rules_engine = getattr(app.state, "rules_engine", None)
        rules_status = rules_engine.status() if rules_engine is not None else {}
        checks["rules_engine"] = "READY" if rules_status.get("rules_enabled", 0) else "NOT_CONFIGURED"
        if not rules_status.get("rules_enabled", 0):
            reasons.append("Rules Engine has no enabled rules")
        decision_interface_ready = bool(
            rules_engine is not None
            and callable(getattr(rules_engine, "execution_authorized", None))
            and rules_engine.execution_authorized() is True
        )
        checks["decision_interface"] = "READY" if decision_interface_ready else "UNAVAILABLE"
        if not decision_interface_ready:
            reasons.append("rule selections are not connected to target-bound, risk-approved execution intents")
        risk = getattr(app.state, "risk", None)
        checks["emergency_stop"] = ("UNAVAILABLE" if risk is None else
                                     "LATCHED" if risk.emergency_latched else "CLEAR")
        if risk and risk.emergency_latched: reasons.append("emergency stop is latched")
        checks["risk"] = "BLOCKED" if risk and risk.emergency_latched else "HEALTHY"
        result = {"ready": not reasons, "state": "LIVE_READY" if not reasons else "BLOCKED", "reasons": list(dict.fromkeys(reasons)), "checks": checks, "checked_at": now()}
        self.last_readiness = result
        return result

    async def start(self, payload: dict[str, Any], *, _mode_authorization=None) -> dict[str, Any]:
        async with self._lock:
            from app.services.betting_mode import LIVE_MODE_AUTHORIZATION
            if _mode_authorization is not LIVE_MODE_AUTHORIZATION:
                return {"ok": False, "error": "backend_mode_authorization_required",
                        "message": "Use the authenticated backend mode review and activation flow"}
            if payload.get("confirmation") != "ENABLE LIVE BETTING":
                return {"ok": False, "error": "explicit_confirmation_required", "message": "Type ENABLE LIVE BETTING to activate live betting"}
            operator_identity = payload.get("operator_identity")
            if not isinstance(operator_identity, str) or not operator_identity.startswith("api-key:"):
                return {"ok": False, "error": "operator_authorization_required",
                        "message": "LIVE activation requires a server-authenticated operator identity"}
            if payload.get("real_funds_can_be_lost") is not True:
                return {"ok": False, "error": "real_funds_at_risk_confirmation_required",
                        "message": "Explicitly confirm that real funds can be lost."}
            if self.live_active:
                return {"ok": False, "error": "already_active", "status": self.status()}
            panel = payload.get("execution_panel")
            stake = payload.get("stake_amount")
            currency = payload.get("currency")
            if type(panel) is not int or panel not in (0, 1):
                return {"ok": False, "error": "execution_panel_required"}
            if type(stake) is not int or stake <= 0:
                return {"ok": False, "error": "positive_live_stake_required"}
            if currency not in {"BIF", "RWF"}:
                return {"ok": False, "error": "supported_live_currency_required"}
            platform_observation = payload.get("_platform_observation") or {}
            if currency != platform_observation.get("currency"):
                return {"ok": False, "error": "live_stake_currency_mismatch"}
            readiness = await self.production_readiness_check(platform_observation, {
                "execution_panel": panel, "stake_amount": stake, "currency": currency,
            })
            if not readiness["ready"]:
                await self._audit("activation_blocked", {"reasons": readiness["reasons"]})
                return {"ok": False, "error": "LIVE ACTIVATION BLOCKED", "reasons": readiness["reasons"], "readiness": readiness}
            profile = payload.get("profile", "PROFILE_A")
            req = SessionStartRequest(action="start", mode="REAL", profile=profile,
                                      starting_balance=payload.get("starting_balance"),
                                      goal_balance=payload.get("goal_balance"),
                                      session_loss_limit_enabled=payload.get("session_loss_limit_enabled"),
                                      session_loss_limit=payload.get("session_loss_limit"),
                                      win_limit_enabled=payload.get("win_limit_enabled"),
                                      win_limit=payload.get("win_limit"), label="live")
            betting = self.app.state.betting
            status = await betting.start_session(req)
            sid = f"LIVE-{uuid.uuid4().hex[:12]}"
            snapshot = {"live_session_id": sid, "configuration_version": payload.get("configuration_version", "current"),
                        "risk_version": "current", "executor_version": "current", "started_at": now(), "profile": profile,
                        "operator_identity": operator_identity, "execution_panel": panel,
                        "stake_amount": stake, "currency": currency,
                        "cashout_target": payload.get("cashout_target", 2.0),
                        "real_funds_can_be_lost": True,
                        "stop_conditions": {
                            "win_limit_enabled": bool(payload.get("win_limit_enabled")),
                            "win_limit": payload.get("win_limit"),
                            "loss_limit_enabled": bool(payload.get("session_loss_limit_enabled")),
                            "loss_limit": payload.get("session_loss_limit"),
                        }}
            self.session = {"session_id": sid, "started_at": snapshot["started_at"], "mode": "LIVE_ACTIVE", "snapshot": snapshot, "status": status}
            self.repository.save_live_session(self.session)
            self.mode = "LIVE_ACTIVE"; self.live_active = True; self.automatic_betting = True; self.reason = None
            self.explicit_live_authorization = True
            await self._audit("started", {"snapshot": snapshot})
            return {"ok": True, "status": self.status()}

    async def pause(self, reason: str = "manual_pause") -> dict[str, Any]:
        async with self._lock:
            if self.live_active:
                try: await self.app.state.betting.stop_session(reason=reason)
                except Exception: pass
            self.live_active = False; self.automatic_betting = False; self.mode = "PAUSED"; self.reason = reason
            self.explicit_live_authorization = False
            if self.session:
                self.session.update(mode="PAUSED", ended_at=now()); self.repository.update_live_session(self.session)
            await self._audit("paused", {"reason": reason})
            return self.status()

    async def stop(self, emergency: bool = False) -> dict[str, Any]:
        if emergency:
            await self.app.state.risk.emergency_stop()
        status = await self.pause("emergency_stop" if emergency else "manual_stop")
        if emergency:
            self.mode = "EMERGENCY_STOP"
            if self.session:
                self.session["mode"] = self.mode
                self.repository.update_live_session(self.session)
            status = self.status()
        return status

    async def resume(self, confirmation: str) -> dict[str, Any]:
        # A paused or crashed session must repeat the full readiness and
        # balance/profile/limits review through /start.
        return {
            "ok": False,
            "error": "fresh_live_activation_required",
            "message": "Review current platform balance and configuration, "
                       "then start a new LIVE session",
        }

    def can_execute_live_bet(self, decision: dict[str, Any], betting_status: dict[str, Any] | None = None) -> dict[str, Any]:
        blocks: list[str] = []
        if not self.live_active or self.mode != "LIVE_ACTIVE": blocks.append("live session is not active")
        mode_manager = getattr(self.app.state, "betting_mode", None) if self.app else None
        if not self.explicit_live_authorization or not mode_manager or not mode_manager.can_execute_live():
            blocks.append("backend live authorization is not active")
        if getattr(getattr(self.app.state, "risk", None), "emergency_latched", False): blocks.append("emergency stop is active")
        status = betting_status or self.app.state.betting.status()
        if status.get("mode") != "REAL" or not status.get("automatic_enabled"): blocks.append("real automatic session is not active")
        balance_stamp = status.get("last_balance_observed_at")
        try:
            balance_age = (datetime.now(timezone.utc) - datetime.fromisoformat(
                str(balance_stamp).replace("Z", "+00:00"))).total_seconds()
            if balance_age < 0 or balance_age > 20:
                blocks.append("real balance observation is stale")
        except (TypeError, ValueError):
            blocks.append("real balance observation timestamp is missing")
        if decision.get("status") != "READY_FOR_EXECUTION": blocks.append("decision is not risk approved")
        expiry = decision.get("expires_at")
        try:
            if not expiry or datetime.fromisoformat(str(expiry).replace("Z", "+00:00")) <= datetime.now(timezone.utc):
                blocks.append("decision expired or expiry missing")
        except (TypeError, ValueError):
            blocks.append("decision expiry invalid")
        decision_risk = decision.get("risk_evaluation") or decision.get("risk") or {}
        if decision_risk.get("status") != "APPROVED" or not decision_risk.get("approved"):
            blocks.append("risk approval missing")
        latest_row = (self.app.state.history_collector.status().get("latest") or {})
        latest = latest_row.get("round_id")
        if latest is None or str(decision.get("source_round_id")) != str(latest):
            blocks.append("source round mismatch or stale decision")
        try:
            latest_marker = self.repository.latest_round_marker()
            target_index = int(decision.get("target_round_index"))
            if latest_marker is None or target_index != int(latest_marker[0]) + 1:
                blocks.append("target round does not match the next canonical history index")
        except (AttributeError, TypeError, ValueError, IndexError):
            blocks.append("target round cannot be verified against canonical history")
        if not decision.get("decision_id") or not decision.get("target_round_id"):
            blocks.append("decision identity missing")
        if self.repository.execution_for(str(decision.get("decision_id")), str(decision.get("target_round_id"))): blocks.append("DUPLICATE_BLOCKED")
        try:
            unresolved = self.repository.open_executions()
            if unresolved:
                blocks.append("unresolved previous execution exists")
        except Exception:
            blocks.append("execution database unavailable")
        if not status.get("current_balance") or status.get("current_balance") < (decision_risk.get("approved_bet") or decision_risk.get("approved_bet_amount") or 0):
            blocks.append("real balance insufficient or unverified")
        settings = getattr(self.app.state.betting, "settings", None)
        if not getattr(settings, "allow_real_placement", False):
            blocks.append("real placement disabled by configuration")
        rules_engine = getattr(self.app.state, "rules_engine", None)
        if (rules_engine is None or not rules_engine.last_evaluation
                or rules_engine.last_evaluation.get("result") is not True
                or not rules_engine.last_evaluation.get("true_rule_ids")
                or not rules_engine.execution_authorized()):
            blocks.append("RULES_ENGINE_NOT_CONFIGURED")
        return {"allowed": not blocks, "reasons": list(dict.fromkeys(blocks)), "checked_at": now()}

    def status(self) -> dict[str, Any]:
        return {"mode": self.mode, "live_active": self.live_active, "automatic_betting": self.automatic_betting,
                "reason": self.reason, "session": self.session, "readiness": self.last_readiness,
                "cashout_refresh": self.last_cashout_refresh}
