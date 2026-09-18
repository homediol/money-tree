"""Pydantic request/response schemas for the betting API."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

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
    starting_balance: Optional[float] = Field(default=None, ge=0)
    goal_balance: Optional[float] = Field(default=None, gt=0)

    @field_validator("profile", mode="before")
    @classmethod
    def _normalise_profile(cls, value):
        return str(value).upper()

    @model_validator(mode="after")
    def _goal_above_start(self):
        if (self.starting_balance is not None and self.goal_balance is not None
                and self.goal_balance <= self.starting_balance):
            raise ValueError("goal_balance must be greater than starting_balance")
        return self


class AutomaticStartRequest(BaseModel):
    starting_balance: float = Field(ge=0)
    goal_balance: float = Field(gt=0)
    profile: ProfileName = "PROFILE_A"

    @field_validator("profile", mode="before")
    @classmethod
    def _normalise_profile(cls, value):
        return str(value).upper()

    @model_validator(mode="after")
    def _goal_above_start(self):
        if self.goal_balance <= self.starting_balance:
            raise ValueError("goal_balance must be greater than starting_balance")
        return self

    def as_session_request(self) -> SessionStartRequest:
        return SessionStartRequest(
            action="start", mode="REAL", profile=self.profile,
            starting_balance=self.starting_balance,
            goal_balance=self.goal_balance,
            label="automatic",
        )


class DecisionIntent(BaseModel):
    """An externally-produced betting decision.

    The betting module adds *no prediction logic*: it validates the contract,
    executes it against the browser/simulation backend and records the result.
    """

    decision_id: str = Field(min_length=1, max_length=120)
    round_id: Optional[str] = Field(default=None, min_length=1, max_length=120)
    execute: bool = True
    profile: ProfileName = "PROFILE_A"
    #: Desired cash-out multiplier (the decision, not a prediction).
    target_multiplier: Optional[float] = Field(default=None, ge=MIN_MULTIPLIER, le=MAX_MULTIPLIER)
    cashout: Optional[float] = Field(default=None, ge=MIN_MULTIPLIER, le=MAX_MULTIPLIER)
    expires_at: Optional[datetime] = None
    #: Stake in BIF. Optional — the profile default is applied.
    amount_bif: Optional[int] = Field(default=None, ge=1)
    bet_amount: Optional[int] = Field(default=None, ge=1)
    #: 0 = first bet slot, 1 = second bet slot (double-bet UI).
    bet_slot: Literal[0, 1] = 0
    #: Free-form provenance label, e.g. "signals/rule-17".
    source: Optional[str] = Field(default=None, max_length=120)

    @field_validator("profile", mode="before")
    @classmethod
    def _normalise_profile(cls, value):
        return str(value).upper()

    @field_validator("target_multiplier")
    @classmethod
    def _round_multiplier(cls, v):
        return None if v is None else round(float(v), 2)

    @field_validator("expires_at")
    @classmethod
    def _timezone_expiry(cls, value):
        if value is not None and value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @model_validator(mode="after")
    def _cashout_contract(self):
        value = self.cashout if self.cashout is not None else self.target_multiplier
        if value is None:
            raise ValueError("cashout is required")
        self.cashout = round(float(value), 2)
        self.target_multiplier = self.cashout
        if self.bet_amount is not None and self.amount_bif is not None:
            if self.bet_amount != self.amount_bif:
                raise ValueError("bet_amount and amount_bif must match")
        amount = self.bet_amount if self.bet_amount is not None else self.amount_bif
        if amount is not None:
            profile = get_profile(self.profile)
            if amount < profile.min_amount_bif or amount > profile.max_amount_bif:
                raise ValueError(
                    f"bet amount {amount} outside {profile.key} band "
                    f"[{profile.min_amount_bif}, {profile.max_amount_bif}]"
                )
            self.bet_amount = amount
            self.amount_bif = amount
        return self

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
        return self.bet_amount if self.bet_amount is not None else profile.default_amount_bif

    def is_expired(self) -> bool:
        return bool(self.expires_at and self.expires_at <= datetime.now(timezone.utc))

    def as_dict(self) -> dict:
        profile = get_profile(self.profile)
        return {
            "decision_id": self.decision_id,
            "round_id": self.round_id,
            "execute": self.execute,
            "profile": self.profile,
            "profile_base_target": profile.base_target,
            "target_multiplier": self.target_multiplier,
            "cashout": self.cashout,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "amount_bif": self.effective_amount(),
            "bet_amount": self.effective_amount(),
            "bet_slot": self.bet_slot,
            "source": self.source,
            "received_at": time.time(),
        }


def profile_choices() -> dict:
    return {key: {
        "name": p.label,
        "cashout": p.base_target,
        "label": p.label,
        "description": p.description,
        "base_target": p.base_target,
        "default_amount_bif": p.default_amount_bif,
        "min_amount_bif": p.min_amount_bif,
        "max_amount_bif": p.max_amount_bif,
        "max_loss_bif": p.max_loss_bif,
    } for key, p in PROFILES.items()}
