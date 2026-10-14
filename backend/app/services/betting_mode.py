"""Backend-owned, mutually exclusive SHADOW_REALISTIC/LIVE_REAL mode switch."""
from __future__ import annotations

import asyncio
import math
import secrets
import time
from datetime import datetime, timezone
from typing import Any

from app.betting.profiles import get_profile


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
               "selected_profile": getattr(getattr(self.app.state, "risk", None),
                                            "selected_profile", "PROFILE_A"),
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
        return {
            "mode": self.mode,
            "badge": "LIVE — REAL MONEY" if self.mode == LIVE_REAL else "SHADOW — NO REAL MONEY",
            "live_authorized": self.live_authorized and bool(live and live.live_active),
            "operator_identity": self.operator_identity if self.live_authorized else None,
            "transitioning": self.transitioning,
            "shadow_running": bool(shadow and shadow.mode == "SHADOW" and self.shadow_ready),
            "shadow": shadow.status() if shadow else None,
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
            verified = bool(payload and payload.get("ok") and numeric_balance
                            and authentication_state == "AUTHENTICATED")
            live_status = getattr(getattr(self.app.state, "live", None), "status", lambda: {})()
            live_session = (live_status or {}).get("session") or {}
            rules_status = getattr(getattr(self.app.state, "rules_engine", None), "status", lambda: {})()
            rules_session = (rules_status or {}).get("active_session") or {}
            application_session_id = (live_session.get("session_id") or rules_session.get("session_id"))
            platform_session_identity = (payload or {}).get("session_identity")
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
                "currency": payload.get("balance_currency") if isinstance(payload, dict) else None,
                "currency_verified": bool(payload and payload.get("balance_currency")),
                "balance_text": payload.get("balance_text") if isinstance(payload, dict) else None,
                "ui_ready": bool(payload.get("ui_ready")) if isinstance(payload, dict) else False,
                "snapshot_ok": bool(payload and payload.get("ok")),
                "browser_status": "CONNECTED" if payload and payload.get("ok") else "UNAVAILABLE",
                "observed_at": (payload or {}).get("balance_observed_at") or _now(),
                "source": "existing_cdp_page_read_only",
                "provider_identity": (payload or {}).get("provider_identity"),
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
                    "observed_at": (payload or {}).get("balance_observed_at") or _now(),
                },
                "error": None if verified else (payload or {}).get("error") or
                    ("authenticated_account_not_verified" if authentication_state != "AUTHENTICATED"
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

    def _profile_review(self, profile_key: str) -> dict[str, Any]:
        betting_profile = get_profile(profile_key)
        risk_profiles = self.app.state.risk.profiles()
        risk_profile = risk_profiles.get(betting_profile.key, {})
        return {
            "profile": betting_profile.key,
            "profile_name": risk_profile.get("name", betting_profile.label),
            "cashout_target": float(risk_profile.get("cashout", betting_profile.base_target)),
            "default_stake": int(betting_profile.default_amount_bif),
            "minimum_bet": risk_profile.get("minimum_bet", betting_profile.min_amount_bif),
            "maximum_bet": risk_profile.get("maximum_bet", betting_profile.max_amount_bif),
            "session_loss_limit": risk_profile.get("maximum_session_loss", betting_profile.max_loss_bif),
            "maximum_balance_percentage": risk_profile.get("maximum_balance_percentage"),
            "maximum_consecutive_losses": risk_profile.get("maximum_consecutive_losses"),
        }

    async def live_review(self, *, profile: str, win_limit_enabled: bool | None,
                          win_limit: float | None, session_loss_limit_enabled: bool | None,
                          session_loss_limit: float | None,
                          execution_panel: int | None = None,
                          stake_amount: int | None = None,
                          currency: str | None = None) -> dict:
        profile_data = self._profile_review(profile)
        platform = await self._read_platform_balance()
        blocks = []
        if profile_data["cashout_target"] != 2.0:
            blocks.append("LIVE profile cashout must equal the required 2.00x")
        if type(execution_panel) is not int or execution_panel not in (0, 1):
            blocks.append("choose_a_live_execution_panel")
        if type(stake_amount) is not int or stake_amount <= 0:
            blocks.append("choose_a_positive_live_stake")
        elif not profile_data["minimum_bet"] <= stake_amount <= profile_data["maximum_bet"]:
            blocks.append("live_stake_outside_profile_limits")
        if not isinstance(currency, str) or not currency:
            blocks.append("choose_live_account_currency")
        elif currency != platform.get("currency"):
            blocks.append("live_stake_currency_does_not_match_verified_account")
        if win_limit_enabled is None:
            blocks.append("choose_whether_to_enable_a_win_limit")
        if session_loss_limit_enabled is None:
            blocks.append("choose_whether_to_enable_a_loss_limit")
        win_amount = float(win_limit) if win_limit is not None else None
        loss_amount = float(session_loss_limit) if session_loss_limit is not None else None
        if win_limit_enabled and (win_amount is None or not math.isfinite(win_amount) or win_amount <= 0):
            blocks.append("enabled_win_limit_requires_a_positive_profit_amount")
            win_amount = None
        if session_loss_limit_enabled and (loss_amount is None or not math.isfinite(loss_amount) or loss_amount <= 0):
            blocks.append("enabled_loss_limit_requires_a_positive_amount")
            loss_amount = None
        goal = (float(platform["balance"]) + win_amount
                if platform["verified"] and win_limit_enabled and win_amount is not None
                and math.isfinite(win_amount) and win_amount > 0 else None)
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
            "stake_amount": stake_amount,
            "currency": currency,
        })
        blocks.extend(readiness.get("reasons", []))
        review_id = secrets.token_urlsafe(32)
        row = {
            "review_id": review_id, "created_monotonic": time.monotonic(),
            "expires_monotonic": time.monotonic() + 120,
            "platform_balance": platform, "profile": profile_data,
            "goal_balance": goal, "win_limit_enabled": bool(win_limit_enabled),
            "win_limit": win_amount if win_limit_enabled else None,
            "session_loss_limit_enabled": bool(session_loss_limit_enabled),
            "session_loss_limit": loss_amount if session_loss_limit_enabled else None,
            "readiness": readiness,
            "execution_panel": execution_panel,
            "stake_amount": stake_amount,
            "currency": currency,
        }
        self._reviews[review_id] = row
        # Keep challenge memory bounded and never restore it after restart.
        for key, review in list(self._reviews.items()):
            if review["expires_monotonic"] < time.monotonic():
                self._reviews.pop(key, None)
        return {
            "ok": True, "mode": self.mode, "live_available": not blocks,
            "platform_balance": platform, "profile": profile_data,
            "cashout_target": profile_data["cashout_target"],
                "approved_bet_limits": {"minimum": profile_data["minimum_bet"],
                                    "maximum": profile_data["maximum_bet"],
                                    "default_stake": profile_data["default_stake"]},
            "session_loss_limit": row["session_loss_limit"],
            "session_loss_limit_enabled": row["session_loss_limit_enabled"],
            "win_limit": row["win_limit"],
            "win_limit_enabled": row["win_limit_enabled"],
            "goal_balance": goal, "readiness": readiness,
            "execution_panel": execution_panel,
            "stake_amount": stake_amount,
            "currency": currency,
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

    async def switch_shadow(self, *, profile: str = "PROFILE_A", goal_balance: float | None = None,
                            starting_balance: float | None = None,
                            configuration: dict[str, Any] | None = None) -> dict:
        if not isinstance(configuration, dict) or str(configuration.get("mode", "")).upper() not in {"MANUAL", "AUTOMATIC"}:
            return {"ok": False, "error": "session_configuration_required",
                    "reasons": ["Choose MANUAL or AUTOMATIC and review the two-panel configuration before starting"]}
        async with self._lock:
            self.transitioning = True
            self.reason = "stopping current mode and reconciling pending executions"
            await self._broadcast()
            try:
                await self._stop_live("mode_switch_to_shadow")
                pending = await self._settle_real_executions()
                if pending:
                    self.mode = SHADOW_REALISTIC
                    self.shadow_ready = False
                    self.reason = "live execution remains unresolved; shadow ledger is paused"
                    self._persist(self.reason, pending_execution_ids=[x.get("execution_id") for x in pending])
                    await self._broadcast()
                    return {"ok": False, "error": "reconciliation_required",
                            "reasons": [f"unresolved_execution:{x.get('execution_id')}:{x.get('status')}" for x in pending],
                            "status": self.status()}
                shadow = self.app.state.shadow
                if shadow._pending:
                    rows = {str(row["round_id"]): row for row in self.app.state.history_collector.rows()}
                    for target, trade in list(shadow._pending.items()):
                        if target in rows:
                            await shadow.reconcile_round(rows[target])
                if shadow._pending:
                    self.shadow_ready = False
                    self.reason = "shadow round remains pending; mode switch waits for its actual result"
                    self._persist(self.reason, pending_shadow_rounds=list(shadow._pending))
                    await self._broadcast()
                    return {"ok": False, "error": "shadow_round_pending",
                            "reasons": [f"shadow_result_pending:{x}" for x in shadow._pending],
                            "status": self.status()}
                if shadow.mode in {"SHADOW", "PAUSED"}:
                    await shadow.stop("mode_switch")
                platform = await self._read_platform_balance()
                if not platform["verified"]:
                    self.mode = SHADOW_REALISTIC
                    self.shadow_ready = False
                    self.reason = f"read-only platform balance unavailable: {platform.get('error') or 'UNKNOWN'}"
                    self._persist(self.reason)
                    await self._broadcast()
                    return {"ok": False, "error": "platform_balance_unverified",
                            "reasons": [self.reason], "status": self.status()}
                if starting_balance is not None and not math.isclose(
                        float(starting_balance), float(platform["balance"]), rel_tol=0, abs_tol=0.01):
                    self.reason = "entered starting balance does not match the verified platform balance"
                    self._persist(self.reason)
                    await self._broadcast()
                    return {"ok": False, "error": "starting_balance_mismatch",
                            "reasons": [self.reason], "status": self.status(),
                            "platform_balance": platform}
                if goal_balance is not None and float(goal_balance) <= float(platform["balance"]):
                    return {"ok": False, "error": "invalid_goal",
                            "reasons": ["goal must exceed the verified starting balance"],
                            "platform_balance": platform}
                await self.app.state.risk.on_session_start(profile)
                await shadow.start(profile=profile, starting_balance=float(platform["balance"]),
                                   goal_balance=goal_balance, health=self.app.state.system_health,
                                   configuration=configuration)
                self.mode = SHADOW_REALISTIC
                self.live_authorized = False
                self.shadow_ready = True
                self.reason = "shadow realistic session active; platform balance was read-only"
                self._persist(self.reason, starting_balance=platform["balance"], profile=profile,
                              goal_balance=(shadow.session or {}).get("goal_balance"),
                              session_configuration=(shadow.session or {}).get("config"))
                await self._broadcast()
                return {"ok": True, "status": self.status(), "platform_balance": platform}
            except ValueError as exc:
                self.mode = SHADOW_REALISTIC
                self.live_authorized = False
                self.shadow_ready = False
                self.reason = f"session_configuration_invalid:{exc}"
                self._persist(self.reason)
                await self._broadcast()
                return {"ok": False, "error": "session_configuration_invalid",
                        "reasons": [str(exc)], "status": self.status()}
            except Exception as exc:
                self.mode = SHADOW_REALISTIC
                self.live_authorized = False
                self.shadow_ready = False
                self.reason = f"mode_switch_failed:{type(exc).__name__}:{exc}"
                try:
                    self._persist(self.reason)
                    await self._broadcast()
                except Exception:
                    pass
                return {"ok": False, "error": "mode_switch_failed", "reasons": [self.reason],
                        "status": self.status()}
            finally:
                self.transitioning = False

    async def switch_live(self, payload: dict[str, Any], *, operator_identity: str | None = None) -> dict:
        if payload.get("configuration") or payload.get("session_configuration"):
            return {"ok": False, "error": "two_panel_live_configuration_not_qualified",
                    "reasons": ["Run and verify the MANUAL and AUTOMATIC configurations in SHADOW_REALISTIC before using them in LIVE_REAL"]}
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
            profile = str(payload.get("profile") or "").upper()
            if profile != review["profile"]["profile"]:
                return {"ok": False, "error": "live_review_mismatch", "reasons": ["profile_changed_after_review"]}
            expected_values = {
                "real_platform_balance": review["platform_balance"].get("balance"),
                "cashout_target": review["profile"]["cashout_target"],
                "minimum_bet": review["profile"]["minimum_bet"],
                "maximum_bet": review["profile"]["maximum_bet"],
                "session_loss_limit_enabled": review["session_loss_limit_enabled"],
                "session_loss_limit": review["session_loss_limit"],
                "win_limit_enabled": review["win_limit_enabled"],
                "win_limit": review["win_limit"],
                "goal_balance": review["goal_balance"],
                "execution_panel": review["execution_panel"],
                "stake_amount": review["stake_amount"],
                "currency": review["currency"],
            }
            for key, expected in expected_values.items():
                if payload.get(key) != expected:
                    return {"ok": False, "error": "live_review_mismatch",
                            "reasons": [f"{key}_does_not_match_backend_review"]}
            if not review["platform_balance"].get("verified"):
                return {"ok": False, "error": "platform_balance_unverified",
                        "reasons": ["real_platform_balance_unverified"]}
            if (review["win_limit_enabled"] and
                    (review["goal_balance"] is None or review["goal_balance"] <= review["platform_balance"]["balance"])):
                return {"ok": False, "error": "goal_required",
                        "reasons": ["goal_must_exceed_real_platform_balance"]}
            # Run all production checks before stopping an existing safe shadow
            # session; a rejected LIVE request must leave Shadow intact.
            platform_now = await self._read_platform_balance()
            if not platform_now.get("verified"):
                return {"ok": False, "error": "platform_balance_unverified",
                        "reasons": [f"real_platform_balance_unverified:{platform_now.get('error') or 'UNKNOWN'}"]}
            reviewed_balance = review["platform_balance"].get("balance")
            if abs(float(platform_now["balance"]) - float(reviewed_balance)) > 0.01:
                return {"ok": False, "error": "live_review_stale",
                        "reasons": ["real_platform_balance_changed_after_review"]}
            readiness = await self.app.state.live.production_readiness_check(platform_now, {
                "execution_panel": review["execution_panel"],
                "stake_amount": review["stake_amount"],
                "currency": review["currency"],
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
                await self.app.state.risk.on_session_start(profile)
                self.live_authorized = True
                self.app.state.live.explicit_live_authorization = True
                result = await self.app.state.live.start({
                    **payload, "profile": profile, "starting_balance": expected_values["real_platform_balance"],
                    "operator_identity": operator_identity,
                    "goal_balance": expected_values["goal_balance"],
                    "session_loss_limit_enabled": expected_values["session_loss_limit_enabled"],
                    "session_loss_limit": expected_values["session_loss_limit"],
                    "win_limit_enabled": expected_values["win_limit_enabled"],
                    "win_limit": expected_values["win_limit"],
                    "execution_panel": expected_values["execution_panel"],
                    "stake_amount": expected_values["stake_amount"],
                    "currency": expected_values["currency"],
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
                self._persist(self.reason, profile=profile, review_id=review_id,
                              operator_identity=operator_identity)
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
        return self.mode == LIVE_REAL and self.live_authorized and not self.transitioning

    async def final_execution_gate(self, intent, *, execution_id: str | None = None) -> dict[str, Any]:
        rules = getattr(self.app.state, "rules_engine", None)
        rules_status = rules.status() if rules is not None else {}
        last_evaluation = getattr(rules, "last_evaluation", None) if rules is not None else None
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
        rules_authorizer = getattr(rules, "execution_authorized", None) if rules is not None else None
        if callable(rules_authorizer) and rules_authorizer() is True:
            checks["rule_to_intent_interface"] = "AUTHORIZED"
        else:
            checks["rule_to_intent_interface"] = "UNAVAILABLE"
            reasons.append("RULE_EXECUTION_INTERFACE_NOT_AUTHORIZED")
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

        # DecisionIntent currently carries only a target round ID. The rules
        # ledger carries a prospective stream/index reference. No verified
        # provider ID bridges these two contracts, so execution must stay shut.
        checks["source_target_binding"] = "UNAVAILABLE"
        reasons.append("rule_selection_is_not_bound_to_exact_source_and_provider_target_identity")
        checks["risk_approval"] = "UNAVAILABLE"
        if risk is not None:
            try:
                if hasattr(intent, "as_dict"):
                    candidate = intent.as_dict()
                elif isinstance(intent, dict):
                    candidate = dict(intent)
                else:
                    candidate = {}
                risk_reasons = risk.validate_execution(candidate, status)
                if not risk_reasons:
                    checks["risk_approval"] = "APPROVED"
                else:
                    reasons.extend(f"risk:{reason}" for reason in risk_reasons)
            except Exception as exc:
                reasons.append(f"risk_validation_failed:{type(exc).__name__}")
        else:
            reasons.append("risk_manager_unavailable")

        execution_store = getattr(getattr(self.app.state, "wp", None), "repository", None)
        repository = getattr(execution_store, "execution_for", None)
        execution_repository = getattr(execution_store, "open_executions", None)
        intent_id = getattr(intent, "decision_id", None) if not isinstance(intent, dict) else intent.get("decision_id")
        target_id = getattr(intent, "round_id", None) if not isinstance(intent, dict) else intent.get("round_id")
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
        required_capabilities = ("target_identity", "betting_deadline", "acceptance_receipt",
                                 "settlement_receipt", "currency_rwf")
        missing_capabilities = [name for name in required_capabilities if capabilities.get(name) is not True]
        checks["platform_acceptance_and_settlement"] = "VERIFIED" if not missing_capabilities else "UNVERIFIED"
        reasons.extend(f"platform_capability_unverified:{name}" for name in missing_capabilities)
        checks["decision_queue_lock"] = "UNAVAILABLE"
        reasons.append("durable target-and-panel execution lock is not configured")

        unique_reasons = list(dict.fromkeys(reasons))
        return {"allowed": False if unique_reasons else True, "reasons": unique_reasons,
                "checks": checks, "decision": "NO_ACTION" if unique_reasons else "AUTHORIZED",
                "automatic_execution": "OFF" if unique_reasons else "ON",
                "checked_at": _now()}

    async def handle_execution_failure(self, reason: str) -> None:
        await self.force_safe(f"pre_bet_failure:{reason}")
