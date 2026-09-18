from __future__ import annotations

from enum import StrEnum
from typing import Literal, Optional

from pydantic import BaseModel, field_validator


class RiskLevel(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    BLOCKED = "BLOCKED"


class ProfileSelection(BaseModel):
    profile: Literal["PROFILE_A", "PROFILE_B"]

    @field_validator("profile", mode="before")
    @classmethod
    def normalize(cls, value):
        return str(value).upper()


class RiskDecision(BaseModel):
    decision_id: str
    round_id: Optional[str]
    approved: bool
    profile: str
    cashout: float
    approved_bet: int
    risk_level: RiskLevel
    reason: str
    timestamp: float
    current_balance: Optional[float]
    requested_bet: int

    def public(self) -> dict:
        return self.model_dump(mode="json")
