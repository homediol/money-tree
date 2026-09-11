"""Betting risk profiles.

A profile only constrains *how* a received decision may be executed (amount
limits, default cash-out target). The betting module never decides *what* to
bet — target multipliers arrive in the decision contract from an external
strategy/signals layer.
"""
from __future__ import annotations

from dataclasses import dataclass

# Global contract bounds for Aviator-style crash games.
MIN_MULTIPLIER = 1.01
MAX_MULTIPLIER = 1000.0


@dataclass(frozen=True)
class BettingProfile:
    key: str
    label: str
    description: str
    base_target: float          # default cash-out multiplier for this profile
    default_amount_bif: int     # stake used when the decision omits amount
    min_amount_bif: int
    max_amount_bif: int
    max_loss_bif: int           # safety: reject decisions that would exceed
    #                              the profile's single-bet loss tolerance

    def clamp_amount(self, amount: int) -> int:
        return max(self.min_amount_bif, min(self.max_amount_bif, amount))


PROFILES: dict[str, BettingProfile] = {
    "PROFILE_A": BettingProfile(
        key="PROFILE_A",
        label="Standard 2.0x",
        description="Targets 2.0x with a 500 BIF default stake.",
        base_target=2.0,
        default_amount_bif=500,
        min_amount_bif=100,
        max_amount_bif=10000,
        max_loss_bif=20000,
    ),
    "PROFILE_B": BettingProfile(
        key="PROFILE_B",
        label="Conservative 1.5x",
        description="Targets 1.5x with a 200 BIF default stake.",
        base_target=1.5,
        default_amount_bif=200,
        min_amount_bif=100,
        max_amount_bif=5000,
        max_loss_bif=10000,
    ),
}


def get_profile(key: str) -> BettingProfile:
    try:
        return PROFILES[key]
    except KeyError:
        raise ValueError(
            f"unknown betting profile {key!r}; choose from {sorted(PROFILES)}"
        ) from None


def profile_keys() -> list[str]:
    return sorted(PROFILES)

