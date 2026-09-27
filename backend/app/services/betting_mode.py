"""Backend-owned, mutually exclusive SHADOW_REALISTIC/LIVE_REAL mode switch."""
from __future__ import annotations

import asyncio
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
        self.transitioning = False
        self.reason = "restored from persistence; LIVE requires fresh authorization" if runtime or persisted else "safe startup mode"
        self.shadow_ready = False
        self.last_change = _now()
        self.recovered_live = bool(persisted and persisted.get("mode") == LIVE_REAL)
        self._lock = asyncio.Lock()
        self._reviews: dict[str, dict[str, Any]] = {}
        self._persist("startup_safe_mode", previous_mode=(persisted or {}).get("mode"))

    def _persist(self, reason: str, **extra) -> None:
        row = {"mode": self.mode, "updated_at": _now(), "reason": reason,
               "live_authorized": self.live_authorized,
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
        """Read the existing CDP-owned page; never launch or create a browser."""
        from app.betting.browser_client import AviatorBrowserClient

        client = AviatorBrowserClient(self.app.state.betting.settings)
        try:
            snapshot = await client.snapshot()
            payload = snapshot.as_dict() if hasattr(snapshot, "as_dict") else snapshot
            balance = payload.get("balance") if isinstance(payload, dict) else None
            return {
                "verified": bool(payload and payload.get("ok") and balance is not None),
                "balance": balance if isinstance(balance, (int, float)) else None,
                "balance_text": payload.get("balance_text") if isinstance(payload, dict) else None,
                "ui_ready": bool(payload.get("ui_ready")) if isinstance(payload, dict) else False,
                "snapshot_ok": bool(payload and payload.get("ok")),
                "browser_status": "CONNECTED" if payload and payload.get("ok") else "UNAVAILABLE",
                "observed_at": _now(),
                "source": "existing_cdp_page_read_only",
                "error": None if payload and payload.get("ok") else (payload or {}).get("error", "balance unavailable"),
            }
        except Exception as exc:
            return {"verified": False, "balance": None, "balance_text": None,
                    "browser_status": "UNAVAILABLE", "observed_at": _now(),
                    "source": "existing_cdp_page_read_only", "error": str(exc)}
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
            "minimum_bet": risk_profile.get("minimum_bet", betting_profile.min_amount_bif),
            "maximum_bet": risk_profile.get("maximum_bet", betting_profile.max_amount_bif),
            "session_loss_limit": risk_profile.get("maximum_session_loss", betting_profile.max_loss_bif),
        }

    async def live_review(self, *, profile: str, goal_balance: float | None) -> dict:
        profile_data = self._profile_review(profile)
        platform = await self._read_platform_balance()
        goal = float(goal_balance) if goal_balance is not None else None
        blocks = []
        if not platform["verified"]:
            blocks.append(f"real_platform_balance_unverified:{platform.get('error') or 'UNKNOWN'}")
        elif goal is not None and goal <= float(platform["balance"]):
            blocks.append("goal_must_exceed_real_platform_balance")
        try:
            from app.core.config import get_settings
            api_key_configured = bool(get_settings().api_key)
        except Exception:
            api_key_configured = False
        if not api_key_configured:
            blocks.append("backend_api_key_required_for_live_authorization")
        live = self.app.state.live
        readiness = await live.production_readiness_check(platform)
        blocks.extend(readiness.get("reasons", []))
        review_id = secrets.token_urlsafe(32)
        row = {
            "review_id": review_id, "created_monotonic": time.monotonic(),
            "expires_monotonic": time.monotonic() + 120,
            "platform_balance": platform, "profile": profile_data,
            "goal_balance": goal, "readiness": readiness,
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
                                    "maximum": profile_data["maximum_bet"]},
            "session_loss_limit": profile_data["session_loss_limit"],
            "goal_balance": goal, "readiness": readiness,
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

    async def switch_shadow(self, *, profile: str = "PROFILE_A", goal_balance: float | None = None) -> dict:
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
                await self.app.state.risk.on_session_start(profile)
                await shadow.start(profile=profile, starting_balance=float(platform["balance"]),
                                   goal_balance=goal_balance, health=self.app.state.system_health)
                self.mode = SHADOW_REALISTIC
                self.live_authorized = False
                self.shadow_ready = True
                self.reason = "shadow realistic session active; platform balance was read-only"
                self._persist(self.reason, starting_balance=platform["balance"], profile=profile,
                              goal_balance=goal_balance)
                await self._broadcast()
                return {"ok": True, "status": self.status(), "platform_balance": platform}
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

    async def switch_live(self, payload: dict[str, Any]) -> dict:
        async with self._lock:
            review_id = str(payload.get("review_id") or "")
            review = self._reviews.pop(review_id, None)
            if not review or review["expires_monotonic"] < time.monotonic():
                return {"ok": False, "error": "fresh_backend_live_review_required",
                        "reasons": ["live_review_missing_expired_or_already_used"]}
            if payload.get("confirmation") != "ENABLE LIVE BETTING":
                return {"ok": False, "error": "explicit_confirmation_required",
                        "reasons": ["type_ENABLE_LIVE_BETTING_after_review"]}
            profile = str(payload.get("profile") or "").upper()
            if profile != review["profile"]["profile"]:
                return {"ok": False, "error": "live_review_mismatch", "reasons": ["profile_changed_after_review"]}
            expected_values = {
                "real_platform_balance": review["platform_balance"].get("balance"),
                "cashout_target": review["profile"]["cashout_target"],
                "minimum_bet": review["profile"]["minimum_bet"],
                "maximum_bet": review["profile"]["maximum_bet"],
                "session_loss_limit": review["profile"]["session_loss_limit"],
                "goal_balance": review["goal_balance"],
            }
            for key, expected in expected_values.items():
                if payload.get(key) != expected:
                    return {"ok": False, "error": "live_review_mismatch",
                            "reasons": [f"{key}_does_not_match_backend_review"]}
            if not review["platform_balance"].get("verified"):
                return {"ok": False, "error": "platform_balance_unverified",
                        "reasons": ["real_platform_balance_unverified"]}
            if review["goal_balance"] is None or review["goal_balance"] <= review["platform_balance"]["balance"]:
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
            readiness = await self.app.state.live.production_readiness_check(platform_now)
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
                    "goal_balance": expected_values["goal_balance"],
                    "_platform_observation": platform_now,
                }, _mode_authorization=LIVE_MODE_AUTHORIZATION)
                if not result.get("ok"):
                    self.live_authorized = False
                    self.app.state.live.explicit_live_authorization = False
                    return result
                self.mode = LIVE_REAL
                self.shadow_ready = False
                self.reason = "explicit live authorization active"
                self._persist(self.reason, profile=profile, review_id=review_id)
                await self._broadcast()
                return {"ok": True, "status": self.status()}
            except Exception as exc:
                self.live_authorized = False
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
            if self.mode != LIVE_REAL and not self.live_authorized:
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

    async def final_execution_gate(self, intent) -> dict[str, Any]:
        reasons = []
        if not self.can_execute_live():
            reasons.append("mode_is_not_authorized_live_real")
        decision = self.app.state.decision_engine.current()
        if (not decision or decision.get("status") != "READY_FOR_EXECUTION"
                or decision.get("decision_id") != intent.decision_id
                or str(decision.get("target_round_id")) != str(intent.round_id)
                or decision.get("profile") != intent.profile):
            reasons.append("decision_not_current_backend_approved_decision")
            decision = decision or {}
        health = self.app.state.system_health.can_bet_now(mode="REAL")
        if not health.get("allowed"):
            reasons.extend(health.get("reasons", []) or ["CanBetNow_blocked"])
        betting_status = self.app.state.betting.status()
        reasons.extend(self.app.state.live.can_execute_live_bet(
            decision, betting_status).get("reasons", []))
        try:
            reasons.extend(self.app.state.risk.validate_execution(decision, betting_status))
        except Exception as exc:
            reasons.append(f"risk_validation_failed:{type(exc).__name__}")
        risk = decision.get("risk_evaluation") or decision.get("risk") or {}
        approved = risk.get("approved_bet", risk.get("approved_bet_amount"))
        if approved is None or int(approved) != int(intent.effective_amount()):
            reasons.append("stake_does_not_match_approved_risk_amount")
        approved_cashout = risk.get("cashout", risk.get("cashout_target"))
        if approved_cashout is None or float(intent.cashout) != float(approved_cashout):
            reasons.append("cashout_does_not_match_approved_profile")
        unique_reasons = list(dict.fromkeys(reasons))
        if unique_reasons:
            await self.force_safe("pre_bet_gate_blocked:" + ";".join(unique_reasons))
        return {"allowed": not unique_reasons, "reasons": unique_reasons,
                "checked_at": _now()}

    async def handle_execution_failure(self, reason: str) -> None:
        await self.force_safe(f"pre_bet_failure:{reason}")
