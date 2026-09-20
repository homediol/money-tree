"""Single source of truth for execution/risk profile limits."""
from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class RiskProfile:
    key: str
    name: str
    cashout: float
    minimum_bet: int
    maximum_bet: int
    maximum_balance_percentage: float
    maximum_session_loss: float
    maximum_consecutive_losses: int
    default_risk_level: str = "MEDIUM"

    def public(self) -> dict:
        return asdict(self)


RISK_PROFILES = {
    "PROFILE_A": RiskProfile("PROFILE_A", "2x Profile", 2.0, 100, 1000, .05, 1500, 3),
    "PROFILE_B": RiskProfile("PROFILE_B", "1.5x Profile", 1.5, 100, 750, .03, 1000, 3),
}


def get_risk_profile(key: str) -> RiskProfile:
    try:
        return RISK_PROFILES[str(key).upper()]
    except KeyError as exc:
        raise ValueError(f"unknown risk profile: {key}") from exc
