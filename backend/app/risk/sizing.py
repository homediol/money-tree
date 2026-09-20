"""Bankroll-constrained bet sizing. No loss-recovery logic belongs here."""
from __future__ import annotations

from dataclasses import dataclass

from app.profiles import RiskProfile


@dataclass(frozen=True)
class BetSizeResult:
    approved: bool
    amount: int
    reason: str


class BetSizing:
    def calculate(
        self, *, current_balance: float, profile: RiskProfile,
        requested_bet: int, risk_level: str, session_state: dict,
    ) -> BetSizeResult:
        del risk_level, session_state  # reserved inputs for future configured policies
        if requested_bet < profile.minimum_bet:
            return BetSizeResult(False, 0, "Bet is below the configured minimum")
        if requested_bet > profile.maximum_bet:
            return BetSizeResult(False, 0, "Bet exceeds the configured maximum")
        balance_limit = current_balance * profile.maximum_balance_percentage
        if requested_bet > balance_limit:
            return BetSizeResult(False, 0, "Bet exceeds the maximum balance percentage")
        if requested_bet > current_balance:
            return BetSizeResult(False, 0, "Insufficient available balance")
        return BetSizeResult(True, int(requested_bet), "Within configured bankroll limits")
