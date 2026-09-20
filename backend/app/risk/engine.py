"""Auditable risk gate. It classifies risk; it never predicts a round."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable, Optional

from app.betting.schemas import DecisionIntent
from app.risk.config import RISK_PROFILES, get_risk_profile
from app.risk.schemas import RiskDecision, RiskLevel
from app.risk.sizing import BetSizing

log = logging.getLogger("risk.engine")
Broadcaster = Callable[[dict], Awaitable[None]]


class RiskManager:
    def __init__(self, *, broadcaster: Optional[Broadcaster] = None,
                 betting_manager=None, audit_cap: int = 500):
        self.broadcaster = broadcaster
        self.betting_manager = betting_manager
        self.sizing = BetSizing()
        self.selected_profile = "PROFILE_A"
        self.emergency_latched = False
        self.audit_cap = audit_cap
        self.audit_log: list[dict] = []
        self._evaluated: set[tuple[str, str]] = set()
        self._lock = asyncio.Lock()
        self.last_evaluation: Optional[dict] = None

    async def _emit(self, event: str, **payload) -> None:
        if self.broadcaster:
            await self.broadcaster({"type": event, **payload})

    async def select_profile(self, key: str) -> dict:
        profile = get_risk_profile(key)
        self.selected_profile = profile.key
        await self._emit("risk:profile_changed", profile=profile.public())
        return profile.public()

    async def on_session_start(self, profile: str) -> None:
        await self.select_profile(profile)
        self._evaluated.clear()

    async def emergency_stop(self) -> None:
        self.emergency_latched = True
        await self._emit("risk:emergency_stop", reason="Emergency stop is latched")

    async def reset_emergency(self) -> None:
        self.emergency_latched = False
        await self._emit("risk:evaluated", approved=False,
                         reason="Emergency stop reset; awaiting a decision")

    def profiles(self) -> dict:
        return {key: value.public() for key, value in RISK_PROFILES.items()}

    def _record(self, result: RiskDecision) -> dict:
        row = result.public()
        self.audit_log.append(row)
        self.audit_log = self.audit_log[-self.audit_cap:]
        self.last_evaluation = row
        log.info("[RISK] decision=%s round=%s approved=%s level=%s reason=%s",
                 result.decision_id, result.round_id, result.approved,
                 result.risk_level, result.reason)
        return row

    async def _finish(self, result: RiskDecision, event: str | None = None,
                      stop_reason: str | None = None) -> RiskDecision:
        payload = self._record(result)
        await self._emit("risk:evaluated", evaluation=payload)
        await self._emit(event or ("risk:approved" if result.approved else "risk:rejected"),
                         evaluation=payload)
        if stop_reason and self.betting_manager:
            try:
                await self.betting_manager.stop_session(emergency=False,
                                                        reason=stop_reason)
            except Exception:
                log.debug("session already stopped", exc_info=True)
        return result

    def status(self, betting_status: Optional[dict] = None) -> dict:
        bs = betting_status or {}
        profile = get_risk_profile(self.selected_profile)
        start = bs.get("starting_balance")
        current = bs.get("current_balance")
        if current is None:
            current = bs.get("last_balance")
        profit = None if start is None or current is None else current - start
        loss = 0 if profit is None or profit >= 0 else abs(profit)
        return {
            "selected_profile": profile.key,
            "profile_name": profile.name,
            "cashout": profile.cashout,
            "minimum_bet": profile.minimum_bet,
            "maximum_bet": profile.maximum_bet,
            "maximum_balance_percentage": profile.maximum_balance_percentage,
            "maximum_session_loss": profile.maximum_session_loss,
            "maximum_consecutive_losses": profile.maximum_consecutive_losses,
            "starting_balance": start,
            "current_balance": current,
            "available_balance": current,
            "profit_loss": profit,
            "session_loss": loss,
            "total_bet_amount": sum(x["approved_bet"] for x in self.audit_log if x["approved"]),
            "consecutive_losses": bs.get("consecutive_losses", 0),
            "current_bet_size": (self.last_evaluation or {}).get("approved_bet", 0),
            "risk_level": (self.last_evaluation or {}).get("risk_level", "BLOCKED"),
            "risk_status": "APPROVED" if (self.last_evaluation or {}).get("approved") else "BLOCKED",
            "reason": (self.last_evaluation or {}).get("reason", "Awaiting authorized decision"),
            "emergency_stop": self.emergency_latched,
            "last_evaluation": self.last_evaluation,
            "audit_count": len(self.audit_log),
        }

    async def evaluate(self, intent: DecisionIntent, betting_status: dict) -> RiskDecision:
        async with self._lock:
            profile_key = str(intent.profile).upper()
            try:
                profile = get_risk_profile(profile_key)
            except ValueError:
                # Pydantic normally prevents this, retained for internal callers.
                profile = get_risk_profile(self.selected_profile)

            current = betting_status.get("current_balance")
            if current is None:
                current = betting_status.get("last_balance")
            requested = intent.effective_amount()
            key = (intent.decision_id, intent.round_id or "")

            def result(approved: bool, reason: str, *, amount: int = 0,
                       level: RiskLevel = RiskLevel.BLOCKED) -> RiskDecision:
                return RiskDecision(
                    decision_id=intent.decision_id, round_id=intent.round_id,
                    approved=approved, profile=profile.key, cashout=profile.cashout,
                    approved_bet=amount, risk_level=level, reason=reason,
                    timestamp=time.time(), current_balance=current,
                    requested_bet=requested,
                )

            if self.emergency_latched:
                return await self._finish(result(False, "Emergency stop is active"), "risk:emergency_stop")
            state = betting_status.get("state", "IDLE")
            if state not in {"STARTING", "CONNECTED", "SIMULATION", "SHADOW", "NOT_CONNECTED", "WAITING_FOR_BROWSER"}:
                return await self._finish(result(False, "Automatic betting session is not active"))
            if profile.key != self.selected_profile:
                return await self._finish(result(False, "Decision profile does not match selected risk profile"))
            if not intent.execute or not intent.round_id or intent.is_expired():
                return await self._finish(result(False, "Decision is invalid, expired, or missing a round ID"))
            if key in self._evaluated:
                return await self._finish(result(False, "Duplicate decision"))
            if betting_status.get("current_bet") is not None:
                return await self._finish(result(False, "Another bet is already active"))
            if profile.cashout != intent.cashout:
                return await self._finish(result(False, "Cashout does not match selected risk profile"))
            if current is None:
                return await self._finish(result(False, "Balance cannot be verified"))
            start = betting_status.get("starting_balance")
            goal = betting_status.get("goal_balance")
            if goal is not None and current >= goal:
                return await self._finish(result(False, "Goal already reached"),
                                          "risk:goal_protection", "goal_reached")
            loss = max(0.0, (start - current)) if start is not None else 0.0
            if loss >= profile.maximum_session_loss:
                return await self._finish(result(False, "Maximum session loss reached"),
                                          "risk:loss_limit_reached", "risk_loss_limit")
            if betting_status.get("consecutive_losses", 0) >= profile.maximum_consecutive_losses:
                return await self._finish(result(False, "Maximum consecutive loss limit reached"),
                                          "risk:consecutive_loss_limit", "risk_consecutive_losses")
            if betting_status.get("mode") == "REAL":
                if betting_status.get("browser_status") != "CONNECTED" or not betting_status.get("last_ui_ready"):
                    return await self._finish(result(False, "Browser is not healthy and ready"))

            size = self.sizing.calculate(
                current_balance=float(current), profile=profile,
                requested_bet=requested, risk_level=profile.default_risk_level,
                session_state=betting_status,
            )
            if not size.approved:
                return await self._finish(result(False, size.reason))
            self._evaluated.add(key)
            return await self._finish(result(True, size.reason, amount=size.amount,
                                             level=RiskLevel(profile.default_risk_level)))

    def validate_execution(self, decision: dict, betting_status: dict) -> list[str]:
        """Final, side-effect-free authorization immediately before UI mutation."""
        reasons: list[str] = []
        risk = decision.get("risk") or decision.get("risk_evaluation") or {}
        if decision.get("status") != "READY_FOR_EXECUTION" or risk.get("status", "APPROVED" if risk.get("approved") else "REJECTED") != "APPROVED":
            reasons.append("decision_or_risk_not_approved")
        try:
            expires = datetime.fromisoformat(str(decision["expires_at"]).replace("Z", "+00:00"))
            if expires <= datetime.now(timezone.utc): reasons.append("decision_expired")
        except Exception:
            reasons.append("invalid_expiry")
        if not betting_status.get("automatic_enabled"): reasons.append("automatic_mode_off")
        if self.emergency_latched: reasons.append("emergency_stop")
        if decision.get("profile") != self.selected_profile: reasons.append("profile_changed")
        current = betting_status.get("current_balance")
        start = betting_status.get("starting_balance")
        goal = betting_status.get("goal_balance")
        profile = get_risk_profile(decision.get("profile", ""))
        amount = risk.get("approved_bet_amount", risk.get("approved_bet"))
        if current is None: reasons.append("balance_unverified")
        elif goal is not None and current >= goal: reasons.append("goal_reached")
        elif start is not None and start - current >= profile.maximum_session_loss: reasons.append("session_loss_limit")
        if betting_status.get("consecutive_losses", 0) >= profile.maximum_consecutive_losses: reasons.append("consecutive_loss_limit")
        if amount is None or current is None or not self.sizing.calculate(
            current_balance=float(current or 0), profile=profile, requested_bet=int(amount or 0),
            risk_level=profile.default_risk_level, session_state=betting_status,
        ).approved: reasons.append("approved_amount_no_longer_safe")
        if betting_status.get("current_bet") is not None: reasons.append("another_bet_active")
        if betting_status.get("mode") == "REAL" and (
            betting_status.get("browser_status") != "CONNECTED" or not betting_status.get("last_ui_ready")
        ): reasons.append("browser_unhealthy")
        return reasons
