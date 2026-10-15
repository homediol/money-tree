"""Explicit, fail-closed production activation and final live execution gate."""
from __future__ import annotations

import asyncio
import json
import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any

from app.betting.schemas import SessionStartRequest

MANUAL_SETUP_READBACK_MAX_AGE_SECONDS = 180


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _whole_bif_stake(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", "")
        if not re.fullmatch(r"\d+(?:\.0+)?", value):
            return None
        try:
            value = float(value)
        except ValueError:
            return None
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    if float(value) <= 0 or not float(value).is_integer():
        return None
    return int(value)


def _bet_button_bif_stake(value: Any) -> int | None:
    if not isinstance(value, str):
        return None
    match = re.fullmatch(r"\s*Bet\s+([\d,]+)(?:\.0+)?\s+BIF\s*", value, re.IGNORECASE)
    return _whole_bif_stake(match.group(1)) if match else None


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
        self.manual_setup_readbacks: dict[str, Any] = {}
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
        placement_configured = bool(settings and getattr(settings, "allow_real_placement", False))
        checks["execution_configuration"] = "READY" if placement_configured else "BLOCKED"
        if not settings:
            checks["live_executor"] = "UNAVAILABLE"
            reasons.append("live execution settings are unavailable")
        elif not placement_configured:
            checks["live_executor"] = "BLOCKED_CONFIGURATION"
            reasons.append("live placement permission is not enabled in backend configuration")
        else:
            checks["live_executor"] = "ENABLED"
        betting_status = getattr(app.state, "betting", None).status() if getattr(app.state, "betting", None) else {}
        use_observation = (platform_observation is not None
                           or health is not None and not betting_status.get("automatic_enabled"))
        observation = (health.browser_observation() if use_observation and health
                       else platform_observation or {})
        observation = observation if isinstance(observation, dict) else {}
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
        account_identity_verified = bool(
            authentication_verified and observation.get("currency_verified") is True
            and observation.get("provider_identity") and observation.get("platform_session_identity")
            and observation.get("fresh") is True
        )
        checks["account_readiness"] = "VERIFIED" if account_identity_verified else "UNVERIFIED"
        if not account_identity_verified:
            reasons.append("fresh account balance, currency, and platform session identity are not all verified")
        cashout = observation.get("cashout_configuration")
        cashout = cashout if isinstance(cashout, dict) else {}
        cashout_panels = observation.get("cashout_panels")
        cashout_panels = cashout_panels if isinstance(cashout_panels, list) else []
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
        controls = observation.get("betting_controls")
        controls = controls if isinstance(controls, dict) else {}
        betting_window = controls.get("betting_window")
        betting_window = betting_window if isinstance(betting_window, dict) else {"state": "UNKNOWN"}
        checks["betting_window"] = str(betting_window.get("state") or "UNKNOWN").upper()
        checks["game_phase"] = str(controls.get("round_phase") or "UNKNOWN").upper()
        if checks["betting_window"] == "UNKNOWN" and checks["game_phase"] == "UNKNOWN":
            reasons.append("shared Aviator betting window and game phase are UNKNOWN")
        deadline_evidence = controls.get("deadline_evidence")
        deadline_evidence = deadline_evidence if isinstance(deadline_evidence, dict) else {"status": "UNKNOWN"}
        checks["target_deadline"] = ("VERIFIED" if deadline_evidence.get("status") == "VERIFIED"
                                     else "UNKNOWN")
        if checks["target_deadline"] != "VERIFIED":
            reasons.append("target betting deadline has no explicit live countdown evidence")
        panel_states = controls.get("panels")
        panel_states = panel_states if isinstance(panel_states, list) else []
        stake_inputs = controls.get("stake_inputs")
        stake_inputs = stake_inputs if isinstance(stake_inputs, list) else []
        bet_buttons = controls.get("bet_buttons")
        bet_buttons = bet_buttons if isinstance(bet_buttons, list) else []
        def panel_flag(panel: dict[str, Any], name: str) -> bool:
            evidence = panel.get("evidence")
            evidence = evidence if isinstance(evidence, dict) else {}
            return panel.get(name) is True or evidence.get(name) is True

        controls_ready = (controls.get("snapshot_ok") is True
                          and len(panel_states) == 2 and len(stake_inputs) == 2
                          and len(bet_buttons) == 2
                          and all(panel.get("pending_bet_state") == "CLEAR"
                                  and panel_flag(panel, "button_visible")
                                  and panel_flag(panel, "stake_input_visible")
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

        # A configured stake is not enough: compare it with the value and Bet
        # action currently shown by the explicitly selected panel. This is a
        # read-only check; LIVE review must never rewrite the user's stake.
        selected_panel_row = None
        selected_input = None
        selected_button = None
        if type(selected_panel) is int and selected_panel in (0, 1):
            expected_panel_id = f"PANEL_{selected_panel + 1}"
            selected_panel_row = next((row for row in panel_states
                if (type(row.get("slot")) is int and row.get("slot") == selected_panel)
                or row.get("panel_id") == expected_panel_id), None)
            selected_input = next((row for row in stake_inputs
                if type(row.get("slot")) is int and row.get("slot") == selected_panel), None)
            selected_button = next((row for row in bet_buttons
                if (type(row.get("slot")) is int and row.get("slot") == selected_panel)
                or (type(row.get("associatedPanel")) is int
                    and row.get("associatedPanel") == selected_panel)), None)

        panel_stake = _whole_bif_stake((selected_panel_row or {}).get("displayed_stake"))
        input_stake = _whole_bif_stake((selected_input or {}).get("value"))
        button_text = ((selected_button or {}).get("text")
                       or (selected_panel_row or {}).get("button_text"))
        button_stake = _bet_button_bif_stake(button_text)
        readback_evidence_present = any(value is not None for value in
                                        (panel_stake, input_stake, button_stake))
        selected_panel_available = bool(
            selected_panel_row
            and selected_panel_row.get("state") == "BET_AVAILABLE"
            and selected_panel_row.get("pending_bet_state") == "CLEAR"
            and selected_panel_row.get("currency") == "BIF"
        )
        selected_input_available = bool(selected_input
                                        and selected_input.get("visible") is True)
        selected_button_available = bool(selected_button
                                         and selected_button.get("visible") is True
                                         and selected_button.get("disabled") is False)
        readback_matches = bool(
            controls_ready and type(selected_stake) is int and selected_stake > 0
            and selected_currency == "BIF" and current_currency == "BIF"
            and selected_panel_available and selected_input_available
            and selected_button_available
            and panel_stake == selected_stake
            and input_stake == selected_stake
            and button_stake == selected_stake
        )
        if readback_matches:
            checks["displayed_stake"] = "MATCHED_SELECTED_PANEL_READBACK"
            checks["stake"] = "MATCHED_SELECTED_PANEL_READBACK"
        elif (type(selected_stake) is int and selected_stake > 0
              and readback_evidence_present and (
                  panel_stake is not None and panel_stake != selected_stake
                  or input_stake is not None and input_stake != selected_stake
                  or button_stake is not None and button_stake != selected_stake
              )):
            checks["displayed_stake"] = "MISMATCHED_SELECTED_PANEL_READBACK"
            checks["stake"] = "MISMATCHED_SELECTED_PANEL_READBACK"
            reasons.append("selected panel displayed stake does not match the configured BIF stake")
        else:
            checks["displayed_stake"] = "UNVERIFIED"
            if type(selected_stake) is int and selected_stake > 0:
                checks["stake"] = "UNAVAILABLE"
                reasons.append("selected panel displayed stake could not be verified")
        # Read-only DOM evidence can verify visible configuration, but it does
        # not prove the platform accepted it or accepted a bet. Keep execution
        # and target identity blocked until those independent receipts exist.
        backend = getattr(getattr(app.state, "betting", None), "backend", None)
        capability_reader = getattr(backend, "verified_capabilities", None)
        capabilities = capability_reader() if callable(capability_reader) else {}
        capabilities = capabilities if isinstance(capabilities, dict) else {}
        required_platform_capabilities = (
            "bet_submission", "target_identity", "betting_deadline",
            "cashout_acknowledgement", "acceptance_receipt", "settlement_receipt",
            "currency_validation",
        )
        missing_platform_capabilities = [name for name in required_platform_capabilities
                                         if capabilities.get(name) is not True]
        checks["platform_capabilities"] = ("VERIFIED" if not missing_platform_capabilities
                                           else "UNVERIFIED")
        checks["provider_adapter_implementation"] = (
            "COMPLETE" if not missing_platform_capabilities else "INCOMPLETE")
        if missing_platform_capabilities:
            reasons.append("provider transaction adapter is incomplete for required LIVE capabilities")
        checks["platform_execution"] = ("READY" if capabilities.get("bet_submission") is True
                                        else "UNVERIFIED")
        checks["transaction_verification"] = (
            "VERIFIED" if capabilities.get("acceptance_receipt") is True
            and capabilities.get("settlement_receipt") is True else "UNVERIFIED")
        reasons.extend(f"platform_capability_unverified:{name}"
                       for name in missing_platform_capabilities)
        checks["target_identity"] = "UNVERIFIED"
        reasons.append("the next target has no verified provider round identity before execution")
        mode_manager = getattr(app.state, "betting_mode", None)
        config = execution_configuration if isinstance(execution_configuration, dict) else {}
        active_authorization = bool(
            mode_manager is not None and getattr(mode_manager, "live_authorized", False)
            and config.get("real_funds_can_be_lost") is True
        )
        authorization_context = config.get("_authorization_context")
        activation_confirmation = bool(
            isinstance(authorization_context, dict)
            and isinstance(authorization_context.get("operator_identity"), str)
            and authorization_context["operator_identity"].startswith("api-key:")
            and bool(authorization_context.get("review_id"))
            and authorization_context.get("confirmation") is True
            and authorization_context.get("real_funds_can_be_lost") is True
        )
        if active_authorization:
            checks["operator_authorization"] = "ACTIVE"
        elif activation_confirmation:
            checks["operator_authorization"] = "CONFIRMED_FOR_ACTIVATION"
        elif config.get("_authorization_review") is True:
            checks["operator_authorization"] = "PENDING_EXPLICIT_CONFIRMATION"
        else:
            checks["operator_authorization"] = "MISSING"
            reasons.append("EXPLICIT_LIVE_SESSION_AUTHORIZATION_REQUIRED")
        checks["live_execution_authorization"] = "ACTIVE" if active_authorization else "BLOCKED_UNTIL_ACTIVATED"
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
        active_rules = rules_status.get("active_session") or {}
        active_rule_selection = (active_rules.get("status") == "RUNNING"
                                 and active_rules.get("selection_status") == "ACTIVE")
        checks["rules_selection_session"] = "READY" if active_rule_selection else "UNAVAILABLE"
        if not active_rule_selection:
            reasons.append("an active Rules Engine selection session is required for LIVE stop controls")
        prior_live_stop = active_rules.get("live_execution_stop") or {}
        if prior_live_stop.get("status") == "UNCONFIGURED":
            checks["live_stop_configuration"] = "AVAILABLE"
        else:
            checks["live_stop_configuration"] = "ALREADY_BOUND_TO_RULES_SESSION"
            reasons.append("LIVE stop controls are already bound to this Rules Engine session")
        interface_status = (rules_engine.decision_interface_status()
                            if rules_engine is not None
                            and callable(getattr(rules_engine, "decision_interface_status", None))
                            else {"available": False, "missing_capabilities": ["rules_engine_interface"]})
        decision_interface_ready = interface_status.get("available") is True
        checks["software_integration"] = "READY" if decision_interface_ready else "INCOMPLETE"
        checks["decision_interface"] = "AVAILABLE" if decision_interface_ready else "UNAVAILABLE"
        if not decision_interface_ready:
            reasons.append("durable_shared_select_intent_store_unavailable")
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
            if currency != "BIF":
                return {"ok": False, "error": "verified_bif_account_required_without_currency_conversion"}
            for enabled_key, count_key in (
                ("reconciled_win_limit_enabled", "reconciled_win_limit_count"),
                ("reconciled_loss_count_limit_enabled", "reconciled_loss_count_limit"),
            ):
                enabled = payload.get(enabled_key)
                count = payload.get(count_key)
                if type(enabled) is not bool:
                    return {"ok": False, "error": f"{enabled_key}_must_be_explicit"}
                if enabled and (type(count) is not int or count < 1):
                    return {"ok": False, "error": f"{count_key}_must_be_positive_integer"}
            platform_observation = payload.get("_platform_observation") or {}
            if currency != platform_observation.get("currency"):
                return {"ok": False, "error": "live_stake_currency_mismatch"}
            if (not isinstance(platform_observation.get("balance"), (int, float))
                    or isinstance(platform_observation.get("balance"), bool)
                    or stake > platform_observation.get("balance")):
                return {"ok": False, "error": "live_stake_exceeds_or_lacks_verified_balance"}
            try:
                if float(payload.get("cashout_target", 2.0)) != 2.0:
                    return {"ok": False, "error": "live_cashout_target_must_be_2_00"}
            except (TypeError, ValueError):
                return {"ok": False, "error": "live_cashout_target_must_be_2_00"}
            for enabled_key, amount_key in (
                ("win_limit_enabled", "win_limit"),
                ("session_loss_limit_enabled", "session_loss_limit"),
            ):
                enabled = payload.get(enabled_key)
                amount = payload.get(amount_key)
                if type(enabled) is not bool:
                    return {"ok": False, "error": f"{enabled_key}_must_be_explicit"}
                if enabled and (isinstance(amount, bool) or not isinstance(amount, (int, float))
                                or not math.isfinite(float(amount)) or amount <= 0):
                    return {"ok": False, "error": f"{amount_key}_must_be_positive"}
            readiness = await self.production_readiness_check(platform_observation, {
                "execution_panel": panel, "stake_amount": stake, "currency": currency,
            })
            if not readiness["ready"]:
                await self._audit("activation_blocked", {"reasons": readiness["reasons"]})
                return {"ok": False, "error": "LIVE ACTIVATION BLOCKED", "reasons": readiness["reasons"], "readiness": readiness}
            profile = payload.get("profile", "PROFILE_A")
            sid = f"LIVE-{uuid.uuid4().hex[:12]}"
            started_at = now()
            rules_engine = getattr(self.app.state, "rules_engine", None)
            install_stops = getattr(rules_engine, "install_live_execution_stops", None)
            if not callable(install_stops):
                return {"ok": False, "error": "live_stop_controls_unavailable",
                        "reasons": ["receipt_backed_live_stop_controls_not_integrated"]}
            stop_installation = install_stops({
                "live_session_id": sid, "started_at": started_at, "currency": currency,
                "profit_target_enabled": bool(payload.get("win_limit_enabled")),
                "profit_target": payload.get("win_limit"),
                "loss_limit_enabled": bool(payload.get("session_loss_limit_enabled")),
                "loss_limit": payload.get("session_loss_limit"),
                "win_count_limit_enabled": payload.get("reconciled_win_limit_enabled"),
                "win_count_limit": payload.get("reconciled_win_limit_count"),
                "loss_count_limit_enabled": payload.get("reconciled_loss_count_limit_enabled"),
                "loss_count_limit": payload.get("reconciled_loss_count_limit"),
            })
            if not stop_installation.get("ok"):
                return {"ok": False, "error": "live_stop_controls_unavailable",
                        "reasons": [stop_installation.get("reason") or "live_stop_configuration_failed"]}
            req = SessionStartRequest(action="start", mode="REAL", profile=profile,
                                      starting_balance=payload.get("starting_balance"),
                                      goal_balance=payload.get("goal_balance"),
                                      session_loss_limit_enabled=payload.get("session_loss_limit_enabled"),
                                      session_loss_limit=payload.get("session_loss_limit"),
                                      win_limit_enabled=payload.get("win_limit_enabled"),
                                      win_limit=payload.get("win_limit"), label="live")
            betting = self.app.state.betting
            try:
                status = await betting.start_session(req)
            except Exception:
                rollback = getattr(rules_engine, "remove_unstarted_live_execution_stops", None)
                if callable(rollback):
                    rollback(sid)
                raise
            snapshot = {"live_session_id": sid, "configuration_version": payload.get("configuration_version", "current"),
                        "risk_version": "current", "executor_version": "current", "started_at": started_at, "profile": profile,
                        "rules_session_id": stop_installation.get("rules_session_id"),
                        "operator_identity": operator_identity, "execution_panel": panel,
                        "stake_amount": stake, "currency": currency,
                        "cashout_target": payload.get("cashout_target", 2.0),
                        "real_funds_can_be_lost": True,
                        "stop_conditions": {
                            "profit_target_enabled": bool(payload.get("win_limit_enabled")),
                            "profit_target": payload.get("win_limit"),
                            "loss_limit_enabled": bool(payload.get("session_loss_limit_enabled")),
                            "loss_limit": payload.get("session_loss_limit"),
                            "win_limit_enabled": bool(payload.get("win_limit_enabled")),
                            "win_limit": payload.get("win_limit"),
                            "win_count_limit_enabled": bool(payload.get("reconciled_win_limit_enabled")),
                            "win_count_limit": payload.get("reconciled_win_limit_count"),
                            "loss_count_limit_enabled": bool(payload.get("reconciled_loss_count_limit_enabled")),
                            "loss_count_limit": payload.get("reconciled_loss_count_limit"),
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
        rules_engine = getattr(getattr(self.app, "state", None), "rules_engine", None)
        rules_status = rules_engine.status() if rules_engine is not None else {}
        active_rules = rules_status.get("active_session") or {}
        live_execution_stop = active_rules.get("live_execution_stop")
        if not live_execution_stop or live_execution_stop.get("status") == "UNCONFIGURED":
            sessions = rules_status.get("sessions") or []
            prior_live_session = next((row for row in sessions
                                       if (row.get("live_execution_stop") or {}).get("status")
                                       not in {None, "UNCONFIGURED"}), None)
            live_execution_stop = (prior_live_session or {}).get("live_execution_stop")
        return {"mode": self.mode, "live_active": self.live_active, "automatic_betting": self.automatic_betting,
                "reason": self.reason, "session": self.session, "readiness": self.last_readiness,
                "cashout_refresh": self.last_cashout_refresh,
                "live_execution_stop": live_execution_stop or {
                    "status": "UNCONFIGURED", "currency": None,
                    "realized_profit_loss": 0.0, "win_count": 0, "loss_count": 0,
                }}
