"""Pydantic request/response schemas for the betting API."""
from __future__ import annotations

import time
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.betting.profiles import (
    MAX_MULTIPLIER,
    MIN_MULTIPLIER,
    PROFILES,
    get_profile,
    profile_keys,
)

ProfileName = Literal["PROFILE_A", "PROFILE_B"]
ModeName = Literal["REAL", "SIMULATION"]
SessionAction = Literal["start", "stop"]


class SessionStartRequest(BaseModel):
    action: SessionAction
    mode: ModeName = "REAL"
    profile: ProfileName = "PROFILE_A"
    # Auto-stop / safety goals (all optional; session keeps running until
    # stopped when none are set).
    target_profit_bif: Optional[float] = Field(default=None, ge=0)
    max_loss_bif: Optional[float] = Field(default=None, ge=0)
    max_rounds: Optional[int] = Field(default=None, ge=1)
    # Label so multiple instances can be told apart in logs/events.
    label: Optional[str] = Field(default=None, max_length=80)


class DecisionIntent(BaseModel):
    """An externally-produced betting decision.

    The betting module adds *no prediction logic*: it validates the contract,
    executes it against the browser/simulation backend and records the result.
    """

    decision_id: str = Field(min_length=1, max_length=120)
    profile: ProfileName = "PROFILE_A"
    #: Desired cash-out multiplier (the decision, not a prediction).
    target_multiplier: float = Field(ge=MIN_MULTIPLIER, le=MAX_MULTIPLIER)
    #: Stake in BIF. Optional — the profile default is applied.
    amount_bif: Optional[int] = Field(default=None, ge=1)
    #: 0 = first bet slot, 1 = second bet slot (double-bet UI).
    bet_slot: Literal[0, 1] = 0
    #: Free-form provenance label, e.g. "signals/rule-17".
    source: Optional[str] = Field(default=None, max_length=120)

    @field_validator("target_multiplier")
    @classmethod
    def _round_multiplier(cls, v: float) -> float:
        return round(float(v), 2)

    @field_validator("amount_bif")
    @classmethod
    def _amount_within_profile(cls, v, info) -> int:
        if v is None:
            return v
        profile = get_profile(info.data.get("profile", "PROFILE_A"))
        if v < profile.min_amount_bif or v > profile.max_amount_bif:
            raise ValueError(
                f"amount {v} outside {profile.key} band "
                f"[{profile.min_amount_bif}, {profile.max_amount_bif}]"
            )
        return v

    def effective_amount(self) -> int:
        """Amount to stake: explicit value or the profile default."""
        profile = get_profile(self.profile)
        return self.amount_bif if self.amount_bif is not None else profile.default_amount_bif

    def as_dict(self) -> dict:
        profile = get_profile(self.profile)
        return {
            "decision_id": self.decision_id,
            "profile": self.profile,
            "profile_base_target": profile.base_target,
            "target_multiplier": self.target_multiplier,
            "amount_bif": self.effective_amount(),
            "bet_slot": self.bet_slot,
            "source": self.source,
            "received_at": time.time(),
        }


def profile_choices() -> dict:
    return {key: {
        "label": p.label,
        "description": p.description,
        "base_target": p.base_target,
        "default_amount_bif": p.default_amount_bif,
        "min_amount_bif": p.min_amount_bif,
        "max_amount_bif": p.max_amount_bif,
        "max_loss_bif": p.max_loss_bif,
    } for key, p in PROFILES.items()}


