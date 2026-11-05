"""Request contracts for betting control and fixed-stake execution."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from app.betting.execution_defaults import (
    DEFAULT_FIXED_STAKE,
    FIXED_CASHOUT,
    MAX_MULTIPLIER,
    MIN_MULTIPLIER,
)

ModeName = Literal["REAL", "SIMULATION"]
SessionAction = Literal["start", "stop"]


class SessionStartRequest(BaseModel):
    action: SessionAction
    mode: ModeName = "REAL"
    target_profit_bif: Optional[float] = Field(default=None, ge=0)
    max_rounds: Optional[int] = Field(default=None, ge=1)
    label: Optional[str] = Field(default=None, max_length=80)
    starting_balance: Optional[float] = Field(default=None, ge=0)
    goal_balance: Optional[float] = Field(default=None, gt=0)
    win_limit_enabled: Optional[bool] = None
    win_limit: Optional[float] = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _goal_above_start(self):
        if (self.starting_balance is not None and self.goal_balance is not None
                and self.goal_balance <= self.starting_balance):
            raise ValueError("goal_balance must be greater than starting_balance")
        if self.win_limit_enabled is True:
            if self.win_limit is not None:
                if self.starting_balance is None:
                    raise ValueError("starting_balance is required when setting a win limit")
                self.goal_balance = self.starting_balance + self.win_limit
            elif self.goal_balance is None:
                raise ValueError("win_limit or goal_balance is required when the win limit is enabled")
        elif self.win_limit_enabled is False:
            self.goal_balance = None
        return self


class AutomaticStartRequest(BaseModel):
    starting_balance: float = Field(ge=0)
    goal_balance: float = Field(gt=0)

    @model_validator(mode="after")
    def _goal_above_start(self):
        if self.goal_balance <= self.starting_balance:
            raise ValueError("goal_balance must be greater than starting_balance")
        return self

    def as_session_request(self) -> SessionStartRequest:
        return SessionStartRequest(action="start", mode="REAL",
                                   starting_balance=self.starting_balance,
                                   goal_balance=self.goal_balance, label="automatic")


class DecisionIntent(BaseModel):
    """A target-bound decision accepted by the existing executor."""

    decision_id: str = Field(min_length=1, max_length=120)
    round_id: str = Field(min_length=1, max_length=120)
    execute: bool = True
    target_multiplier: Optional[float] = Field(default=None, ge=MIN_MULTIPLIER, le=MAX_MULTIPLIER)
    cashout: Optional[float] = Field(default=None, ge=MIN_MULTIPLIER, le=MAX_MULTIPLIER)
    expires_at: datetime
    amount_bif: Optional[int] = Field(default=None, ge=1)
    bet_amount: Optional[int] = Field(default=None, ge=1)
    bet_slot: Literal[0, 1] = 0
    amount_currency: Optional[Literal["BIF", "RWF"]] = None
    source: Optional[str] = Field(default=None, max_length=120)
    source_round_id: Optional[str] = Field(default=None, min_length=1, max_length=120)
    target_stream_id: Optional[str] = Field(default=None, min_length=1, max_length=120)
    target_round_index: Optional[int] = Field(default=None, ge=0)
    provider_round_id: Optional[str] = Field(default=None, min_length=1, max_length=256)

    @field_validator("target_multiplier")
    @classmethod
    def _round_multiplier(cls, value):
        return None if value is None else round(float(value), 2)

    @field_validator("expires_at")
    @classmethod
    def _timezone_expiry(cls, value):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @model_validator(mode="after")
    def _fixed_execution_contract(self):
        value = self.cashout if self.cashout is not None else self.target_multiplier
        if value is None:
            value = FIXED_CASHOUT
        if round(float(value), 2) != FIXED_CASHOUT:
            raise ValueError("cashout must remain fixed at 2.00x")
        self.cashout = FIXED_CASHOUT
        self.target_multiplier = FIXED_CASHOUT
        if self.bet_amount is not None and self.amount_bif is not None:
            if self.bet_amount != self.amount_bif:
                raise ValueError("bet_amount and amount_bif must match")
        amount = self.bet_amount if self.bet_amount is not None else self.amount_bif
        if amount is None:
            amount = DEFAULT_FIXED_STAKE
        self.bet_amount = amount
        self.amount_bif = amount
        return self

    def effective_amount(self) -> int:
        return int(self.bet_amount if self.bet_amount is not None else DEFAULT_FIXED_STAKE)

    def is_expired(self) -> bool:
        return bool(self.expires_at and self.expires_at <= datetime.now(timezone.utc))

    def as_dict(self) -> dict:
        return {
            "decision_id": self.decision_id,
            "round_id": self.round_id,
            "execute": self.execute,
            "target_multiplier": FIXED_CASHOUT,
            "cashout": FIXED_CASHOUT,
            "expires_at": self.expires_at.isoformat(),
            "amount_bif": self.effective_amount(),
            "bet_amount": self.effective_amount(),
            "bet_slot": self.bet_slot,
            "amount_currency": self.amount_currency,
            "source": self.source,
            "source_round_id": self.source_round_id,
            "target_stream_id": self.target_stream_id,
            "target_round_index": self.target_round_index,
            "provider_round_id": self.provider_round_id,
            "received_at": time.time(),
        }
