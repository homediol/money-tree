"""Explicit, fail-closed production activation and final live execution gate."""
from __future__ import annotations

import asyncio
import json
import math
import re
import uuid
from datetime import datetime, timezone
from typing import Any

MANUAL_SETUP_READBACK_MAX_AGE_SECONDS = 180
REAL_MONEY_EXECUTION_ENABLED = False


def live_execution_blocker_matrix() -> list[dict[str, str]]:
    """Report original LIVE blockers without treating Demo evidence as LIVE proof."""
    rows = [
        ("REAL-MONEY EXECUTION: DISABLED", "UNRESOLVED", "Execution constant is false; low-level bet placement fails closed.", "Execution gate remains disabled.", "app/live.py; app/betting/browser_client.py", "Fail-closed tests; no LIVE transaction test.", "Complete LIVE qualification and provider evidence."),
        ("real_money_execution_disabled_during_refactoring", "RESOLVED_SHARED_CODE", "The old wording called the stop temporary refactoring work, but qualification is incomplete.", "Renamed the reason to pending LIVE qualification without enabling execution.", "app/live.py; app/services/betting_mode.py; tests/test_betting_mode_safety.py", "Reason and disabled-state regression tests.", "Keep execution disabled until remaining LIVE requirements pass."),
        ("live placement permission is not enabled in backend configuration", "UNRESOLVED", "Backend placement permission is unset or false by default.", "Configuration gate remains off absent an explicit authorized decision.", "app/live.py production readiness", "Readiness configuration tests; permission unchanged.", "Authorized backend configuration decision after qualification."),
        ("both auto-cashout panels are not freshly confirmed at 2.00x with Auto Bet OFF", "REQUIRES_WINNER_LIVE_VERIFICATION", "No current Winner LIVE readback for both panels was obtained.", "Platform settings remain operator-owned; stale evidence is rejected.", "Winner LIVE DOM/readback required; active collector owns browser lock.", "No current panel evidence.", "Fresh Winner LIVE panel values and provider acknowledgement."),
        ("shared Aviator betting window and game phase are UNKNOWN", "REQUIRES_WINNER_LIVE_VERIFICATION", "No reliable current Winner LIVE phase observation was obtained.", "Unknown phase remains fail-closed.", "Winner LIVE DOM/frame required; Demo probe could not acquire collector lock.", "No phase transition test.", "Verify Winner LIVE selectors and phase transitions."),
        ("target betting deadline has no explicit live countdown evidence", "REQUIRES_WINNER_LIVE_VERIFICATION", "No provider countdown tied to a Winner LIVE target was verified.", "Estimated deadlines are not accepted.", "Winner LIVE DOM/provider evidence required.", "No countdown measurement.", "Verify fresh deadline and target binding."),
        ("provider transaction adapter is incomplete for required LIVE capabilities", "UNRESOLVED", "Real adapter reports required provider capabilities as unverified and blocks placement.", "No capability or receipt was fabricated.", "app/betting/browser_adapters.py; app/betting/browser_client.py", "Fail-closed adapter tests; no provider transaction test.", "Complete and verify the Winner LIVE transaction contract."),
        ("platform_capability_unverified:bet_submission", "REQUIRES_PROVIDER_AUTHORIZATION", "Provider submission behavior is not authorized or verified for Winner LIVE.", "Browser placement remains blocked.", "app/betting/browser_adapters.py capability contract.", "No real-money submission attempted.", "Authorized submission and provider acceptance evidence."),
        ("platform_capability_unverified:target_identity", "REQUIRES_WINNER_LIVE_VERIFICATION", "No provider-issued target round identity is verified before execution.", "Missing identity remains UNKNOWN.", "Winner LIVE DOM/provider evidence required.", "No target identity captured.", "Verify the exact provider target identity."),
        ("platform_capability_unverified:betting_deadline", "REQUIRES_WINNER_LIVE_VERIFICATION", "No Winner LIVE deadline evidence is verified.", "Deadline gate remains fail-closed.", "Winner LIVE DOM/provider evidence required.", "No deadline captured.", "Verify explicit countdown and expiry behavior."),
        ("platform_capability_unverified:cashout_acknowledgement", "REQUIRES_PROVIDER_AUTHORIZATION", "Visible control state does not prove provider acknowledgement.", "UI color or local state is not treated as acknowledgement.", "app/betting/browser_adapters.py capability contract.", "No cashout or provider acknowledgement test.", "Authorized provider acknowledgement evidence."),
        ("platform_capability_unverified:acceptance_receipt", "REQUIRES_PROVIDER_AUTHORIZATION", "No Winner LIVE acceptance receipt contract is verified.", "Submission and panel changes are not acceptance.", "app/betting/browser_adapters.py transaction verification.", "No receipt captured.", "Authentic Winner LIVE acceptance receipt."),
        ("platform_capability_unverified:settlement_receipt", "REQUIRES_PROVIDER_AUTHORIZATION", "No independent provider settlement receipt is verified.", "Round result or balance movement alone is not settlement.", "app/betting/browser_adapters.py transaction verification.", "No settlement receipt captured.", "Authentic settlement record and reconciliation."),
        ("platform_capability_unverified:currency_validation", "REQUIRES_WINNER_LIVE_VERIFICATION", "Fresh Winner LIVE account and panel currency are unavailable.", "Unknown or mismatched currency remains blocked.", "Winner LIVE account and panel readback required.", "No current currency evidence.", "Verify account, panel, and accepted stake currency."),
        ("the next target has no verified provider round identity before execution", "REQUIRES_WINNER_LIVE_VERIFICATION", "Next target is not bound to a verified provider-issued round identity.", "Selection intents remain undispatched without target evidence.", "app/live.py readiness; Winner LIVE provider evidence required.", "Target identity regression tests; no live target captured.", "Verify exact next Winner LIVE round identity."),
        ("EXPLICIT_LIVE_SESSION_AUTHORIZATION_REQUIRED", "REQUIRES_PROVIDER_AUTHORIZATION", "No explicit authorized Winner LIVE session is active.", "Authorization remains session-scoped and is not restored after restart or login expiry.", "app/live.py authorization checks.", "Authorization rejection tests; no authorization issued.", "Authenticated operator must authorize a qualified LIVE session."),
        ("an active Rules Engine selection session is required", "UNRESOLVED", "No active selection session was verified for this run.", "Session lifecycle and selection gates remain required; no session was started by this audit.", "app/rules_engine.py; app/live.py selection-session check.", "Existing lifecycle tests; no Demo-round session test.", "Start and verify a session on fresh committed rounds."),
    ]
    return [{"original_blocker": original, "status": status, "root_cause": cause,
             "fix_implemented": fix, "evidence_source": evidence,
             "test_result": result, "remaining_requirement": remaining}
            for original, status, cause, fix, evidence, result, remaining in rows]


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
    supports_manual_executor_v2 = True
    supports_readiness_stop_state_v1 = True

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
        if not REAL_MONEY_EXECUTION_ENABLED:
            checks["real_money_execution"] = "DISABLED_PENDING_LIVE_QUALIFICATION"
            reasons.append("real_money_execution_disabled_pending_live_qualification")
        app = self.app
        if app is None:
            reasons.append("application unavailable")
            return {"ready": False, "state": "BLOCKED", "reasons": reasons,
                    "checks": checks, "optional_bankroll_policy": "NOT_APPLIED"}
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
            if (both_cashout_panels_configured
                    and "PLATFORM_CASHOUT_ACKNOWLEDGEMENT_UNVERIFIED" in failures):
                reason = "both auto-cashout panels are configured, but platform acknowledgement is unverified"
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
            # Selection is application configuration; fresh panel/button
            # evidence is a separate execution-integrity check above. Keep
            # these facts independent so a missing browser snapshot cannot
            # erase a valid operator-selected panel.
            checks["selected_panel"] = "VERIFIED"
        else:
            checks["selected_panel"] = "UNSELECTED"
            reasons.append("LIVE execution panel must be explicitly selected")
        current_currency = observation.get("currency")
        current_balance = observation.get("balance")
        # Stake is operator-owned. Compare fresh read-only values from the
        # selected panel, field, and Bet label; never write to platform controls.
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
        selected_stake = panel_stake if panel_stake is not None else input_stake
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
            controls_ready and isinstance(selected_stake, int) and selected_stake > 0
            and current_currency == "BIF"
            and selected_panel_available and selected_input_available
            and selected_button_available
            and panel_stake == selected_stake
            and input_stake == selected_stake
            and button_stake == selected_stake
        )
        if readback_matches:
            checks["displayed_stake"] = "MATCHED_SELECTED_PANEL_READBACK"
            checks["stake"] = "MATCHED_SELECTED_PANEL_READBACK"
            checks["stake_currency"] = "MATCHES_VERIFIED_ACCOUNT"
            if (isinstance(current_balance, (int, float)) and not isinstance(current_balance, bool)
                    and selected_stake > current_balance):
                checks["stake"] = "EXCEEDS_VERIFIED_PLATFORM_BALANCE"
                reasons.append("operator_stake_exceeds_current_verified_platform_balance")
        elif (readback_evidence_present and (panel_stake != input_stake
                                             or input_stake != button_stake)):
            checks["displayed_stake"] = "MISMATCHED_SELECTED_PANEL_READBACK"
            checks["stake"] = "MISMATCHED_SELECTED_PANEL_READBACK"
            checks["stake_currency"] = "UNVERIFIED_OR_MISMATCHED"
            reasons.append("operator_stake_field_panel_and_button_values_do_not_match")
        else:
            checks["displayed_stake"] = "UNVERIFIED"
            checks["stake"] = "UNAVAILABLE"
            checks["stake_currency"] = ("MATCHES_VERIFIED_ACCOUNT" if current_currency == "BIF"
                                         else "UNVERIFIED_OR_MISMATCHED")
            reasons.append("operator_stake_readback_unverified")
        # Read-only DOM evidence can verify visible configuration, but it does
        # not prove the platform accepted it or accepted a bet. Keep execution
        # and target identity blocked until those independent receipts exist.
        backend = getattr(getattr(app.state, "betting", None), "backend", None)
        capability_reader = getattr(backend, "verified_capabilities", None)
        if callable(capability_reader):
            capabilities = capability_reader()
        else:
            from app.betting.browser_adapters import UNVERIFIED_LIVE_CAPABILITIES
            capabilities = dict(UNVERIFIED_LIVE_CAPABILITIES)
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
            reasons.append("an active Rules Engine selection session is required")
        interface_status = (rules_engine.decision_interface_status()
                            if rules_engine is not None
                            and callable(getattr(rules_engine, "decision_interface_status", None))
                            else {"available": False, "missing_capabilities": ["rules_engine_interface"]})
        decision_interface_ready = interface_status.get("available") is True
        executor_dispatch_ready = interface_status.get("executor_dispatch_available") is True
        checks["software_integration"] = (
            "READY" if decision_interface_ready and executor_dispatch_ready else "INCOMPLETE")
        checks["decision_interface"] = "AVAILABLE" if decision_interface_ready else "UNAVAILABLE"
        checks["rules_to_executor_dispatch"] = (
            "AVAILABLE" if executor_dispatch_ready else
            str(interface_status.get("executor_dispatch") or "UNAVAILABLE"))
        if not decision_interface_ready:
            reasons.append("durable_shared_select_intent_store_unavailable")
        if not executor_dispatch_ready:
            reasons.append("rules_engine_select_not_dispatched_to_betting_executor")
        risk = getattr(app.state, "risk", None)
        checks["emergency_stop"] = ("UNAVAILABLE" if risk is None else
                                     "LATCHED" if risk.emergency_latched else "CLEAR")
        if risk and risk.emergency_latched: reasons.append("emergency stop is latched")
        checks["execution_integrity"] = (
            "READY" if (REAL_MONEY_EXECUTION_ENABLED and risk is not None
                        and not risk.emergency_latched and not reasons) else "BLOCKED")
        result = {"ready": not reasons, "state": "LIVE_READY" if not reasons else "BLOCKED",
                  "reasons": list(dict.fromkeys(reasons)), "checks": checks,
                  "optional_bankroll_policy": "NOT_APPLIED",
                  "provider_capabilities": capabilities,
                  "missing_provider_capabilities": missing_platform_capabilities,
                  "blocker_matrix": live_execution_blocker_matrix(),
                  "checked_at": now()}
        self.last_readiness = result
        return result

    async def start(self, payload: dict[str, Any], *, _mode_authorization=None) -> dict[str, Any]:
        async with self._lock:
            from app.services.betting_mode import LIVE_MODE_AUTHORIZATION
            if _mode_authorization is not LIVE_MODE_AUTHORIZATION:
                return {"ok": False, "error": "backend_mode_authorization_required"}
            if payload.get("confirmation") != "ENABLE LIVE BETTING":
                return {"ok": False, "error": "explicit_confirmation_required"}
            operator_identity = payload.get("operator_identity")
            if not isinstance(operator_identity, str) or not operator_identity.startswith("api-key:"):
                return {"ok": False, "error": "operator_authorization_required"}
            if payload.get("real_funds_can_be_lost") is not True:
                return {"ok": False, "error": "real_funds_at_risk_confirmation_required"}
            if self.live_active:
                return {"ok": False, "error": "already_active", "status": self.status()}
            if not REAL_MONEY_EXECUTION_ENABLED:
                result = {"ok": False, "error": "real_money_execution_disabled_pending_live_qualification",
                          "reasons": ["REAL_MONEY_EXECUTION_DISABLED_PENDING_LIVE_QUALIFICATION"]}
                await self._audit("activation_blocked", result)
                return result
            return {"ok": False, "error": "live_executor_not_qualified",
                    "reasons": ["LIVE_EXECUTOR_INTEGRITY_REVIEW_REQUIRED"]}

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
                "cashout_refresh": self.last_cashout_refresh,
                "execution_integrity": {"status": "DISABLED" if not REAL_MONEY_EXECUTION_ENABLED
                                        else "NOT_AUTHORIZED",
                                        "reason": "REAL_MONEY_EXECUTION_DISABLED_PENDING_LIVE_QUALIFICATION"},
                "blocker_matrix": live_execution_blocker_matrix()}
