"""Part 8 qualification gate. It never sizes or places a bet."""
from __future__ import annotations

import asyncio
import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable

from app.betting.profiles import get_profile
from app.betting.schemas import DecisionIntent
from app.decision.schemas import DecisionConfig, DecisionRecord, DecisionStatus

RANK = {"VERY_LOW": 0, "VERY_WEAK": 0, "LOW": 1, "WEAK": 1,
        "MEDIUM": 2, "MODERATE": 2, "HIGH": 3, "STRONG": 3,
        "VERY_HIGH": 4, "VERY_STRONG": 4}
TERMINAL = {DecisionStatus.BLOCKED, DecisionStatus.RISK_REJECTED,
            DecisionStatus.EXPIRED, DecisionStatus.INVALID}


def next_round_id(source: str) -> str:
    try:
        return str(int(source) + 1)
    except ValueError:
        return f"after:{source}"


class DecisionEngine:
    def __init__(self, path: Path, repository, *, config: DecisionConfig | None = None,
                 broadcaster: Callable[[dict], Awaitable[None]] | None = None):
        self.path = Path(path)
        self.config_path = self.path.with_name("decision_config.json")
        self.repository = repository
        self.config = config or self._load_config()
        self.broadcaster = broadcaster
        self._lock = asyncio.Lock()

    def _load_config(self) -> DecisionConfig:
        persisted = self.repository.load_application_state("decision_config")
        if persisted:
            try:
                return DecisionConfig.model_validate(persisted)
            except Exception:
                pass
        # One-time migration for existing installations. PostgreSQL becomes
        # authoritative as soon as the validated value is copied.
        try:
            config = DecisionConfig.model_validate_json(self.config_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = DecisionConfig()
        self.repository.save_application_state("decision_config", config.model_dump(mode="json"),
                                               datetime.now(timezone.utc).isoformat())
        return config

    async def _emit(self, event: str, decision: dict) -> None:
        if self.broadcaster:
            await self.broadcaster({"type": event, "decision": decision})

    @staticmethod
    def _transition(record: dict, status: DecisionStatus, reason: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        record["status"] = status.value
        record["updated_at"] = now
        record["transitions"].append({"status": status.value, "at": now, "reason": reason})

    def _persist(self, record: dict) -> dict:
        validated = DecisionRecord.model_validate(record).model_dump(mode="json")
        self.repository.save_decision(validated)
        return validated

    def current(self) -> dict | None:
        latest = self.repository.latest_decision()
        if latest is None:
            return None
        try:
            record = DecisionRecord.model_validate(latest)
            payload = record.model_dump(mode="json")
            if record.status not in TERMINAL and record.expires_at <= datetime.now(timezone.utc):
                self._transition(payload, DecisionStatus.EXPIRED, "decision TTL elapsed")
                payload["execution_status"] = "EXPIRED"
                payload["block_reasons"].append("decision_expired")
                return self._persist(payload)
            return payload
        except Exception:
            return None

    def update_config(self, payload: dict) -> dict:
        self.config = DecisionConfig.model_validate(self.config.model_dump() | payload)
        result = self.config.model_dump(mode="json")
        self.repository.save_application_state("decision_config", result,
                                               datetime.now(timezone.utc).isoformat())
        return self.config.model_dump()

    async def evaluate(self, prediction: dict | None, evidence: dict | None, *,
                       model_status: dict, betting_status: dict, risk_manager,
                       target_round_id: str | None = None) -> dict:
        async with self._lock:
            if not prediction or not evidence:
                return {"status": DecisionStatus.WAITING.value,
                        "reasons": [], "block_reasons": ["prediction_or_evidence_missing"]}
            source = str(prediction.get("source_round_id") or "")
            target = target_round_id or next_round_id(source)
            profile_key = str(betting_status.get("profile") or risk_manager.selected_profile or "PROFILE_A").upper()
            get_profile(profile_key)  # centralized profile validation
            raw_key = f"{prediction.get('prediction_id')}|{target}|{profile_key}"
            key = hashlib.sha256(raw_key.encode()).hexdigest()
            existing = self.repository.decision_by_key(key)
            if existing:
                return existing
            now = datetime.now(timezone.utc)
            record = {
                "contract_version": "part8-v1", "decision_id": uuid.uuid4().hex,
                "idempotency_key": key, "status": DecisionStatus.CREATED.value,
                "created_at": now.isoformat(), "updated_at": now.isoformat(),
                "expires_at": (now + timedelta(seconds=self.config.decision_ttl_seconds)).isoformat(),
                "source_round_id": source, "target_round_id": target,
                "prediction_id": str(prediction.get("prediction_id") or ""),
                "evidence_id": str(evidence.get("evidence_id") or ""),
                "probability": prediction.get("probability_2x"),
                "confidence": evidence.get("confidence", "VERY_LOW"),
                "evidence_strength": evidence.get("evidence_strength", "VERY_WEAK"),
                "profile": profile_key, "requested_action": "NO_ACTION",
                "risk_status": "NOT_REQUESTED", "execution_status": "NOT_AUTHORIZED",
                "reasons": [], "block_reasons": [],
                "prediction_created_at": prediction.get("created_at"),
                "evidence_calculated_at": evidence.get("calculated_at"),
                "model_version": str(prediction.get("model_version") or ""),
                "feature_version": str(prediction.get("feature_version") or ""),
                "risk_evaluation": None,
                "transitions": [{"status": "CREATED", "at": now.isoformat(), "reason": "prediction and evidence received"}],
            }
            self._transition(record, DecisionStatus.VALIDATING, "validating qualification rules")
            blocks = self._validate(record, prediction, evidence, model_status, betting_status, target)
            if blocks:
                record["block_reasons"] = blocks
                stale_target = "target_round_stale" in blocks
                final_status = DecisionStatus.EXPIRED if stale_target else DecisionStatus.BLOCKED
                self._transition(record, final_status,
                                 "target round already passed" if stale_target else "qualification blocked")
                record["risk_status"] = "BLOCKED"
                record["execution_status"] = "EXPIRED" if stale_target else "BLOCKED"
                result = self._persist(record)
                await self._emit("decision:expired" if stale_target else "decision:blocked", result)
                return result
            record["reasons"] = ["probability_threshold_met", "confidence_threshold_met",
                                 "evidence_threshold_met", "model_and_data_healthy"]
            record["requested_action"] = "QUALIFY_FOR_RISK"
            self._transition(record, DecisionStatus.CANDIDATE, "qualification rules passed")
            self._transition(record, DecisionStatus.PENDING_RISK, "submitted to independent risk manager")
            record["risk_status"] = "PENDING"
            await self._emit("decision:risk_pending", self._persist(record))
            profile = get_profile(profile_key)
            configuration = betting_status.get("session_configuration") or {}
            requested_amount = None
            if configuration.get("mode") == "MANUAL":
                enabled_panels = [panel for panel in configuration.get("panels", [])
                                  if panel.get("enabled")]
                requested_amount = sum(int(panel.get("stake") or 0) for panel in enabled_panels)
            intent = DecisionIntent(decision_id=record["decision_id"], round_id=target,
                                    execute=True, profile=profile_key, cashout=profile.base_target,
                                    bet_amount=requested_amount,
                                    expires_at=record["expires_at"], source="part8-decision-engine")
            risk = await risk_manager.evaluate(intent, betting_status)
            record["risk_evaluation"] = risk.public()
            record["risk"] = risk.public()
            if not risk.approved:
                record["risk_status"], record["execution_status"] = "REJECTED", "BLOCKED"
                record["block_reasons"].append(f"risk_rejected:{risk.reason}")
                self._transition(record, DecisionStatus.RISK_REJECTED, "risk manager rejected candidate")
                result = self._persist(record)
                await self._emit("decision:risk_rejected", result)
                return result
            self._transition(record, DecisionStatus.RISK_APPROVED, "risk manager approved candidate")
            record["risk_status"] = "APPROVED"
            record["requested_action"] = "EXECUTE_APPROVED_DECISION"
            record["execution_status"] = "READY"
            self._transition(record, DecisionStatus.READY_FOR_EXECUTION, "eligible for separate executor consumption")
            result = self._persist(record)
            await self._emit("decision:ready", result)
            return result

    def _validate(self, record: dict, prediction: dict, evidence: dict,
                  model: dict, betting: dict, target: str) -> list[str]:
        blocks: list[str] = []
        if evidence.get("prediction_id") != prediction.get("prediction_id"):
            blocks.append("prediction_evidence_mismatch")
        if evidence.get("model_version") != prediction.get("model_version") or evidence.get("feature_version") != prediction.get("feature_version"):
            blocks.append("model_or_feature_version_mismatch")
        if model.get("model_version") != prediction.get("model_version") or model.get("feature_version") != prediction.get("feature_version"):
            blocks.append("active_model_or_feature_version_mismatch")
        if target != next_round_id(record["source_round_id"]):
            blocks.append("target_round_mismatch")
        latest_round = betting.get("latest_history_round_id")
        if latest_round is not None and str(latest_round) != record["source_round_id"]:
            blocks.append("target_round_stale")
        if record["probability"] is None or record["probability"] < self.config.minimum_probability:
            blocks.append("probability_below_threshold")
        if RANK.get(record["confidence"], -1) < RANK[self.config.minimum_confidence]:
            blocks.append("confidence_below_threshold")
        if RANK.get(record["evidence_strength"], -1) < RANK[self.config.minimum_evidence_strength]:
            blocks.append("evidence_strength_below_threshold")
        if self.config.require_pattern_support and evidence.get("pattern", {}).get("sample_size", 0) < 1:
            blocks.append("pattern_support_missing")
        if model.get("status") != "READY" or not model.get("deployable"):
            blocks.append("model_unhealthy")
        if not prediction.get("fresh") or not evidence.get("data_quality", {}).get("fresh"):
            blocks.append("prediction_or_data_stale")
        if not self.config.automatic_mode or not betting.get("automatic_enabled"):
            blocks.append("automatic_mode_off")
        if betting.get("stop_reason") == "goal_reached":
            blocks.append("goal_reached")
        if betting.get("emergency_stop"):
            blocks.append("emergency_stop")
        if betting.get("mode") == "REAL" and (betting.get("browser_status") != "CONNECTED" or not betting.get("last_ui_ready")):
            blocks.append("browser_unavailable")
        return blocks
