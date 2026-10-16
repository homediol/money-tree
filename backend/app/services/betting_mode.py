"""Backend-owned, mutually exclusive SHADOW_REALISTIC/LIVE_REAL mode switch."""
from __future__ import annotations

import asyncio
import math
import secrets
import time
from datetime import datetime, timezone
from typing import Any

SHADOW_REALISTIC = "SHADOW_REALISTIC"
LIVE_REAL = "LIVE_REAL"
LIVE_MODE_AUTHORIZATION = object()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class BettingModeManager:
    MODES = {SHADOW_REALISTIC, LIVE_REAL}

    def __init__(self, app):
        self.app = app
        self.repository = app.state.wp.repository
        persisted = self.repository.load_betting_mode()
        runtime = (self.repository.load_application_state("betting_runtime")
                   if hasattr(self.repository, "load_application_state") else None) or {}
        # Authorization is process-local by design. A restart always disarms
        # LIVE and requires a fresh balance review and explicit activation.
        self.mode = SHADOW_REALISTIC if (runtime.get("mode") or persisted and persisted.get("mode")) != LIVE_REAL else SHADOW_REALISTIC
        self.live_authorized = False
        self.operator_identity: str | None = None
        self.transitioning = False
        self.reason = "restored from persistence; LIVE requires fresh authorization" if runtime or persisted else "safe startup mode"
        self.shadow_ready = False
        self.last_change = _now()
        self.recovered_live = bool(persisted and persisted.get("mode") == LIVE_REAL)
        self._lock = asyncio.Lock()
        self._balance_read_lock = asyncio.Lock()
        self._reviews: dict[str, dict[str, Any]] = {}
        self._persist("startup_safe_mode", previous_mode=(persisted or {}).get("mode"))

    def _persist(self, reason: str, **extra) -> None:
        row = {"mode": self.mode, "updated_at": _now(), "reason": reason,
               "live_authorized": self.live_authorized,
               "operator_identity": self.operator_identity,
               **extra}
        self.repository.save_betting_mode(self.mode, row)
        if hasattr(self.repository, "save_application_state"):
            self.repository.save_application_state("betting_runtime", row, row["updated_at"])
        self.last_change = row["updated_at"]

    async def _broadcast(self) -> None:
        manager = getattr(self.app.state, "manager", None)
        if manager:
            await manager.broadcast({"type": "betting-mode:updated", "mode": self.status()})

    def status(self) -> dict[str, Any]:
        shadow = getattr(self.app.state, "shadow", None)
        live = getattr(self.app.state, "live", None)
        open_executions = []
        try:
            open_executions = self.repository.open_executions()
        except Exception:
            pass
        shadow_status = (None if shadow is None else {
            "mode": shadow.mode,
            "session_id": (shadow.session or {}).get("session_id"),
            "workflow": "RETIRED_READ_ONLY_HISTORY",
        })
        return {
            "mode": self.mode,
            "badge": "LIVE — REAL MONEY" if self.mode == LIVE_REAL else "SHADOW — NO REAL MONEY",
            "live_authorized": self.live_authorized and bool(live and live.live_active),
            "operator_identity": self.operator_identity if self.live_authorized else None,
            "transitioning": self.transitioning,
            "shadow_running": bool(shadow and shadow.mode == "SHADOW" and self.shadow_ready),
            "shadow": shadow_status,
            "live": live.status() if live else None,
            "open_real_executions": len(open_executions),
            "reconciliation_required": bool(open_executions),
            "reason": self.reason,
            "recovered_live_on_restart": self.recovered_live,
            "updated_at": self.last_change,
        }

    def betting_status(self) -> dict:
        if self.mode == SHADOW_REALISTIC:
            shadow = self.app.state.shadow
            if shadow.mode == "SHADOW" and self.shadow_ready:
                return shadow.betting_status()
            return {"mode": "SHADOW", "state": "STOPPED", "automatic_enabled": False,
                    "current_balance": None, "browser_status": "SHADOW",
                    "last_ui_ready": True, "current_bet": None,
                    "emergency_stop": bool(self.app.state.risk.emergency_latched)}
        return self.app.state.betting.status()

    async def _read_platform_balance(self) -> dict[str, Any]:
        async with self._balance_read_lock:
            try:
                observation = await asyncio.wait_for(self._snapshot_platform_balance(), timeout=12)
            except asyncio.CancelledError:
                task = asyncio.current_task()
                # Preserve caller/shutdown cancellation. A cancellation raised
                # by the browser transport itself is an unavailable observation,
                # not a middleware-level missing-response failure.
                if task is not None and task.cancelling():
                    raise
                observation = {"verified": False, "balance": None, "ui_ready": False,
                               "browser_status": "UNAVAILABLE", "observed_at": _now(),
                               "source": "existing_cdp_page_read_only",
                               "error": "browser_balance_snapshot_cancelled"}
            except Exception as exc:
                observation = {"verified": False, "balance": None, "ui_ready": False,
                               "browser_status": "UNAVAILABLE", "observed_at": _now(),
                               "source": "existing_cdp_page_read_only",
                               "error": str(exc) or "browser balance read timed out"}
            health = getattr(self.app.state, "system_health", None)
            if health is not None:
                health.record_browser_observation(observation)
            return observation

    async def _snapshot_platform_balance(self) -> dict[str, Any]:
        """Read the existing CDP-owned page; never launch or create a browser."""
        from app.betting.browser_client import AviatorBrowserClient
        from app.betting.cashout import inspect_cashout_configuration

        client = AviatorBrowserClient(self.app.state.betting.settings)
        try:
            snapshot = await client.snapshot()
            payload = snapshot.as_dict() if hasattr(snapshot, "as_dict") else snapshot
            balance = payload.get("balance") if isinstance(payload, dict) else None
            numeric_balance = (isinstance(balance, (int, float)) and not isinstance(balance, bool)
                               and math.isfinite(balance) and balance >= 0)
            authentication_state = str((payload or {}).get("authentication_state") or "UNKNOWN")
            live_status = getattr(getattr(self.app.state, "live", None), "status", lambda: {})()
            live_session = (live_status or {}).get("session") or {}
            rules_status = getattr(getattr(self.app.state, "rules_engine", None), "status", lambda: {})()
            rules_session = (rules_status or {}).get("active_session") or {}
            application_session_id = (live_session.get("session_id") or rules_session.get("session_id"))
            platform_session_identity = (payload or {}).get("session_identity")
            provider_identity = (payload or {}).get("provider_identity")
            observed_at = (payload or {}).get("balance_observed_at")
            currency = payload.get("balance_currency") if isinstance(payload, dict) else None
            age_seconds = None
            try:
                observed = datetime.fromisoformat(str(observed_at).replace("Z", "+00:00"))
                if observed.tzinfo is None:
                    observed = observed.replace(tzinfo=timezone.utc)
                age_seconds = (datetime.now(timezone.utc) - observed).total_seconds()
            except (TypeError, ValueError):
                pass
            fresh = age_seconds is not None and 0 <= age_seconds <= 20
            verified = bool(payload and payload.get("ok") and numeric_balance and currency
                            and authentication_state == "AUTHENTICATED" and provider_identity
                            and platform_session_identity and fresh)
            raw_cashout_controls = (payload or {}).get("cashout_controls") or []
            cashout_panels = []
            for slot in (0, 1):
                control = next((item for item in raw_cashout_controls
                                if item.get("slot") == slot), None)
                inspected = inspect_cashout_configuration(control)
                cashout_panels.append({
                    "panel": slot + 1,
                    "configuration_valid": inspected.get("configuration_valid") is True,
                    "auto_bet_enabled": control.get("autoBetEnabled") if control else None,
                    "auto_tab_selected": control.get("autoTabSelected") if control else None,
                    "auto_cashout_enabled": inspected.get("enabled"),
                    "displayed_value": inspected.get("displayed_value"),
                    "platform_acknowledged": inspected.get("platform_acknowledged"),
                    "failures": inspected.get("failures", []),
                })
            observation = {
                "verified": verified,
                "balance": balance if verified else None,
                "displayed_amount": balance if numeric_balance else None,
                "currency": currency,
                "currency_verified": bool(currency),
                "balance_text": payload.get("balance_text") if isinstance(payload, dict) else None,
                "ui_ready": bool(payload.get("ui_ready")) if isinstance(payload, dict) else False,
                "snapshot_ok": bool(payload and payload.get("ok")),
                "browser_status": "CONNECTED" if payload and payload.get("ok") else "UNAVAILABLE",
                "observed_at": observed_at or _now(),
                "age_seconds": age_seconds,
                "fresh": fresh,
                "source": "existing_cdp_page_read_only",
                "provider_identity": provider_identity,
                "session_identity": platform_session_identity or application_session_id,
                "session_identity_source": "PLATFORM" if platform_session_identity else
                    "APPLICATION_SESSION" if application_session_id else "UNAVAILABLE",
                "platform_session_identity": platform_session_identity,
                "application_session_id": application_session_id,
                "authentication_state": authentication_state,
                "cashout_configuration": inspect_cashout_configuration(
                    next((item for item in (payload or {}).get("cashout_controls", [])
                          if item.get("slot") == 0), None)),
                "cashout_panels": cashout_panels,
                "betting_controls": {
                    "snapshot_ok": bool(payload and payload.get("ok")),
                    "panels": (payload or {}).get("bet_panel_states") or [],
                    "stake_inputs": (payload or {}).get("stake_inputs") or [],
                    "bet_buttons": (payload or {}).get("place_bet_buttons") or [],
                    "betting_window": (payload or {}).get("betting_window") or {"state": "UNKNOWN"},
                    "round_phase": (payload or {}).get("round_phase") or "UNKNOWN",
                    "phase_hints": (payload or {}).get("phase_hints") or [],
                    "status_messages": (payload or {}).get("status_messages") or [],
                    "deadline_evidence": (payload or {}).get("deadline_evidence") or {"status": "UNKNOWN"},
                    "observed_at": (payload or {}).get("observed_at") or observed_at or _now(),
                },
                "error": None if verified else (payload or {}).get("error") or
                    ("authenticated_account_not_verified" if authentication_state != "AUTHENTICATED"
                     else "balance_currency_unverified" if not currency
                     else "provider_or_session_identity_unverified" if not (provider_identity and platform_session_identity)
                     else "balance_observation_stale_or_missing" if not fresh
                     else "UI balance unavailable or invalid"),
            }
            if hasattr(self.repository, "record_platform_balance_observation"):
                saved = self.repository.record_platform_balance_observation(observation)
                if saved:
                    observation["observation_id"] = saved["observation_id"]
            return observation
        except Exception as exc:
            return {"verified": False, "balance": None, "balance_text": None,
                    "browser_status": "UNAVAILABLE", "observed_at": _now(),
                    "source": "existing_cdp_page_read_only", "currency": None,
                    "currency_verified": False, "provider_identity": None,
                    "session_identity": None, "application_session_id": None,
                    "authentication_state": "UNKNOWN",
                    "cashout_configuration": inspect_cashout_configuration(None),
                    "error": str(exc)}
        finally:
            await client.close()

    @staticmethod
    def _empty_live_configuration() -> dict[str, Any]:
        return {
            "configuration_version": 2, "execution_panel": None,
            "configuration_valid": False,
            "configuration_status": "INCOMPLETE",
            "missing_configuration": ["execution_panel"],
            "saved_at": None, "reviewed_at": None,
        }

    def live_configuration(self) -> dict[str, Any]:
        """Return a stable configuration shape even before the first review."""
        reviewed = self.repository.load_application_state("live_activation_configuration")
        draft = self.repository.load_application_state("live_activation_configuration_draft")
        # Reviews also mirror their values into the draft key. A later operator
        # edit must therefore win over the last reviewed snapshot; otherwise the
        # save endpoint appears to succeed but the next read returns stale data.
        def state_time(value: dict[str, Any] | None, key: str) -> float:
            raw = (value or {}).get(key)
            if not raw:
                return float("-inf")
            try:
                return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()
            except (TypeError, ValueError, OverflowError):
                return float("-inf")

        draft_is_newer = bool(draft and (draft.get("reviewed_at") is None or not reviewed or
            state_time(draft, "saved_at") >= state_time(reviewed, "reviewed_at")))
        stored = (draft if draft_is_newer else reviewed) or draft or reviewed or {}
        persistence_source = ("DRAFT" if draft_is_newer else
                              "REVIEWED" if reviewed else "DRAFT" if draft else "DEFAULTS")
        value = {**self._empty_live_configuration(),
                 "execution_panel": (stored or {}).get("execution_panel"),
                 "saved_at": (stored or {}).get("saved_at"),
                 "reviewed_at": (stored or {}).get("reviewed_at")}
        missing = [] if type(value["execution_panel"]) is int and value["execution_panel"] in (0, 1) else ["execution_panel"]
        value["missing_configuration"] = missing
        value["configuration_valid"] = not missing
        value["configuration_status"] = "COMPLETE" if not missing else "INCOMPLETE"
        value["persistence_source"] = persistence_source
        return value

    def save_live_configuration_draft(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Persist operator choices without authorizing or activating LIVE."""
        if not isinstance(payload, dict):
            raise ValueError("INVALID_LIVE_CONFIGURATION")
        live = getattr(self.app.state, "live", None)
        if self.mode == LIVE_REAL or self.live_authorized or bool(getattr(live, "live_active", False)):
            raise ValueError("LIVE_ACTIVE_CONFIGURATION_IMMUTABLE")
        current = self.live_configuration()
        allowed = {"execution_panel"}
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError("OPTIONAL_BANKROLL_CONFIGURATION_REMOVED")
        value = {"configuration_version": 2,
                 "execution_panel": payload.get("execution_panel", current.get("execution_panel"))}
        panel = value.get("execution_panel")
        if panel is not None and (type(panel) is not int or panel not in (0, 1)):
            raise ValueError("INVALID_LIVE_EXECUTION_PANEL")
        value["saved_at"] = _now()
        # A saved edit is not a reviewed configuration, even if the last
        # reviewed values were complete. Activation challenges are one-shot
        # and must be refreshed after any save.
        value["reviewed_at"] = None
        self.repository.save_application_state(
            "live_activation_configuration_draft", value, value["saved_at"])
        self._reviews.clear()
        return self.live_configuration()

    async def live_review(self, *, execution_panel: int | None = None) -> dict:
        configuration = self.live_configuration()
        if execution_panel is None:
            execution_panel = configuration.get("execution_panel")
        platform = await self._read_platform_balance()
        blocks = []
        if type(execution_panel) is not int or execution_panel not in (0, 1):
            blocks.append("choose_a_live_execution_panel")
        if not platform["verified"]:
            blocks.append(f"real_platform_balance_unverified:{platform.get('error') or 'UNKNOWN'}")
        try:
            from app.core.config import get_settings
            api_key_configured = bool(get_settings().api_key)
        except Exception:
            api_key_configured = False
        if not api_key_configured:
            blocks.append("backend_api_key_required_for_live_authorization")
        live = self.app.state.live
        readiness = await live.production_readiness_check(platform, {
            "execution_panel": execution_panel,
            "_authorization_review": True,
        })
        blocks.extend(readiness.get("reasons", []))
        review_id = secrets.token_urlsafe(32)
        row = {
            "review_id": review_id, "created_monotonic": time.monotonic(),
            "expires_monotonic": time.monotonic() + 120,
            "platform_balance": platform,
            "readiness": readiness,
            "execution_panel": execution_panel,
        }
        self._reviews[review_id] = row
        configuration_valid = type(execution_panel) is int and execution_panel in (0, 1)
        persisted_configuration = {"configuration_version": 2,
                                  "execution_panel": execution_panel,
                                  "configuration_valid": configuration_valid,
                                  "reviewed_at": _now()}
        if configuration_valid:
            self.repository.save_application_state("live_activation_configuration", persisted_configuration,
                                                   persisted_configuration["reviewed_at"])
            self.repository.save_application_state("live_activation_configuration_draft", persisted_configuration,
                                                   persisted_configuration["reviewed_at"])
        # Keep challenge memory bounded and never restore it after restart.
        for key, review in list(self._reviews.items()):
            if review["expires_monotonic"] < time.monotonic():
                self._reviews.pop(key, None)
        return {
            "ok": True, "mode": self.mode, "live_available": not blocks,
            "platform_balance": platform, "readiness": readiness,
            "execution_panel": execution_panel,
            "configuration_valid": configuration_valid,
            "configuration_persisted": configuration_valid,
            "blockers": list(dict.fromkeys(blocks)), "review_id": review_id,
            "review_expires_in_s": 120,
        }

    async def _settle_real_executions(self) -> list[dict]:
        rows = self.repository.open_executions()
        if not rows:
            return []
        known = {str(row["round_id"]): row for row in self.app.state.history_collector.rows()}
        for execution in rows:
            target = known.get(str(execution.get("target_round_id")))
            if target:
                await self.app.state.reconciliation.reconcile_round(target)
        return self.repository.open_executions()

    async def _stop_live(self, reason: str) -> None:
        live = self.app.state.live
        self.live_authorized = False
        self.operator_identity = None
        live.explicit_live_authorization = False
        if live.live_active:
            await live.pause(reason)
        session = getattr(self.app.state.betting, "session", None)
        task = getattr(session, "_task", None) if session else None
        if task and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=8)
            except asyncio.TimeoutError:
                session.request_stop("mode_switch_timeout")

    async def switch_shadow(self, **_legacy_configuration) -> dict:
        """Retire the bankroll-managed paper executor without touching state."""
        return {"ok": False, "error": "SHADOW_FINANCIAL_EXECUTOR_RETIRED",
                "reasons": ["Rules Engine sessions observe and record SELECT decisions only"],
                "real_money_action_submitted": False}

    async def switch_live(self, payload: dict[str, Any], *, operator_identity: str | None = None) -> dict:
        async with self._lock:
            review_id = str(payload.get("review_id") or "")
            review = self._reviews.pop(review_id, None)
            if not review or review["expires_monotonic"] < time.monotonic():
                return {"ok": False, "error": "fresh_backend_live_review_required",
                        "reasons": ["live_review_missing_expired_or_already_used"]}
            if payload.get("confirmation") != "ENABLE LIVE BETTING":
                return {"ok": False, "error": "explicit_confirmation_required",
                        "reasons": ["type_ENABLE_LIVE_BETTING_after_review"]}
            if not isinstance(operator_identity, str) or not operator_identity.startswith("api-key:"):
                return {"ok": False, "error": "operator_authorization_required",
                        "reasons": ["LIVE activation requires a server-authenticated operator identity"]}
            if payload.get("real_funds_can_be_lost") is not True:
                return {"ok": False, "error": "real_funds_at_risk_confirmation_required",
                        "reasons": ["operator_must_explicitly_confirm_real_funds_can_be_lost"]}
            if payload.get("execution_panel") != review["execution_panel"]:
                return {"ok": False, "error": "live_review_mismatch",
                        "reasons": ["execution_panel_does_not_match_backend_review"]}
            if not review["platform_balance"].get("verified"):
                return {"ok": False, "error": "platform_balance_unverified",
                        "reasons": ["real_platform_balance_unverified"]}
            # Run all production checks before stopping an existing safe shadow
            # session; a rejected LIVE request must leave Shadow intact.
            platform_now = await self._read_platform_balance()
            if not platform_now.get("verified"):
                return {"ok": False, "error": "platform_balance_unverified",
                        "reasons": [f"real_platform_balance_unverified:{platform_now.get('error') or 'UNKNOWN'}"]}
            reviewed_balance = review["platform_balance"].get("balance")
            if (platform_now.get("currency") != review["platform_balance"].get("currency")
                    or platform_now.get("platform_session_identity")
                    != review["platform_balance"].get("platform_session_identity")):
                return {"ok": False, "error": "live_review_stale",
                        "reasons": ["platform_account_identity_or_currency_changed_after_review"]}
            readiness = await self.app.state.live.production_readiness_check(platform_now, {
                "execution_panel": review["execution_panel"],
                "_authorization_context": {
                    "operator_identity": operator_identity,
                    "review_id": review_id,
                    "confirmation": payload.get("confirmation") == "ENABLE LIVE BETTING",
                    "real_funds_can_be_lost": payload.get("real_funds_can_be_lost") is True,
                },
            })
            if not readiness["ready"]:
                return {"ok": False, "error": "live_activation_blocked",
                        "reasons": readiness["reasons"], "readiness": readiness}
            if self.repository.open_executions():
                return {"ok": False, "error": "reconciliation_required",
                        "reasons": ["unresolved_real_execution_exists"]}
            shadow = self.app.state.shadow
            if shadow._pending:
                return {"ok": False, "error": "shadow_round_pending",
                        "reasons": [f"shadow_result_pending:{x}" for x in shadow._pending]}
            self.transitioning = True
            await self._broadcast()
            try:
                if shadow.mode in {"SHADOW", "PAUSED"}:
                    await shadow.stop("mode_switch_to_live")
                self.live_authorized = True
                self.app.state.live.explicit_live_authorization = True
                result = await self.app.state.live.start({
                    **payload, "starting_balance": reviewed_balance,
                    "operator_identity": operator_identity,
                    "execution_panel": review["execution_panel"],
                    "_platform_observation": platform_now,
                }, _mode_authorization=LIVE_MODE_AUTHORIZATION)
                if not result.get("ok"):
                    self.live_authorized = False
                    self.app.state.live.explicit_live_authorization = False
                    return result
                self.mode = LIVE_REAL
                self.operator_identity = operator_identity
                self.shadow_ready = False
                self.reason = "explicit live authorization active"
                self._persist(self.reason, review_id=review_id, operator_identity=operator_identity)
                await self._broadcast()
                return {"ok": True, "status": self.status()}
            except Exception as exc:
                self.live_authorized = False
                self.operator_identity = None
                self.app.state.live.explicit_live_authorization = False
                self.mode = SHADOW_REALISTIC
                self.shadow_ready = False
                self.reason = f"live_activation_failed:{type(exc).__name__}:{exc}"
                self._persist(self.reason)
                await self._broadcast()
                return {"ok": False, "error": "live_activation_failed", "reasons": [self.reason],
                        "status": self.status()}
            finally:
                self.transitioning = False

    async def force_safe(self, reason: str) -> dict:
        """Disarm LIVE after any health, browser, database or safety failure."""
        async with self._lock:
            self.operator_identity = None
            if self.mode != LIVE_REAL and not self.live_authorized:
                if (self.mode == SHADOW_REALISTIC
                        and getattr(getattr(self.app.state, "shadow", None), "mode", None) == "SHADOW"):
                    await self.app.state.shadow.pause()
                    self.shadow_ready = False
                    self.reason = reason
                    self._persist(reason)
                    await self._broadcast()
                return self.status()
            self.transitioning = True
            self.mode = SHADOW_REALISTIC
            self.live_authorized = False
            self.shadow_ready = False
            self.reason = reason
            try:
                await self._stop_live(reason)
            except Exception as exc:
                self.reason = f"{reason}; live_stop_error:{type(exc).__name__}:{exc}"
            try:
                self._persist(reason)
            except Exception:
                # Memory is still disarmed; failed persistence is surfaced as
                # a safety reason and future live activation is unavailable.
                self.reason = f"{reason}; mode persistence failed"
            try:
                await self._broadcast()
            finally:
                self.transitioning = False
            return self.status()

    async def monitor_safety(self) -> dict:
        if self.mode != LIVE_REAL:
            return self.status()
        reasons = []
        if not self.live_authorized or not self.app.state.live.live_active:
            reasons.append("live_authorization_lost")
        if self.app.state.risk.emergency_latched:
            reasons.append("emergency_stop")
        can_bet = self.app.state.system_health.can_bet_now(mode="REAL")
        if not can_bet.get("allowed"):
            reasons.extend(can_bet.get("reasons", []) or ["CanBetNow_blocked"])
        try:
            with self.repository.connect() as conn:
                conn.execute("SELECT 1").fetchone()
        except Exception:
            reasons.append("database_unavailable")
        status = self.app.state.betting.status()
        if status.get("state") in {"ERROR", "NOT_CONNECTED", "WAITING_FOR_BROWSER"}:
            reasons.append(f"browser_session_{str(status.get('state')).lower()}")
        if (status.get("state") == "CONNECTED" and status.get("current_balance") is None
                and status.get("automatic_enabled")):
            reasons.append("real_balance_unverified")
        try:
            if any(row.get("status") in {"UNKNOWN", "RECONCILIATION_PENDING"}
                   or row.get("balance_status") == "MISMATCH"
                   for row in self.repository.open_executions()):
                reasons.append("unknown_result_or_balance_mismatch")
        except Exception:
            reasons.append("execution_state_unavailable")
        if reasons:
            return await self.force_safe(";".join(dict.fromkeys(reasons)))
        return self.status()

    def can_execute_live(self) -> bool:
        try:
            from app.live import REAL_MONEY_EXECUTION_ENABLED
        except Exception:
            return False
        return (REAL_MONEY_EXECUTION_ENABLED is True
                and self.mode == LIVE_REAL and self.live_authorized and not self.transitioning)

    async def final_execution_gate(self, intent, *, execution_id: str | None = None) -> dict[str, Any]:
        rules = getattr(self.app.state, "rules_engine", None)
        rules_status = rules.status() if rules is not None else {}
        last_evaluation = getattr(rules, "last_evaluation", None) if rules is not None else None
        execution_store = getattr(getattr(self.app.state, "wp", None), "repository", None)
        if hasattr(intent, "as_dict"):
            candidate = intent.as_dict()
        elif isinstance(intent, dict):
            candidate = dict(intent)
        else:
            candidate = {}
        decision_id = candidate.get("decision_id")
        rule_intent_reader = getattr(execution_store, "rule_execution_intent", None)
        durable_rule_intent = (rule_intent_reader(str(decision_id))
                               if callable(rule_intent_reader) and decision_id else None)
        if durable_rule_intent:
            candidate = {**durable_rule_intent, **candidate}
            # The durable record is authoritative for these immutable fields.
            for field in ("decision_id", "source_round_id", "target_round_id", "target_stream_id",
                          "target_round_index", "target_round_ref", "selected_rule_ids",
                          "selected_panel", "requested_stake", "requested_stake_currency",
                          "cashout_target", "created_at", "expires_at", "session_id",
                          "execution_status", "execution_eligibility", "deadline_evidence"):
                if field in durable_rule_intent:
                    candidate[field] = durable_rule_intent[field]
        betting = getattr(self.app.state, "betting", None)
        risk = getattr(self.app.state, "risk", None)
        checks: dict[str, str] = {}
        reasons = []
        if self.can_execute_live():
            checks["operator_authorization"] = "ACTIVE"
        else:
            checks["operator_authorization"] = "BLOCKED"
            reasons.append("mode_is_not_authorized_live_real")
        if risk is not None and not risk.emergency_latched:
            checks["emergency_stop"] = "CLEAR"
        else:
            checks["emergency_stop"] = "LATCHED" if risk is not None else "UNAVAILABLE"
            reasons.append("emergency_stop_active_or_risk_manager_unavailable")
        if int(rules_status.get("rules_enabled") or 0) >= 1:
            checks["enabled_rules"] = "AVAILABLE"
        else:
            checks["enabled_rules"] = "UNAVAILABLE"
            reasons.append("RULES_ENGINE_NOT_CONFIGURED")
        if (last_evaluation and last_evaluation.get("result") is True
                and last_evaluation.get("true_rule_ids")):
            checks["rule_selection"] = "TRUE"
        else:
            checks["rule_selection"] = "NO_TRUE_PROPOSAL"
            reasons.append("NO_ENABLED_RULE_RETURNED_TRUE")
        interface_reader = getattr(rules, "decision_interface_status", None) if rules is not None else None
        interface_status = interface_reader() if callable(interface_reader) else {"available": False}
        if interface_status.get("available") is True:
            checks["rule_to_intent_interface"] = "AVAILABLE"
        else:
            checks["rule_to_intent_interface"] = "UNAVAILABLE"
            reasons.append("RULE_EXECUTION_INTENT_STORE_UNAVAILABLE")
        rules_authorizer = getattr(rules, "execution_authorized", None) if rules is not None else None
        checks["financial_execution_authorization"] = (
            "AUTHORIZED" if callable(rules_authorizer) and rules_authorizer() is True else "BLOCKED"
        )
        settings = getattr(betting, "settings", None)
        if settings and bool(getattr(settings, "allow_real_placement", False)):
            checks["placement_configuration"] = "ENABLED"
        else:
            checks["placement_configuration"] = "DISABLED"
            reasons.append("live_placement_disabled_by_configuration")

        status = betting.status() if betting is not None else {}
        if status.get("mode") == "REAL" and status.get("automatic_enabled"):
            checks["real_session"] = "ACTIVE"
        else:
            checks["real_session"] = "INACTIVE"
            reasons.append("live_real_session_not_active")
        balance = status.get("current_balance")
        if isinstance(balance, (int, float)) and not isinstance(balance, bool) and math.isfinite(balance) and balance >= 0:
            checks["balance"] = "OBSERVED"
            stamp = status.get("last_balance_observed_at")
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(
                    str(stamp).replace("Z", "+00:00"))).total_seconds()
                if age < 0 or age > 20:
                    raise ValueError("stale balance")
            except (TypeError, ValueError):
                checks["balance_freshness"] = "STALE_OR_MISSING"
                reasons.append("real_balance_observation_stale_or_missing")
            else:
                checks["balance_freshness"] = "FRESH"
        else:
            checks["balance"] = "UNVERIFIED"
            checks["balance_freshness"] = "UNAVAILABLE"
            reasons.append("real_available_balance_unverified")

        # Re-read the owned account immediately before any possible action;
        # a prior UI snapshot is not current transaction authorization.
        account_observation = await self._read_platform_balance()
        if not isinstance(account_observation, dict):
            account_observation = {"verified": False, "fresh": False,
                                   "error": "balance_observation_unavailable"}
        account_currency = account_observation.get("currency")
        account_verified = bool(
            account_observation.get("verified") is True
            and account_observation.get("fresh") is True
            and account_observation.get("provider_identity")
            and account_observation.get("platform_session_identity")
            and account_currency
        )
        checks["account_readiness"] = "VERIFIED" if account_verified else "UNVERIFIED"
        if not account_verified:
            reasons.append("fresh_platform_balance_currency_and_session_identity_unverified")

        selected_panel = candidate.get("selected_panel")
        if type(selected_panel) is int and selected_panel in {0, 1}:
            checks["execution_panel"] = "SELECTED"
        else:
            checks["execution_panel"] = "MISSING"
            reasons.append("execution_panel_not_selected")
        stake = candidate.get("requested_stake")
        stake_currency = candidate.get("requested_stake_currency")
        if isinstance(stake, (int, float)) and not isinstance(stake, bool) and stake > 0:
            checks["stake"] = "VALID"
        else:
            checks["stake"] = "INVALID_OR_MISSING"
            reasons.append("execution_stake_missing_or_invalid")
        if account_verified and isinstance(stake_currency, str) and stake_currency == account_currency:
            checks["stake_currency"] = "MATCHES_VERIFIED_ACCOUNT"
        else:
            checks["stake_currency"] = "UNVERIFIED_OR_MISMATCHED"
            reasons.append("stake_currency_does_not_match_verified_account_currency")

        cashout_panels = account_observation.get("cashout_panels")
        cashout_panels = cashout_panels if isinstance(cashout_panels, list) else []
        cashout_panel = next((row for row in cashout_panels
                              if isinstance(row, dict) and row.get("panel") ==
                              (selected_panel + 1 if type(selected_panel) is int else -1)), None)
        try:
            multiplier = float(cashout_panel.get("displayed_value")) if cashout_panel else None
        except (TypeError, ValueError):
            multiplier = None
        panel_cashout_ready = bool(
            cashout_panel and cashout_panel.get("configuration_valid") is True
            and cashout_panel.get("auto_bet_enabled") is False
            and cashout_panel.get("auto_tab_selected") is True
            and cashout_panel.get("auto_cashout_enabled") is True
            and multiplier == 2.0
            and cashout_panel.get("platform_acknowledged") is True
        )
        checks["cashout_readiness"] = "VERIFIED" if panel_cashout_ready else "UNVERIFIED"
        if not panel_cashout_ready:
            reasons.append("selected_panel_cashout_configuration_or_platform_acknowledgement_unverified")

        controls = account_observation.get("betting_controls")
        controls = controls if isinstance(controls, dict) else {}
        control_panels = controls.get("panels")
        control_panels = control_panels if isinstance(control_panels, list) else []
        panel_state = next((row for row in control_panels
                            if isinstance(row, dict) and row.get("slot") == selected_panel), None)
        if panel_state and panel_state.get("pending_bet_state") == "CLEAR":
            checks["selected_panel_bet_state"] = "CLEAR"
        else:
            checks["selected_panel_bet_state"] = "UNKNOWN_OR_PENDING"
            reasons.append("selected_panel_active_or_pending_bet_not_clear")
        betting_window = controls.get("betting_window")
        betting_window = betting_window if isinstance(betting_window, dict) else {}
        checks["betting_window"] = str(betting_window.get("state") or "UNKNOWN").upper()
        if checks["betting_window"] != "BETTING_OPEN":
            reasons.append("verified_betting_window_not_open")

        target_ref = candidate.get("target_round_ref") or {}
        source_ref = target_ref.get("expectedImmediateSuccessorOf") if isinstance(target_ref, dict) else None
        source_id = candidate.get("source_round_id")
        source_stream = (source_ref or {}).get("streamId")
        source_ref_id = (source_ref or {}).get("roundId")
        source_index = (source_ref or {}).get("roundIndex")
        target_stream = candidate.get("target_stream_id")
        target_index = candidate.get("target_round_index")
        internal_binding_valid = bool(
            source_id and source_id == source_ref_id and source_stream
            and source_stream == target_stream
            and type(source_index) is int and type(target_index) is int
            and target_index == source_index + 1
        )
        checks["source_target_binding"] = "BOUND_PROSPECTIVE" if internal_binding_valid else "UNAVAILABLE"
        if not internal_binding_valid:
            reasons.append("source_and_prospective_target_reference_mismatch")
        eligibility = str(candidate.get("execution_eligibility") or "").upper()
        execution_status = str(candidate.get("execution_status") or "").upper()
        if eligibility == "EXPIRED" or execution_status == "EXPIRED":
            checks["target_identity"] = "EXPIRED"
            reasons.append("target_round_already_completed_intent_expired")
        elif candidate.get("provider_target_round_id"):
            checks["target_identity"] = "VERIFIED"
        else:
            checks["target_identity"] = "UNVERIFIED"
            reasons.append("provider_target_round_identity_unverified")
        deadline = candidate.get("deadline_evidence") or {}
        if not isinstance(deadline, dict):
            deadline = {}
        deadline_at = deadline.get("deadline_at")
        try:
            deadline_time = datetime.fromisoformat(str(deadline_at).replace("Z", "+00:00"))
            if deadline_time.tzinfo is None:
                deadline_time = deadline_time.replace(tzinfo=timezone.utc)
            remaining = (deadline_time - datetime.now(timezone.utc)).total_seconds()
        except (TypeError, ValueError):
            remaining = None
        if deadline.get("status") == "VERIFIED" and remaining is not None and remaining > 0:
            checks["target_deadline"] = "VERIFIED"
        elif remaining is not None and remaining <= 0:
            checks["target_deadline"] = "EXPIRED"
            reasons.append("target_betting_deadline_expired")
        else:
            checks["target_deadline"] = "UNKNOWN"
            reasons.append("target_betting_deadline_unverified")
        # Optional bankroll limits are not part of executor authorization.
        # The following checks independently enforce transaction integrity.
        checks["optional_bankroll_policy"] = "NOT_APPLIED"
        checks["execution_integrity"] = "CHECKED"
        from app.live import REAL_MONEY_EXECUTION_ENABLED
        if not REAL_MONEY_EXECUTION_ENABLED:
            checks["real_money_execution"] = "DISABLED_PENDING_LIVE_QUALIFICATION"
            reasons.append("real_money_execution_disabled_pending_live_qualification")

        repository = getattr(execution_store, "execution_for", None)
        execution_repository = getattr(execution_store, "open_executions", None)
        intent_id = candidate.get("decision_id")
        target_id = candidate.get("target_round_id") or candidate.get("round_id")
        current_reservation = None
        if execution_id and execution_store is not None:
            try:
                candidate = execution_store.execution_by_id(str(execution_id))
                if (candidate and candidate.get("execution_id") == execution_id
                        and candidate.get("decision_id") == intent_id
                        and candidate.get("target_round_id") == target_id
                        and candidate.get("status") == "VALIDATING"
                        and candidate.get("lifecycle_state") == "QUEUED"):
                    current_reservation = candidate
            except Exception:
                current_reservation = None
        if callable(execution_repository):
            try:
                open_rows = execution_repository()
                unresolved = [row for row in open_rows if not current_reservation
                              or row.get("execution_id") != execution_id]
                if unresolved:
                    checks["unresolved_executions"] = "BLOCKED"
                    reasons.append("unresolved_real_execution_requires_reconciliation")
                else:
                    checks["unresolved_executions"] = "CLEAR"
            except Exception:
                checks["unresolved_executions"] = "UNAVAILABLE"
                reasons.append("open_execution_state_unavailable")
        else:
            checks["unresolved_executions"] = "UNAVAILABLE"
            reasons.append("open_execution_state_unavailable")
        if callable(repository) and intent_id and target_id:
            existing = repository(str(intent_id), str(target_id))
            if (current_reservation is not None and existing is not None
                    and existing.get("execution_id") == execution_id):
                checks["duplicate_protection"] = "CURRENT_RESERVATION"
            elif existing:
                checks["duplicate_protection"] = "DUPLICATE"
                reasons.append("duplicate_execution_already_recorded")
            else:
                checks["duplicate_protection"] = "NO_MATCH"
        else:
            checks["duplicate_protection"] = "UNAVAILABLE"
            reasons.append("duplicate_execution_store_unavailable")

        backend = getattr(betting, "backend", None)
        capabilities = backend.verified_capabilities() if backend and callable(
            getattr(backend, "verified_capabilities", None)) else {}
        required_capabilities = ("bet_submission", "target_identity", "betting_deadline",
                                 "cashout_acknowledgement", "acceptance_receipt",
                                 "settlement_receipt", "currency_validation")
        missing_capabilities = [name for name in required_capabilities if capabilities.get(name) is not True]
        checks["platform_acceptance_and_settlement"] = "VERIFIED" if not missing_capabilities else "UNVERIFIED"
        reasons.extend(f"platform_capability_unverified:{name}" for name in missing_capabilities)
        selected_panel = candidate.get("selected_panel")
        if (durable_rule_intent and type(selected_panel) is int and selected_panel in {0, 1}
                and target_stream and type(target_index) is int):
            checks["decision_queue_lock"] = "DURABLE_TARGET_PANEL_UNIQUE"
        else:
            checks["decision_queue_lock"] = "UNAVAILABLE"
            reasons.append("durable_target_panel_execution_lock_unavailable")

        unique_reasons = list(dict.fromkeys(reasons))
        return {"allowed": False if unique_reasons else True, "reasons": unique_reasons,
                "checks": checks, "decision": "NO_ACTION" if unique_reasons else "AUTHORIZED",
                "automatic_execution": "OFF" if unique_reasons else "ON",
                "checked_at": _now()}

    async def handle_execution_failure(self, reason: str) -> None:
        await self.force_safe(f"pre_bet_failure:{reason}")
