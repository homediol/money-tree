from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class DecisionStatus(StrEnum):
    NO_ACTION = "NO_ACTION"
    WAITING = "WAITING"
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    CANDIDATE = "CANDIDATE"
    PENDING_RISK = "PENDING_RISK"
    RISK_APPROVED = "RISK_APPROVED"
    RISK_REJECTED = "RISK_REJECTED"
    READY_FOR_EXECUTION = "READY_FOR_EXECUTION"
    BLOCKED = "BLOCKED"
    EXPIRED = "EXPIRED"
    INVALID = "INVALID"


class DecisionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minimum_probability: float = Field(default=.55, ge=0, le=1)
    minimum_confidence: Literal["VERY_LOW", "LOW", "MEDIUM", "HIGH", "VERY_HIGH"] = "MEDIUM"
    minimum_evidence_strength: Literal["VERY_WEAK", "WEAK", "MODERATE", "STRONG", "VERY_STRONG"] = "MODERATE"
    require_pattern_support: bool = True
    automatic_mode: bool = False
    decision_ttl_seconds: int = Field(default=30, ge=1, le=600)


class DecisionConfigUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    minimum_probability: float | None = Field(default=None, ge=0, le=1)
    minimum_confidence: Literal["VERY_LOW", "LOW", "MEDIUM", "HIGH", "VERY_HIGH"] | None = None
    minimum_evidence_strength: Literal["VERY_WEAK", "WEAK", "MODERATE", "STRONG", "VERY_STRONG"] | None = None
    require_pattern_support: bool | None = None
    automatic_mode: bool | None = None
    decision_ttl_seconds: int | None = Field(default=None, ge=1, le=600)


class DecisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contract_version: str = "part8-v1"
    decision_id: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1)
    status: DecisionStatus
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    source_round_id: str = Field(min_length=1)
    target_round_id: str = Field(min_length=1)
    prediction_id: str = Field(min_length=1)
    evidence_id: str = Field(min_length=1)
    probability: float = Field(ge=0, le=1)
    confidence: str
    evidence_strength: str
    profile: str
    requested_action: Literal["NO_ACTION", "QUALIFY_FOR_RISK", "EXECUTE_APPROVED_DECISION"]
    risk_status: Literal["NOT_REQUESTED", "PENDING", "APPROVED", "REJECTED", "BLOCKED"]
    execution_status: Literal["NOT_AUTHORIZED", "READY", "BLOCKED", "EXPIRED"]
    reasons: list[str]
    block_reasons: list[str]
    prediction_created_at: datetime
    evidence_calculated_at: datetime
    model_version: str
    feature_version: str
    risk_evaluation: dict | None = None
    transitions: list[dict]

    @model_validator(mode="after")
    def enforce_execution_contract(self):
        if self.status == DecisionStatus.READY_FOR_EXECUTION:
            if self.risk_status != "APPROVED" or self.execution_status != "READY":
                raise ValueError("READY_FOR_EXECUTION requires risk approval")
        if self.execution_status == "READY" and self.status != DecisionStatus.READY_FOR_EXECUTION:
            raise ValueError("execution READY requires READY_FOR_EXECUTION status")
        allowed = {
            "CREATED": {"VALIDATING"}, "VALIDATING": {"CANDIDATE", "BLOCKED", "EXPIRED", "INVALID"},
            "CANDIDATE": {"PENDING_RISK"},
            "PENDING_RISK": {"RISK_APPROVED", "RISK_REJECTED", "EXPIRED"},
            "RISK_APPROVED": {"READY_FOR_EXECUTION"},
            "READY_FOR_EXECUTION": {"EXPIRED"},
            "BLOCKED": set(), "RISK_REJECTED": set(), "EXPIRED": set(), "INVALID": set(),
        }
        states = [str(item.get("status")) for item in self.transitions]
        if states and states[0] != "CREATED":
            raise ValueError("decision lifecycle must start at CREATED")
        for before, after in zip(states, states[1:]):
            if after not in allowed.get(before, set()):
                raise ValueError(f"invalid decision transition {before} -> {after}")
        if states and states[-1] != self.status.value:
            raise ValueError("latest transition must match decision status")
        return self
