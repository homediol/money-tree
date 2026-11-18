from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import logging
import math
import os
from typing import Any, Mapping
from urllib.request import Request as UrlRequest, urlopen

from fastapi import HTTPException

from app.api import signals as signals_api


log = logging.getLogger(__name__)


def _is_committed_verified(event: Mapping[str, Any]) -> bool:
    return (event.get("event_type") == "round.persisted"
            and event.get("durability") == "COMMITTED"
            and event.get("continuity_status") == "VERIFIED")


def _is_verified_observation(event: Mapping[str, Any]) -> bool:
    return (event.get("event_type") == "round.observed"
            and event.get("durability") == "PENDING"
            and event.get("continuity_status") == "VERIFIED")


async def _deliver_behaviour_settlement(app: Any, payload: Mapping[str, Any]) -> bool:
    """Deliver a committed settlement from the durable SHADOW outbox to Behaviour."""
    signal_id = str(payload.get("signal_id") or "")
    repository = signals_api._repository(app)
    marker = getattr(repository, "mark_behaviour_strategy_settlement_delivered", None)
    inbox = getattr(app.state, "round_observation_inbox", None)
    if not signal_id or not callable(marker):
        return False
    try:
        if inbox is not None:
            mirrored_settlement = {
                "status": str(payload.get("settlement_status") or ""),
                "result": str(payload.get("result") or "").upper(),
                "target_round_id": payload.get("target_round_id"),
                "target_round_index": payload.get("target_round_index"),
                "target_stream_id": payload.get("target_stream_id"),
                "actual_multiplier": payload.get("actual_multiplier"),
                "profit_loss_rwf": payload.get("net_profit"),
                "evidence": dict(payload.get("evidence") or {}),
            }
            mirrored = await asyncio.to_thread(inbox.commit_fast_signal_settlement,
                                                signal_id, mirrored_settlement)
            if not mirrored:
                # The mirror can already contain the canonical settlement
                # under a resolved round identity while the durable SHADOW
                # outbox still carries its original index-only target. Do not
                # let that projection conflict suppress delivery to Behaviour;
                # its locked-signal and committed-evidence checks are the
                # authoritative idempotency gate.
                log.info("[SHADOW_SETTLEMENT_FEEDBACK] mirror not updated; sending authoritative receipt signal=%s",
                         signal_id)

        base = os.getenv("BEHAVIOUR_LOCAL_API_URL", "http://127.0.0.1:8100").rstrip("/")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        secret = os.getenv("BEHAVIOUR_SHADOW_SHARED_SECRET", "").strip()
        if secret:
            headers["X-Behaviour-Shadow-Secret"] = secret
        request = UrlRequest(base + "/api/v1/behaviour/internal/shadow-settlement",
            data=json.dumps(dict(payload), separators=(",", ":"), default=str).encode("utf-8"),
            headers=headers, method="POST")

        def send():
            with urlopen(request, timeout=2.0) as response:
                return json.loads(response.read().decode("utf-8"))

        result = await asyncio.to_thread(send)
        delivered = isinstance(result, dict) and result.get("status") in {"RECORDED", "ALREADY_RECORDED"}
        if not delivered:
            raise RuntimeError("behaviour_settlement_ack_invalid")
        await asyncio.to_thread(marker, signal_id, delivered=True)
        return True
    except Exception as exc:
        try:
            await asyncio.to_thread(marker, signal_id, delivered=False,
                                    error=f"{type(exc).__name__}:{exc}")
        except Exception:
            log.exception("[SHADOW_SETTLEMENT_FEEDBACK] could not persist retry status signal=%s", signal_id)
        log.info("[SHADOW_SETTLEMENT_FEEDBACK] deferred signal=%s error=%s",
                 signal_id, type(exc).__name__)
        return False


class ShadowSignalBridge:
    """Publish persisted Shadow selections through the existing signal service."""

    def __init__(self) -> None:
        self._status: dict[str, Any] = {
            "state": "IDLE", "last_attempt_at": None, "last_published_at": None,
            "last_signal_id": None, "last_decision_id": None, "last_target_round_id": None,
            "last_target_round_index": None, "last_target_identity_status": None,
            "last_error": None, "published_count": 0, "duplicate_count": 0,
            "last_publication_error": None, "last_failed_decision_id": None,
            "settled_count": 0,
        }
        self._settlement_recovery_task: asyncio.Task | None = None

    def status(self) -> dict[str, Any]:
        return dict(self._status)

    def _update(self, app: Any, **changes: Any) -> None:
        self._status.update(changes)
        app.state.shadow_signal_bridge_status = self.status()

    def _blocked(self, app: Any, reason: str, session_id: str = "",
                 selection_id: str = "", target_round_id: str | None = None) -> str:
        self._update(app, state="BLOCKED", last_error=reason,
                     last_decision_id=selection_id or self._status.get("last_decision_id"),
                     last_target_round_id=target_round_id or self._status.get("last_target_round_id"))
        log.warning("[SHADOW_SIGNAL] selection blocked session=%s decision=%s target=%s reason=%s",
                    session_id or "UNKNOWN", selection_id or "UNKNOWN",
                    target_round_id or "UNKNOWN", reason)
        return "SKIPPED"

    def _skip_stale_selection(self, app: Any, session_id: str, selection_id: str,
                              target_round_id: str | None = None) -> str:
        """Ignore a committed decision whose target window has already moved on."""
        self._update(app, state="SKIPPED_STALE_SELECTION", last_error=None,
                     last_decision_id=selection_id or self._status.get("last_decision_id"),
                     last_target_round_id=target_round_id or self._status.get("last_target_round_id"))
        log.info("[SHADOW_SIGNAL] skipped stale selection session=%s decision=%s target=%s",
                 session_id or "UNKNOWN", selection_id or "UNKNOWN",
                 target_round_id or "UNKNOWN")
        return "STALE"

    async def process_committed_round(self, app: Any, event: Mapping[str, Any],
                                      evaluation: Mapping[str, Any] | None = None, *,
                                      collector_detection_at: str | None = None,
                                      collector_receipt_at: str | None = None) -> dict[str, Any]:
        """Settle prior signals and publish any new committed TRUE selection."""
        if not _is_committed_verified(event):
            return {"status": "IGNORED_UNCOMMITTED_EVENT", "published": 0, "settled": 0}

        self._update(app, state="PROCESSING", last_error=None)

        if evaluation is None:
            engine = getattr(app.state, "rules_engine", None)
            if engine is None:
                return {"status": "RULES_ENGINE_UNAVAILABLE", "published": 0, "settled": 0}
            evaluation = await asyncio.to_thread(engine.observe_round, event,
                target_already_completed=(str(event.get("delivery_mode") or "LIVE").upper() != "LIVE"))

        settled = await self._settle_signals_for_target(app, event)
        if getattr(app.state, "rules_engine", None) is not None:
            self._schedule_pending_settlement_recovery(app)

        published = 0
        duplicates = 0
        for session_record in evaluation.get("sessions", []) if isinstance(evaluation, Mapping) else []:
            if not isinstance(session_record, Mapping):
                continue
            result = await self._publish_selection(
                app, event, session_record,
                collector_detection_at=collector_detection_at, collector_receipt_at=collector_receipt_at)
            if result == "PUBLISHED":
                published += 1
            elif result == "DUPLICATE":
                duplicates += 1

        if published:
            status = "PUBLISHED"
        elif duplicates:
            status = "ALREADY_PUBLISHED"
        elif settled:
            status = "SETTLED"
        elif self._status.get("state") in {
                "REJECTED_INVALID_DECISION", "BLOCKED", "RETRYING"}:
            status = str(self._status["state"])
        else:
            status = "NO_TRUE_COMMITTED_SELECTION"
        self._update(app, state=status, settled_count=int(self._status["settled_count"]) + settled)
        return {"status": status, "published": published, "duplicates": duplicates,
                "settled": settled}

    async def process_observed_round(self, app: Any, event: Mapping[str, Any],
                                     evaluation: Mapping[str, Any], *,
                                     decision_started_ns: int | None = None,
                                     backend_received_ns: int | None = None,
                                     collector_detection_at: str | None = None,
                                     collector_receipt_at: str | None = None) -> dict[str, Any]:
        """Publish SHADOW selections on the fast verified-observation path.

        Waiting for the PostgreSQL batch handoff can mean the next round is
        already committed before a signal is published. This path runs after
        the observation is durably accepted by the API inbox; settlement still
        waits for the matching committed target event.
        """
        if not _is_verified_observation(event):
            return {"status": "IGNORED_UNVERIFIED_OBSERVATION", "published": 0,
                    "duplicates": 0}
        self._update(app, state="PROCESSING", last_error=None)
        published = 0
        duplicates = 0
        for session_record in evaluation.get("sessions", []) if isinstance(evaluation, Mapping) else []:
            if not isinstance(session_record, Mapping):
                continue
            result = await self._publish_selection(app, event, session_record,
                                                   observed_source=True,
                                                   decision_started_ns=decision_started_ns,
                                                   backend_received_ns=backend_received_ns,
                                                   collector_detection_at=collector_detection_at,
                                                   collector_receipt_at=collector_receipt_at)
            if result == "PUBLISHED":
                published += 1
            elif result == "DUPLICATE":
                duplicates += 1
        if published:
            status = "PUBLISHED"
        elif duplicates:
            status = "ALREADY_PUBLISHED"
        elif self._status.get("state") in {"REJECTED_INVALID_DECISION", "BLOCKED", "RETRYING"}:
            status = str(self._status["state"])
        else:
            status = "NO_TRUE_OBSERVED_SELECTION"
        self._update(app, state=status)
        return {"status": status, "published": published, "duplicates": duplicates}

    async def _publish_selection(self, app: Any, event: Mapping[str, Any],
                                 session_record: Mapping[str, Any], *,
                                 observed_source: bool = False,
                                 decision_started_ns: int | None = None,
                                 backend_received_ns: int | None = None,
                                 collector_detection_at: str | None = None,
                                 collector_receipt_at: str | None = None) -> str:
        decision = session_record.get("decision")
        if not isinstance(decision, Mapping) or decision.get("action") != "SELECT":
            return "SKIPPED"
        session_id = str(session_record.get("session_id") or "")
        selection_id = str(decision.get("selection_id") or "")
        source_is_verified_observation = observed_source and _is_verified_observation(event)
        expected_persistence = "PENDING" if source_is_verified_observation else "COMMITTED"
        if session_record.get("persistence_status") != expected_persistence:
            return self._blocked(app, "SELECT_DECISION_NOT_COMMITTED", session_id, selection_id)
        source = session_record.get("source_round")
        round_id = str(event.get("round_id") or "")
        stream_id = str(event.get("stream_id") or "")
        try:
            source_index = int(event.get("round_index"))
            record_source_index = int(source.get("roundIndex")) if isinstance(source, Mapping) else 0
        except (TypeError, ValueError, OverflowError):
            return self._blocked(app, "COMMITTED_SOURCE_INDEX_INVALID", session_id, selection_id)
        if (not isinstance(source, Mapping) or str(source.get("roundId") or "") != round_id
                or str(source.get("streamId") or "") != stream_id
                or record_source_index != source_index):
            return self._blocked(app, "SELECT_SOURCE_IDENTITY_MISMATCH", session_id, selection_id)

        matched = decision.get("true_rule_ids")
        if (not session_id or not selection_id or not isinstance(matched, list)
                or not matched or any(not isinstance(rule_id, str) or not rule_id for rule_id in matched)):
            self._update(app, state="REJECTED_INVALID_DECISION", last_error="SELECT_DECISION_IDENTITY_INVALID")
            log.error("[SHADOW_SIGNAL] rejected SELECT with incomplete identity session=%s round=%s",
                      session_id or "UNKNOWN", round_id)
            return "SKIPPED"

        engine = getattr(app.state, "rules_engine", None)
        selection_context = getattr(engine, "signal_selection_context", None)
        if callable(selection_context):
            detail = selection_context(session_id, selection_id)
        else:
            detail = engine.session(session_id) if engine is not None else None
        if not isinstance(detail, dict):
            return self._blocked(app, "SHADOW_SESSION_DETAIL_UNAVAILABLE", session_id, selection_id)
        if "selection" in detail:
            selection = detail.get("selection")
        else:
            selections = list(detail.get("selections") or []) if isinstance(detail.get("selections"), list) else []
            rule_selections = detail.get("rule_selections")
            if isinstance(rule_selections, Mapping):
                for rows in rule_selections.values():
                    if isinstance(rows, list):
                        selections.extend(rows)
            selection = next((row for row in selections
                              if isinstance(row, Mapping)
                              and str(row.get("selection_id") or row.get("decision_id") or "") == selection_id), None)
        expected = (session_record.get("target_round") or
                    (selection or {}).get("expected_target"))
        if not isinstance(expected, Mapping):
            # A committed evaluation is itself the durable decision record. A
            # missing live ledger row must not strand the entire SHADOW bridge.
            return self._skip_stale_selection(app, session_id, selection_id)
        selection_status = selection.get("status") if isinstance(selection, Mapping) else "PENDING"
        settled_shadow_selection = selection_status in {"WIN", "LOSS"}
        if isinstance(selection, Mapping) and selection_status != "PENDING" and not settled_shadow_selection:
            return self._skip_stale_selection(app, session_id, selection_id,
                                              str(expected.get("roundId") or "") or None)

        # A resolved result may still be emitted as simulation-only while its
        # target is absent from the committed stream. The signal validator
        # rejects it once that target has committed.
        try:
            current_index = int(detail.get("last_round_index"))
        except (TypeError, ValueError, OverflowError):
            current_index = source_index
        if current_index > source_index and not settled_shadow_selection:
            return self._skip_stale_selection(app, session_id, selection_id,
                                              str(expected.get("roundId") or "") or None)

        try:
            expected_index = int(expected.get("roundIndex", expected.get("round_index")))
        except (TypeError, ValueError, OverflowError):
            return self._blocked(app, "SELECTION_TARGET_INDEX_UNAVAILABLE", session_id, selection_id)
        if expected_index != source_index + 1:
            return self._blocked(app, "SELECTION_TARGET_NOT_IMMEDIATE_SUCCESSOR", session_id,
                                 selection_id)
        if str(expected.get("streamId", expected.get("stream_id")) or "") != stream_id:
            return self._blocked(app, "SELECTION_TARGET_STREAM_MISMATCH", session_id,
                                 selection_id)
        if settled_shadow_selection:
            actual = selection.get("actual_target") if isinstance(selection, Mapping) else None
            try:
                actual_index = int(actual.get("roundIndex")) if isinstance(actual, Mapping) else 0
            except (TypeError, ValueError, OverflowError):
                actual_index = 0
            if (not isinstance(actual, Mapping) or actual.get("streamId") != stream_id
                    or actual_index != expected_index or not actual.get("roundId")):
                return self._skip_stale_selection(app, session_id, selection_id,
                                                  str(expected.get("roundId") or "") or None)

        # The Spribe frame does not expose the synthetic data-aviator-* marker
        # used by the optional phase observer. Preserve verified provider IDs
        # when present; otherwise publish a simulation-only stream/index target.
        # Provider proof still gates external target publication. This fallback
        # is only enough to retain the local SHADOW simulation target.
        phase = getattr(app.state, "latest_provider_phase_observation", None)
        phase_source_index = None
        phase_target_index = None
        if isinstance(phase, Mapping):
            try:
                phase_source_index = int(phase.get("source_round_index"))
                phase_target_index = int(phase.get("target_round_index"))
            except (TypeError, ValueError, OverflowError):
                pass
        if (phase_source_index is not None and phase_source_index > source_index
                and not settled_shadow_selection):
            return self._skip_stale_selection(app, session_id, selection_id,
                                              str(expected.get("roundId") or "") or None)
        provider_target_id = str(phase.get("canonical_target_round_id") or "") if isinstance(phase, Mapping) else ""
        provider_target_verified = bool(
            isinstance(phase, Mapping)
            and phase.get("phase") == "BETTING_OPEN"
            and phase.get("source_round_id") == round_id
            and phase_source_index == source_index
            and phase_target_index == expected_index
            and phase.get("target_identity_verified") is True
            and phase.get("target_started") is False
            and provider_target_id
        )
        target_identity_status = "PROVIDER_VERIFIED" if provider_target_verified else "INDEX_ONLY_SHADOW"
        if settled_shadow_selection and provider_target_verified:
            # A provider-verified target must still be open when emitted. The
            # late-result path below is reserved for simulation-only targets.
            return self._skip_stale_selection(app, session_id, selection_id,
                                              str(expected.get("roundId") or "") or None)
        target_id = (provider_target_id if provider_target_verified
                     else f"shadow-index:{stream_id}:{expected_index}")

        # Active sessions evaluate their frozen rule_ids snapshot. Global rule
        # toggles configure future sessions and may change after this decision
        # was produced; using them here would discard a valid session result.
        selected_rules = signals_api._session_snapshot_selected(detail)
        matched_rules = [str(rule_id) for rule_id in matched]
        selection_rules = (selection.get("contributing_rule_ids")
                           or selection.get("matched_rule_ids")
                           if isinstance(selection, Mapping) else None)
        valid_selection_rules = (
            isinstance(selection_rules, list)
            and all(isinstance(rule_id, str) and rule_id for rule_id in selection_rules)
            and len(selection_rules) == len(set(selection_rules))
        )
        if (not selected_rules or not set(matched_rules).issubset(set(selected_rules))
                or not valid_selection_rules
                or set(matched_rules) != set(selection_rules)):
            self._update(app, state="REJECTED_INVALID_DECISION", last_error="TRUE_RULES_NOT_IN_ACTIVE_SELECTION")
            log.error("[SHADOW_SIGNAL] rejected SELECT outside active session snapshot session=%s selection=%s true_rules=%s session_rules=%s saved_selection_rules=%s",
                      session_id, selection_id, matched_rules, selected_rules, selection_rules)
            return "SKIPPED"

        reasons = decision.get("contributor_reasons")
        description_parts = []
        if isinstance(reasons, list):
            description_parts = [f"{item.get('rule_id')}: {item.get('reason')}" for item in reasons
                                 if isinstance(item, dict) and item.get("rule_id") in matched_rules
                                 and item.get("reason")]
        description = "; ".join(description_parts) or (
            "TRUE rules: " + ", ".join(matched_rules))
        payload = {
            "action": "BET", "rule": matched_rules[0], "description": description,
            "selected_rules": selected_rules, "matched_rules": matched_rules,
            "round_id": round_id, "target_round_id": target_id,
            "target_stream_id": stream_id, "target_round_index": expected_index,
            "source_stream_id": stream_id, "source_round_index": source_index,
            "target_identity_status": target_identity_status,
            "target_identity_verified": provider_target_verified,
            "provider_target_id": provider_target_id if provider_target_verified else None,
            "shadow_result_after_observation": settled_shadow_selection,
            "session_id": session_id, "mode": "SHADOW",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "signal_id": f"shadow-{selection_id}", "decision_id": selection_id,
        }
        self._update(app, state="PUBLISHING", last_attempt_at=payload["timestamp"],
                     last_decision_id=selection_id, last_target_round_id=target_id,
                     last_target_identity_status=target_identity_status, last_error=None)
        try:
            published = await signals_api.publish_internal_signal(app, payload,
                allow_index_only_shadow=not provider_target_verified,
                observed_source_event=event if source_is_verified_observation else None,
                decision_started_ns=(decision_started_ns if observed_source else None),
                backend_received_ns=(backend_received_ns if observed_source else None),
                collector_detection_at=collector_detection_at,
                collector_receipt_at=collector_receipt_at,
                trusted_selection_id=selection_id)
        except HTTPException as exc:
            detail_error = exc.detail.get("error") if isinstance(exc.detail, dict) else str(exc.detail)
            self._update(app, last_publication_error=str(detail_error or "signal_publish_failed"),
                         last_failed_decision_id=selection_id)
            if detail_error in {"target_round_already_completed", "shadow_target_round_already_completed"}:
                return self._skip_stale_selection(app, session_id, selection_id, target_id)
            self._update(app, state="RETRYING" if exc.status_code >= 500 else "BLOCKED",
                         last_error=str(detail_error or "signal_publish_failed"))
            if exc.status_code >= 500:
                log.exception("[SHADOW_SIGNAL] durable publish failed session=%s selection=%s target=%s",
                              session_id, selection_id, target_id)
                raise
            log.warning("[SHADOW_SIGNAL] selection not publishable session=%s selection=%s target=%s reason=%s",
                        session_id, selection_id, target_id, detail_error)
            return "SKIPPED"

        signal = published.get("signal") or {}
        self._update(app,
            state="PUBLISHED" if published.get("accepted") else "ALREADY_PUBLISHED",
            last_signal_id=signal.get("signal_id"),
            last_target_round_index=expected_index,
            last_target_round_id=signal.get("target_round_id") or target_id,
            last_target_identity_status=signal.get("target_identity_status") or target_identity_status,
            last_published_at=datetime.now(timezone.utc).isoformat() if published.get("accepted")
                else self._status.get("last_published_at"),
            last_error=None,
            published_count=int(self._status["published_count"]) + (1 if published.get("accepted") else 0),
            duplicate_count=int(self._status["duplicate_count"]) + (1 if published.get("duplicate") else 0),
        )
        log.info("[SHADOW_SIGNAL] signal %s session=%s decision=%s source=%s target=%s signal_id=%s",
                 "published" if published.get("accepted") else "already persisted",
                 session_id, selection_id, round_id, target_id, signal.get("signal_id"))
        return "PUBLISHED" if published.get("accepted") else "DUPLICATE"

    async def _settle_signals_for_target(self, app: Any, event: Mapping[str, Any],
                                         signals: list[dict[str, Any]] | None = None) -> int:
        target_round_id = str(event.get("round_id") or "")
        if not target_round_id:
            return 0
        repository = signals_api._repository(app)
        if signals is None:
            try:
                signals = await asyncio.to_thread(repository.bet_signals_for_target_round, target_round_id)
                index_lookup = getattr(repository, "bet_signals_for_target_round_index", None)
                if callable(index_lookup):
                    try:
                        stream_id = str(event.get("stream_id") or "")
                        target_index = int(event.get("round_index"))
                        index_signals = await asyncio.to_thread(index_lookup, stream_id, target_index)
                        signals.extend(index_signals)
                    except (TypeError, ValueError, OverflowError):
                        pass
            except Exception as exc:
                log.exception("[SHADOW_SIGNAL] could not load signals for committed target=%s", target_round_id)
                raise HTTPException(503, detail={"error": "signal_settlement_lookup_unavailable"}) from exc

        engine = getattr(app.state, "rules_engine", None)
        settled = 0
        for signal in signals:
            if not isinstance(signal, dict):
                continue
            if signal.get("settlement_status") == "COMMITTED":
                if signal.get("source_id") == "BEHAVIOUR_DETECTION":
                    try:
                        await asyncio.to_thread(repository.update_bet_signal_settlement,
                            str(signal.get("signal_id") or ""),
                            str(signal.get("target_round_id") or target_round_id),
                            signal.get("settlement") or {})
                        feedback = await asyncio.to_thread(
                            repository.behaviour_strategy_settlement_feedback,
                            str(signal.get("signal_id") or ""))
                        if isinstance(feedback, dict):
                            await _deliver_behaviour_settlement(app, feedback)
                    except Exception:
                        log.exception("[SHADOW_SETTLEMENT_FEEDBACK] committed result recovery failed signal=%s",
                                      signal.get("signal_id"))
                continue
            decision_id = str(signal.get("decision_id") or "")
            session_id = str(signal.get("session_id") or "")
            detail = engine.session(session_id) if engine is not None and session_id else None
            if not isinstance(detail, dict) or not decision_id:
                continue
            selections = list(detail.get("selections") or [])
            selections.extend(selection for rows in (detail.get("rule_selections") or {}).values()
                              for selection in rows)
            selection = next((item for item in selections if isinstance(item, dict)
                              and str(item.get("selection_id") or item.get("decision_id") or "") == decision_id), None)
            if not isinstance(selection, dict) or selection.get("status") not in {"WIN", "LOSS"}:
                continue
            actual = selection.get("actual_target") or {}
            evidence = selection.get("settlement_evidence") or {}
            index_only_shadow = signal.get("target_identity_status") == "INDEX_ONLY_SHADOW"
            persistence_proof = evidence.get("persistence_confirmation")
            if not isinstance(persistence_proof, Mapping):
                persistence_proof = evidence
            proof_target_id = persistence_proof.get("target_round_id")
            proof_stream_id = persistence_proof.get("stream_id")
            proof_index = persistence_proof.get("target_round_index", evidence.get("target_round_index"))
            proof_multiplier = persistence_proof.get("target_multiplier", evidence.get("target_multiplier"))
            try:
                event_index = int(event.get("round_index"))
                actual_index = int(actual.get("roundIndex"))
                event_multiplier = float(event.get("multiplier"))
                actual_multiplier = float(actual.get("multiplier"))
                proof_index = int(proof_index) if proof_index is not None else None
                proof_multiplier = float(proof_multiplier) if proof_multiplier is not None else None
            except (TypeError, ValueError, OverflowError):
                continue
            if (selection.get("target_persistence_status") != "COMMITTED"
                    or actual.get("streamId") != event.get("stream_id")
                    or actual.get("roundId") != target_round_id
                    or actual_index != event_index
                    or (index_only_shadow and (
                        signal.get("target_stream_id") != event.get("stream_id")
                        or signal.get("target_round_index") != event_index))
                    or not math.isclose(actual_multiplier, event_multiplier, rel_tol=0, abs_tol=0.000001)
                    or persistence_proof.get("event_type") != "round.persisted"
                    or persistence_proof.get("durability") != "COMMITTED"
                    or persistence_proof.get("continuity_status") != "VERIFIED"
                    or proof_target_id != target_round_id
                    or proof_stream_id != event.get("stream_id")
                    or (proof_index is not None and proof_index != event_index)
                    or (proof_multiplier is not None and not math.isclose(
                        proof_multiplier, event_multiplier, rel_tol=0, abs_tol=0.000001))):
                continue
            settlement = {
                "status": "COMMITTED", "target_round_id": target_round_id,
                "target_stream_id": str(event.get("stream_id") or ""),
                "target_round_index": event_index, "actual_multiplier": event_multiplier,
                "result": selection["status"], "settled_at": selection.get("settled_at"),
                "profit_loss_rwf": selection.get("profit_loss_rwf"),
                "evidence": {**dict(evidence),
                    "target_round_id": target_round_id,
                    "target_stream_id": str(event.get("stream_id") or ""),
                    "target_round_index": event_index},
            }
            try:
                updated = await asyncio.to_thread(repository.update_bet_signal_settlement,
                    str(signal.get("signal_id") or ""),
                    str(signal.get("target_round_id") or target_round_id), settlement)
            except Exception as exc:
                log.exception("[SHADOW_SIGNAL] could not persist committed result signal=%s target=%s",
                              signal.get("signal_id"), target_round_id)
                raise HTTPException(503, detail={"error": "signal_settlement_persistence_unavailable"}) from exc
            if updated and updated.get("settlement_status") == "COMMITTED":
                settled += 1
                log.info("[SHADOW_SIGNAL] result committed signal=%s target=%s result=%s multiplier=%s",
                         signal.get("signal_id"), target_round_id, settlement["result"], event_multiplier)
                if updated.get("source_id") == "BEHAVIOUR_DETECTION":
                    feedback = await asyncio.to_thread(
                        repository.behaviour_strategy_settlement_feedback,
                        str(signal.get("signal_id") or ""))
                    if isinstance(feedback, dict):
                        await _deliver_behaviour_settlement(app, feedback)
        return settled

    def _schedule_pending_settlement_recovery(self, app: Any) -> None:
        task = self._settlement_recovery_task
        if task is None or task.done():
            self._settlement_recovery_task = asyncio.create_task(
                self._run_pending_settlement_recovery(app),
                name="shadow-pending-settlement-recovery")

    def start_pending_settlement_recovery(self, app: Any) -> None:
        """Recover durable SHADOW results as soon as the service starts."""
        self._schedule_pending_settlement_recovery(app)

    async def _run_pending_settlement_recovery(self, app: Any) -> None:
        try:
            settled = await self._retry_pending_settlements(app)
        except Exception:
            log.exception("[SHADOW_SIGNAL] pending settlement recovery failed")
            return
        repository = signals_api._repository(app)
        snapshot_loader = getattr(repository, "shadow_signal_bridge_status_snapshot", None)
        changes: dict[str, Any] = {}
        if callable(snapshot_loader):
            try:
                snapshot = await asyncio.to_thread(snapshot_loader)
            except Exception:
                log.exception("[SHADOW_SIGNAL] durable bridge status lookup failed")
                snapshot = None
            if isinstance(snapshot, Mapping):
                changes["published_count"] = max(
                    int(self._status["published_count"]), int(snapshot.get("published_count") or 0))
                changes["settled_count"] = max(
                    int(self._status["settled_count"]), int(snapshot.get("settled_count") or 0))
                latest = snapshot.get("last_signal")
                if isinstance(latest, Mapping):
                    latest_at = str(latest.get("timestamp") or "")
                    current_at = str(self._status.get("last_published_at") or "")
                    if latest_at and latest_at > current_at:
                        changes.update({
                            "last_published_at": latest_at,
                            "last_signal_id": latest.get("signal_id"),
                            "last_decision_id": latest.get("decision_id"),
                            "last_target_round_id": latest.get("target_round_id"),
                            "last_target_round_index": latest.get("target_round_index"),
                            "last_target_identity_status": latest.get("target_identity_status"),
                        })
            else:
                changes["settled_count"] = int(self._status["settled_count"]) + settled
        else:
            changes["settled_count"] = int(self._status["settled_count"]) + settled
        if changes:
            self._update(app, **changes)

    async def _retry_pending_settlements(self, app: Any) -> int:
        """Backfill outcomes for earlier committed targets after an outage."""
        repository = signals_api._repository(app)
        pending_loader = getattr(repository, "pending_bet_signals", None)
        by_id = getattr(repository, "committed_round_distribution_event_by_id", None)
        by_index = getattr(repository, "committed_round_distribution_event_by_index", None)
        if not callable(pending_loader) or not callable(by_id) or not callable(by_index):
            return 0
        try:
            pending = await asyncio.to_thread(pending_loader, 1000)
        except Exception:
            log.exception("[SHADOW_SIGNAL] pending settlement recovery lookup failed")
            return 0

        events: dict[tuple[str, str, int], tuple[dict[str, Any], list[dict[str, Any]]]] = {}
        event_cache: dict[tuple[str, str, int], dict[str, Any] | None] = {}
        for signal in pending:
            if not isinstance(signal, dict):
                continue
            index_only = signal.get("target_identity_status") == "INDEX_ONLY_SHADOW"
            if index_only:
                stream_id = str(signal.get("target_stream_id") or "")
                try:
                    target_index = int(signal.get("target_round_index"))
                except (TypeError, ValueError, OverflowError):
                    continue
                if not stream_id or target_index < 1:
                    continue
                lookup = ("index", stream_id, target_index)
            else:
                target_round_id = str(signal.get("target_round_id") or "")
                if not target_round_id:
                    continue
                lookup = ("round", target_round_id, 0)
            if lookup not in event_cache:
                try:
                    if lookup[0] == "index":
                        event_cache[lookup] = await asyncio.to_thread(by_index, lookup[1], lookup[2])
                    else:
                        event_cache[lookup] = await asyncio.to_thread(by_id, lookup[1])
                except Exception:
                    log.exception("[SHADOW_SIGNAL] committed target lookup failed during settlement recovery")
                    event_cache[lookup] = None
            event = event_cache[lookup]
            if not isinstance(event, dict) or not _is_committed_verified(event):
                continue
            try:
                event_index = int(event.get("round_index"))
            except (TypeError, ValueError, OverflowError):
                continue
            if (index_only and (event.get("stream_id") != signal.get("target_stream_id")
                                or event_index != int(signal.get("target_round_index")))
                    or not index_only and event.get("round_id") != signal.get("target_round_id")):
                continue
            identity = (str(event.get("stream_id") or ""),
                        str(event.get("round_id") or ""), event_index)
            if identity not in events:
                events[identity] = (event, [])
            events[identity][1].append(signal)

        settled = 0
        for event, signals in events.values():
            settled += await self._settle_signals_for_target(app, event, signals)
        feedback_loader = getattr(repository, "pending_behaviour_strategy_settlements", None)
        if callable(feedback_loader):
            try:
                feedback_events = await asyncio.to_thread(feedback_loader, 100)
            except Exception:
                log.exception("[SHADOW_SETTLEMENT_FEEDBACK] retry outbox lookup failed")
                feedback_events = []
            for feedback_event in feedback_events:
                if isinstance(feedback_event, dict):
                    await _deliver_behaviour_settlement(app, feedback_event)
        return settled

    async def recover_for_open_phase(self, app: Any, phase: Mapping[str, Any]) -> dict[str, Any]:
        """Retry a stored committed SELECT after a fresh target-open phase arrives."""
        if phase.get("phase") != "BETTING_OPEN" or phase.get("target_started") is not False:
            return {"status": "PHASE_NOT_OPEN", "published": 0}
        repository = signals_api._repository(app)
        try:
            snapshot = await asyncio.to_thread(repository.round_distribution_snapshot)
        except Exception as exc:
            log.exception("[SHADOW_SIGNAL] latest committed source lookup failed")
            raise HTTPException(503, detail={"error": "signal_recovery_source_unavailable"}) from exc
        event = snapshot.get("latest") if isinstance(snapshot, dict) else None
        if (not isinstance(event, dict)
                or str(event.get("round_id") or "") != str(phase.get("source_round_id") or "")):
            return {"status": "PHASE_SOURCE_NOT_LATEST_COMMITTED", "published": 0}
        engine = getattr(app.state, "rules_engine", None)
        if engine is None:
            return {"status": "RULES_ENGINE_UNAVAILABLE", "published": 0}
        # observe_round is identity-idempotent. Replaying the committed event
        # returns its saved decision and cannot create a second selection.
        evaluation = await asyncio.to_thread(engine.observe_round, event,
            target_already_completed=(str(event.get("delivery_mode") or "LIVE").upper() != "LIVE"))
        return await self.process_committed_round(app, event, evaluation)
