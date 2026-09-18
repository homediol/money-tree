"""Centralized, configurable risk profiles and limits."""
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


# These values are operating limits, not claims about strategy performance.
RISK_PROFILES: dict[str, RiskProfile] = {
    "PROFILE_A": RiskProfile(
        key="PROFILE_A", name="2x Profile", cashout=2.0,
        minimum_bet=100, maximum_bet=1000,
        maximum_balance_percentage=0.05,
        maximum_session_loss=1500, maximum_consecutive_losses=3,
    ),
    "PROFILE_B": RiskProfile(
        key="PROFILE_B", name="1.5x Profile", cashout=1.5,
        minimum_bet=100, maximum_bet=750,
        maximum_balance_percentage=0.03,
        maximum_session_loss=1000, maximum_consecutive_losses=3,
    ),
}


def get_risk_profile(key: str) -> RiskProfile:
    normalized = str(key).upper()
    try:
        return RISK_PROFILES[normalized]
    except KeyError as exc:
        raise ValueError(f"unknown risk profile: {key}") from exc
