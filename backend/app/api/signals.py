from __future__ import annotations

import asyncio
from collections import deque
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from functools import partial
from typing import Any, Literal, Mapping

from fastapi import APIRouter, Header, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.requests import ClientDisconnect

from app.api import round_distribution as round_distribution_api

router = APIRouter(prefix="/api/v1", tags=["bet signals"])
log = logging.getLogger(__name__)

MAX_SIGNAL_AGE_SECONDS = 30.0
MAX_PHASE_AGE_SECONDS = 15.0
SIGNAL_QUEUE_SIZE = 128
SIGNAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
PUBLISHER_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
WS_TICKET_TTL_SECONDS = 30
WS_AUTH_TIMEOUT_SECONDS = 5
PUBLISHER_HEARTBEAT_STALE_SECONDS = 45
REQUIRED_SIGNAL_FIELDS = {
    "action", "rule", "description", "selected_rules", "matched_rules",
    "round_id", "target_round_id", "session_id", "mode", "timestamp",
}


def _schedule_rules_session_checkpoint(app: Any, engine: Any, session_id: str) -> None:
    """Persist a just-published Behaviour selection off the signal hot path."""
    queue = getattr(app.state, "rules_state_persistence_queue", None)
    worker = getattr(app.state, "rules_state_persistence_task", None)
    if queue is not None and worker is not None and not worker.done():
        try:
            # The ordered persistence worker coalesces snapshots and retries DB
            # failures. Empty event coverage means this checkpoints only the
            # session snapshot; it does not acknowledge a round inbox entry.
            queue.put_nowait(([], "", "", time.perf_counter_ns(), [session_id]))
            return
        except asyncio.QueueFull:
            log.warning("[BEHAVIOUR_SESSION_CHECKPOINT] queue full session_id=%s", session_id)

    tasks = getattr(app.state, "behaviour_snapshot_tasks", None)
    if tasks is None:
        tasks = set()
        app.state.behaviour_snapshot_tasks = tasks

    async def persist_with_retry() -> None:
        for attempt in range(5):
            try:
                persist = partial(engine.persist_current_snapshot, session_ids={session_id})
                executor = getattr(app.state, "round_persistence_executor", None)
                if executor is None:
                    await asyncio.to_thread(persist)
                else:
                    await asyncio.get_running_loop().run_in_executor(executor, persist)
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("[BEHAVIOUR_SESSION_CHECKPOINT] failed session_id=%s attempt=%s",
                              session_id, attempt + 1)
                await asyncio.sleep(min(2.0, 0.1 * (2 ** attempt)))

    task = asyncio.create_task(persist_with_retry(), name=f"behaviour-session-checkpoint-{session_id}")
    tasks.add(task)
    task.add_done_callback(tasks.discard)


class LatestSignalActionResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={"examples": [
            {"action": "OK", "index": "#20063"},
            {"action": "WAIT", "index": "#20064"},
        ]},
    )

    action: Literal["OK", "WAIT"] = Field(
        description=("A projection of the latest canonical published signal decision: BET maps to "
                     "OK, and a canonical published WAIT remains WAIT."))
    index: str | None = Field(
        description=("The published signal's actual target_round_index, formatted as "
                     "#<round number>. It is never inferred from another field."),
        pattern=r"^#[1-9][0-9]*$")


class LatestSignalNoDataResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ok: Literal[True]
    signal: None


SIGNAL_OPENAPI_EXTRA = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": sorted(REQUIRED_SIGNAL_FIELDS),
                    "properties": {
                        "action": {"type": "string", "const": "BET"},
                        "rule": {"type": ["string", "null"],
                                 "description": "Primary matched rule chosen by the authoritative engine, when applicable."},
                        "description": {"type": "string", "description": "Why the matched rules evaluated TRUE."},
                        "selected_rules": {"type": "array", "items": {"type": "string"}, "uniqueItems": True,
                                           "description": "All user selected and enabled rules in the active session."},
                        "matched_rules": {"type": "array", "items": {"type": "string"}, "uniqueItems": True,
                                          "description": "Selected rules that the authoritative engine evaluated TRUE; an empty list is acknowledged without publishing."},
                        "round_id": {"type": "string", "description": "Committed source round ID."},
                        "target_round_id": {"type": "string", "description": "Provider verified target ID for external submissions. Built-in SHADOW responses can use a synthetic index-only key when provider IDs are unavailable; check target_identity_status."},
                        "target_identity_status": {"type": "string", "enum": ["PROVIDER_VERIFIED", "INDEX_ONLY_SHADOW"],
                                                   "description": "INDEX_ONLY_SHADOW is simulation-only and is never accepted from external publishers."},
                        "target_stream_id": {"type": "string"},
                        "target_round_index": {"type": "integer", "minimum": 1,
                                               "description": "Trusted collector target index; provider-verified submissions use the server-validated collector value."},
                        "decision_id": {"type": "string", "description": "Stable originating Shadow selection identifier, when published by the built-in engine."},
                        "session_id": {"type": "string"},
                        "mode": {"type": "string", "const": "SHADOW"},
                        "timestamp": {"type": "string", "format": "date-time"},
                        "signal_id": {"type": "string", "description": "Optional caller idempotency identifier."},
                    },
                    "example": {
                        "action": "BET", "rule": "RULE_1", "description": "RULE_1 returned TRUE",
                        "selected_rules": ["RULE_1", "RULE_2"], "matched_rules": ["RULE_1"],
                        "round_id": "source-round-id", "target_round_id": "verified-target-round-id",
                        "session_id": "shadow-session-id", "mode": "SHADOW",
                        "timestamp": "2026-10-17T12:00:00Z",
                    },
                },
            },
        },
    },
}


def _timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError, OverflowError):
        return None
    if result.tzinfo is None:
        return None
    return result.astimezone(timezone.utc)


def _app(request_or_app: Any):
    return getattr(request_or_app, "app", request_or_app)


def _repository(request_or_app: Any):
    app = _app(request_or_app)
    repository = getattr(getattr(app.state, "wp", None), "repository", None)
    if repository is None or not getattr(repository, "database_url", None):
        raise HTTPException(503, detail={"error": "authoritative_postgresql_unavailable"})
    return repository


async def _priority_database_call(app: Any, function, *args, **kwargs):
    """Run latency-critical signal validation and persistence outside the default executor."""
    executor = getattr(getattr(app, "state", None), "signal_priority_executor", None)
    if executor is None:
        return await asyncio.to_thread(function, *args, **kwargs)
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(executor, partial(function, *args, **kwargs))


def _setting_secret(settings: Any, name: str) -> str:
    value = getattr(settings, name, None)
    if hasattr(value, "get_secret_value"):
        return str(value.get_secret_value() or "")
    return str(value or "")


def _allowed_browser_origins(settings: Any) -> set[str]:
    return set(getattr(settings, "cors_origins", []) or []) | set(
        getattr(settings, "round_stream_allowed_origins", []) or [])


def _hub(request_or_app: Any) -> "BetSignalHub":
    app = _app(request_or_app)
    hub = getattr(app.state, "bet_signal_hub", None)
    if hub is None:
        hub = BetSignalHub()
        app.state.bet_signal_hub = hub
    return hub


def _require_engine_auth(request: Request) -> None:
    round_distribution_api._require_https_or_loopback(request)
    settings = getattr(request.app.state, "settings", None)
    supplied = request.headers.get("authorization", "")
    if supplied.startswith("Bearer "):
        raw_token = supplied[7:].strip()
        publisher_key = _setting_secret(settings, "rules_publisher_api_key")
        if publisher_key and hmac.compare_digest(raw_token, publisher_key):
            return
        if 32 <= len(raw_token) <= 256:
            try:
                token = _repository(request).external_project_token(
                    hashlib.sha256(raw_token.encode("utf-8")).hexdigest())
            except Exception as exc:
                raise HTTPException(503, detail={"error": "publisher_key_store_unavailable"}) from exc
            scopes = token.get("scopes", []) if token else []
            if (token and token.get("key_type") == "publisher"
                    and "signals:write" in scopes and not _token_is_expired(token)):
                limiter = getattr(request.app.state, "round_distribution", None)
                if limiter is not None:
                    allowed, retry_after = limiter.allow_request(token["token_id"])
                    if not allowed:
                        raise HTTPException(429, detail={"error": "publisher_rate_limit_exceeded"},
                                            headers={"Retry-After": str(retry_after)})
                touch = getattr(_repository(request), "touch_external_project_token", None)
                if callable(touch):
                    try:
                        touch(token["token_id"])
                    except Exception as exc:
                        raise HTTPException(503, detail={"error": "publisher_key_store_unavailable"}) from exc
                return
    request.app.state.rules_publisher_last_auth_failure_at = datetime.now(timezone.utc).isoformat()
    raise HTTPException(401, detail={"error": "rule_engine_authentication_required"},
                        headers={"WWW-Authenticate": "Bearer"})


def _token_is_expired(token: dict) -> bool:
    expires = token.get("expires_at")
    if expires is None:
        return False
    if isinstance(expires, datetime):
        parsed = expires
    else:
        try:
            parsed = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
        except (TypeError, ValueError, OverflowError):
            return True
    if parsed.tzinfo is None:
        return True
    return parsed.astimezone(timezone.utc) <= datetime.now(timezone.utc)


async def _authenticate_consumer(request: Request, *, required_scope: str = "signals:read") -> dict:
    round_distribution_api._require_https_or_loopback(request)
    round_distribution_api._validate_origin(request)
    authorization = request.headers.get("authorization", "")
    if not authorization.startswith("Bearer "):
        raise HTTPException(401, detail={"error": "project_token_required"},
                            headers={"WWW-Authenticate": "Bearer"})
    raw_token = authorization[7:].strip()
    if not 32 <= len(raw_token) <= 256:
        raise HTTPException(401, detail={"error": "invalid_project_token"})
    repository = _repository(request)
    try:
        token = await asyncio.to_thread(
            repository.external_project_token,
            hashlib.sha256(raw_token.encode("utf-8")).hexdigest())
    except Exception as exc:
        raise HTTPException(503, detail={"error": "project_token_store_unavailable"}) from exc
    if not token or token.get("key_type", "consumer") != "consumer" or _token_is_expired(token):
        raise HTTPException(401, detail={"error": "invalid_or_revoked_project_token"})
    scopes = token.get("scopes", ["signals:read", "rules:read", "metrics:read"])
    if isinstance(scopes, str):
        try:
            scopes = json.loads(scopes)
        except (TypeError, json.JSONDecodeError):
            scopes = []
    if required_scope not in scopes:
        raise HTTPException(403, detail={"error": "project_token_scope_required",
                                         "required_scope": required_scope})
    allowed, retry_after = request.app.state.round_distribution.allow_request(token["token_id"])
    if not allowed:
        raise HTTPException(429, detail={"error": "project_rate_limit_exceeded"},
                            headers={"Retry-After": str(retry_after)})
    touch = getattr(repository, "touch_external_project_token", None)
    if callable(touch):
        try:
            await asyncio.to_thread(touch, token["token_id"])
        except Exception as exc:
            raise HTTPException(503, detail={"error": "project_token_store_unavailable"}) from exc
    return token


def _active_session(request_or_app: Any, session_id: str | None = None, *,
                    allow_saved_selection: bool = False) -> tuple[dict, dict]:
    app = _app(request_or_app)
    engine = getattr(app.state, "rules_engine", None)
    if engine is None:
        raise HTTPException(503, detail={"error": "rules_engine_unavailable"})
    if allow_saved_selection:
        # Internal SHADOW publication already carries the exact session ID and
        # decision ID produced by the engine. Read only the small validation
        # snapshot so a growing session history is never deep-copied on the
        # signal's critical path.
        lightweight_reader = getattr(engine, "signal_validation_session", None)
        if callable(lightweight_reader):
            try:
                detail = lightweight_reader(session_id, allow_saved_selection=True)
            except TypeError:
                # Compatibility for external adapters implementing the older
                # single-argument lightweight reader.
                detail = lightweight_reader(session_id)
        else:
            session_reader = getattr(engine, "session", None)
            detail = session_reader(session_id) if callable(session_reader) else None
        active = detail or {}
    else:
        lightweight_reader = getattr(engine, "signal_validation_session", None)
        if callable(lightweight_reader):
            detail = lightweight_reader(session_id)
            active = detail or {}
        else:
            # Compatibility for test adapters and older engine implementations.
            state = engine.status() or {}
            active = state.get("active_session") or {}
            if (not active or active.get("status") != "RUNNING"
                    or active.get("selection_status") != "ACTIVE"
                    or (session_id is not None and str(active.get("session_id")) != str(session_id))):
                raise HTTPException(409, detail={"error": "active_shadow_rules_session_required"})
            detail = engine.session(str(active.get("session_id")))
    active_now = (active and active.get("status") == "RUNNING"
                  and active.get("selection_status") == "ACTIVE")
    saved_selection_settling = (
        allow_saved_selection and active
        and active.get("status") == "SETTLING"
        and active.get("selection_status") == "STOPPED"
    )
    detail_now = (detail and detail.get("status") == "RUNNING"
                  and detail.get("selection_status") == "ACTIVE")
    saved_detail_settling = (
        allow_saved_selection and detail
        and detail.get("status") == "SETTLING"
        and detail.get("selection_status") == "STOPPED"
    )
    if (not active or not detail
            or (not active_now and not saved_selection_settling)
            or (not detail_now and not saved_detail_settling)
            or (session_id is not None and str(active.get("session_id")) != str(session_id))):
        raise HTTPException(409, detail={"error": "active_shadow_rules_session_required"})
    return active, detail


def _currently_enabled_selected(engine: Any, detail: dict) -> list[str]:
    rule_ids = detail.get("rule_ids")
    if not isinstance(rule_ids, list):
        return []
    configuration = getattr(engine, "rule_configuration", None)
    if not isinstance(configuration, dict):
        return []
    enabled: list[str] = []
    for raw_id in rule_ids:
        rule_id = str(raw_id)
        row = configuration.get(rule_id)
        if isinstance(row, dict) and row.get("configured") is True and row.get("enabled") is True:
            enabled.append(rule_id)
    return enabled


def _session_snapshot_selected(detail: dict) -> list[str]:
    """Return the rules frozen into this active session at its start boundary."""
    rule_ids = detail.get("rule_ids")
    if (not isinstance(rule_ids, list)
            or any(not isinstance(rule_id, str) or not rule_id for rule_id in rule_ids)
            or len(rule_ids) != len(set(rule_ids))):
        return []
    return list(rule_ids)


def _selection_context(engine: Any, detail: dict, session_id: str,
                       selection_id: str) -> dict | None:
    """Read a persisted active-session selection for trusted bridge publication."""
    reader = getattr(engine, "signal_selection_context", None)
    context = reader(session_id, selection_id) if callable(reader) else None
    if isinstance(context, dict):
        return context

    session_reader = getattr(engine, "session", None)
    session = session_reader(session_id) if callable(session_reader) else detail
    if not isinstance(session, dict):
        return None
    selections = list(session.get("selections") or [])
    rule_selections = session.get("rule_selections")
    if isinstance(rule_selections, dict):
        for rows in rule_selections.values():
            if isinstance(rows, list):
                selections.extend(rows)
    selection = next((row for row in selections
                      if isinstance(row, dict)
                      and str(row.get("selection_id") or row.get("decision_id") or "")
                          == selection_id), None)
    if selection is None:
        return None
    return {"session_id": str(session.get("session_id") or ""),
            "status": session.get("status"),
            "selection_status": session.get("selection_status"),
            "selection_stop_reason": session.get("selection_stop_reason"),
            "selection_stop_source": session.get("selection_stop_source"),
            "selection_stopped_at_round_id": session.get("selection_stopped_at_round_id"),
            "rule_ids": list(session.get("rule_ids") or []),
            "selection": selection}


async def _validate_signal_body(raw: Any, request_or_app: Any, *,
                                allow_index_only_shadow: bool = False,
                                observed_source_event: Mapping[str, Any] | None = None,
                                trusted_selection_id: str | None = None,
                                trusted_behaviour_signal_id: str | None = None) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        raise HTTPException(422, detail={"error": "signal_must_be_an_object"})
    missing = sorted(REQUIRED_SIGNAL_FIELDS - raw.keys())
    if missing:
        raise HTTPException(422, detail={"error": "required_signal_fields_missing", "fields": missing})
    if raw.get("action") != "BET":
        raise HTTPException(422, detail={"error": "published_signal_action_must_be_BET"})
    if raw.get("mode") != "SHADOW":
        raise HTTPException(422, detail={"error": "signal_mode_must_be_SHADOW"})

    session_id = raw.get("session_id")
    round_id = raw.get("round_id")
    target_round_id = raw.get("target_round_id")
    if not all(isinstance(value, str) and value.strip() and len(value) <= 256
               for value in (session_id, round_id, target_round_id)):
        raise HTTPException(422, detail={"error": "round_and_session_identities_required"})
    if round_id == target_round_id:
        raise HTTPException(422, detail={"error": "source_and_target_round_must_differ"})
    selected = raw.get("selected_rules")
    matched = raw.get("matched_rules")
    if (not isinstance(selected, list) or any(not isinstance(item, str) or not item for item in selected)
            or len(selected) != len(set(selected))):
        raise HTTPException(422, detail={"error": "selected_rules_must_be_unique_rule_ids"})
    if (not isinstance(matched, list) or any(not isinstance(item, str) or not item for item in matched)
            or len(matched) != len(set(matched))):
        raise HTTPException(422, detail={"error": "matched_rules_must_be_unique_rule_ids"})
    if not matched and trusted_behaviour_signal_id is None:
        # An empty TRUE set is an ordinary no-signal evaluation.
        return None
    if not isinstance(raw.get("description"), str) or not raw["description"].strip():
        raise HTTPException(422, detail={"error": "matched_rule_description_required"})
    primary = raw.get("rule")
    if primary is not None and (not isinstance(primary, str) or primary not in matched):
        raise HTTPException(422, detail={"error": "primary_rule_must_be_a_matched_rule"})

    stamp = _timestamp(raw.get("timestamp"))
    if stamp is None:
        raise HTTPException(422, detail={"error": "signal_timestamp_must_be_timezone_aware_iso8601"})
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    if age < -5 or age > MAX_SIGNAL_AGE_SECONDS:
        raise HTTPException(409, detail={"error": "signal_is_stale_or_future_dated"})

    app = _app(request_or_app)
    validation_correlation_id = str(raw.get("round_id") or raw.get("decision_id") or "")
    latency_hub = getattr(app.state, "round_distribution", None)

    def record_validation_stage(name: str, started_ns: int) -> None:
        recorder = getattr(latency_hub, "record_priority_latency", None)
        if callable(recorder) and validation_correlation_id:
            recorder(f"signal_validation_{name}_ms",
                     f"{validation_correlation_id}:signal_validation_{name}_ms",
                     max(0.0, (time.perf_counter_ns() - started_ns) / 1_000_000.0))

    session_validation_started_ns = time.perf_counter_ns()
    active, detail = _active_session(app, str(session_id),
                                     allow_saved_selection=trusted_selection_id is not None)
    engine = app.state.rules_engine
    if trusted_behaviour_signal_id is not None:
        context = engine.session(str(session_id))
        selection = next((item for item in (context or {}).get("selections", [])
                          if isinstance(item, dict)
                          and str(item.get("selection_id") or "") == trusted_behaviour_signal_id), None)
        if (not isinstance(context, dict) or context.get("status") != "RUNNING"
                or context.get("selection_status") != "ACTIVE"
                or str(raw.get("decision_id") or "") != trusted_behaviour_signal_id
                or not isinstance(selection, dict)
                or selection.get("source_id") != "BEHAVIOUR_DETECTION"
                or selection.get("status") != "PENDING"
                or str(selection.get("series_id") or "") != str(raw.get("series_id") or "")
                or str(selection.get("part") or "") != str(raw.get("part") or "")
                or str(selection.get("batch_id") or "") != str(raw.get("batch_id") or "")
                or matched != ["BEHAVIOUR_ROLLING_HIGH"]):
            raise HTTPException(409, detail={"error": "behaviour_selection_not_active_in_session"})
        enabled_selected = selected
    elif trusted_selection_id is not None:
        if str(raw.get("decision_id") or "") != trusted_selection_id:
            raise HTTPException(409, detail={"error": "trusted_selection_identity_mismatch"})
        context = _selection_context(engine, detail, str(session_id), trusted_selection_id)
        selection = context.get("selection") if isinstance(context, dict) else None
        session_rules = (_session_snapshot_selected(context)
                         if isinstance(context, dict) else [])
        selection_rules = (selection.get("contributing_rule_ids")
                           or selection.get("matched_rule_ids")
                           if isinstance(selection, dict) else None)
        valid_selection_rules = (
            isinstance(selection_rules, list)
            and all(isinstance(rule_id, str) and rule_id for rule_id in selection_rules)
            and len(selection_rules) == len(set(selection_rules))
        )
        session_is_active = bool(
            isinstance(context, dict)
            and context.get("status") == "RUNNING"
            and context.get("selection_status") == "ACTIVE"
        )
        automatic_stop_sources = {
            "MAX_SELECTIONS_REACHED": "MAX_SELECTIONS",
            "PROFIT_TARGET_REACHED": "PROFIT_TARGET",
            "WIN_LIMIT_REACHED": "NET_OUTCOME_LIMIT",
            "LOSS_LIMIT_REACHED": "NET_OUTCOME_LIMIT",
        }
        stop_reason = context.get("selection_stop_reason") if isinstance(context, dict) else None
        stop_source = context.get("selection_stop_source") if isinstance(context, dict) else None
        source_round = selection.get("source_round") if isinstance(selection, dict) else None
        saved_source_round_id = (source_round.get("roundId")
                                 if isinstance(source_round, dict) else
                                 selection.get("source_round_id") if isinstance(selection, dict) else None)
        automatic_settling_selection = bool(
            isinstance(context, dict)
            and context.get("status") == "SETTLING"
            and context.get("selection_status") == "STOPPED"
            and automatic_stop_sources.get(stop_reason) == stop_source
            and str(context.get("selection_stopped_at_round_id") or "") == str(round_id)
            and str(saved_source_round_id or "") == str(round_id)
            and isinstance(selection, dict)
            and selection.get("status") == "PENDING"
        )
        if (not context or (not session_is_active and not automatic_settling_selection)
                or str(context.get("session_id") or "") != str(session_id)
                or not session_rules
                or not valid_selection_rules
                or set(matched) != set(selection_rules)):
            raise HTTPException(409, detail={"error": "trusted_selection_not_active_in_session"})
        enabled_selected = session_rules
    else:
        enabled_selected = _currently_enabled_selected(engine, detail)
    if not enabled_selected and trusted_behaviour_signal_id is None:
        raise HTTPException(409, detail={"error": "no_selected_enabled_rules_in_active_session"})
    if trusted_behaviour_signal_id is None and set(selected) != set(enabled_selected):
        raise HTTPException(422, detail={"error": "selected_rules_do_not_match_active_enabled_selection",
                                         "selected_rules": enabled_selected})
    if trusted_behaviour_signal_id is None and not set(matched).issubset(set(enabled_selected)):
        raise HTTPException(422, detail={"error": "matched_rule_was_not_selected_and_enabled"})
    record_validation_stage("session_selection", session_validation_started_ns)

    repository = _repository(app)
    index_only_shadow = bool(allow_index_only_shadow
        and raw.get("target_identity_status") == "INDEX_ONLY_SHADOW")
    observed_source_verified = False
    if observed_source_event is not None:
        try:
            observed_index = int(observed_source_event.get("round_index"))
            raw_source_index = int(raw.get("source_round_index"))
        except (TypeError, ValueError, OverflowError):
            observed_index = raw_source_index = 0
        observed_source_verified = bool(
            observed_source_event.get("event_type") == "round.observed"
            and observed_source_event.get("durability") == "PENDING"
            and observed_source_event.get("continuity_status") == "VERIFIED"
            and str(observed_source_event.get("round_id") or "") == str(round_id)
            and str(observed_source_event.get("stream_id") or "") == str(raw.get("source_stream_id") or "")
            and raw_source_index > 0 and observed_index == raw_source_index
            and raw.get("mode") == "SHADOW")
        if not observed_source_verified:
            raise HTTPException(409, detail={"error": "shadow_observed_source_evidence_invalid"})
    if index_only_shadow:
        target_validation_started_ns = time.perf_counter_ns()
        target_stream_id = raw.get("target_stream_id")
        source_index_raw = raw.get("source_round_index")
        target_index_raw = raw.get("target_round_index")
        source_index = (source_index_raw if isinstance(source_index_raw, int)
                        and not isinstance(source_index_raw, bool) else 0)
        target_index = (target_index_raw if isinstance(target_index_raw, int)
                        and not isinstance(target_index_raw, bool) else 0)
        expected_key = f"shadow-index:{target_stream_id}:{target_index}"
        if (not isinstance(target_stream_id, str) or target_stream_id != raw.get("source_stream_id")
                or source_index < 1 or target_index != source_index + 1
                or target_round_id != expected_key
                or raw.get("target_identity_verified") is not False
                or raw.get("provider_target_id") is not None):
            raise HTTPException(409, detail={"error": "shadow_target_index_identity_invalid"})
        stream_getter = getattr(repository, "round_distribution_stream_id", None)
        try:
            current_stream_id = getattr(app.state, "round_stream_id", None)
            if not isinstance(current_stream_id, str) and callable(stream_getter):
                current_stream_id = await _priority_database_call(app, stream_getter)
            if current_stream_id != target_stream_id:
                raise HTTPException(409, detail={"error": "shadow_target_stream_is_not_current"})
            source_matches = observed_source_verified or await _priority_database_call(app,
                repository.has_committed_round_distribution_identity,
                target_stream_id, str(round_id), source_index)
            # The trusted observed-round handoff runs before PostgreSQL
            # commits its source event. Its validated observation envelope is
            # sufficient source proof for this internal SHADOW call; the
            # immediate target must still be absent from committed history.
            if not source_matches and not observed_source_verified:
                raise HTTPException(409, detail={"error": "shadow_source_index_identity_mismatch"})
            source_metadata = (observed_source_event or {}).get("source_metadata")
            watch_source = (observed_source_verified and isinstance(source_metadata, Mapping)
                            and source_metadata.get("source") == "ORBIT_DATA_WATCH_API")
            if watch_source:
                latest = getattr(app.state, "latest_watch_observation", None)
                observed_at = _timestamp(observed_source_event.get("observed_at"))
                age = ((datetime.now(timezone.utc) - observed_at).total_seconds()
                       if observed_at else float("inf"))
                inbox = getattr(app.state, "round_observation_inbox", None)
                if (not isinstance(latest, dict) or latest.get("round_id") != round_id
                        or observed_source_event.get("delivery_mode") != "LIVE"
                        or not -2 <= age <= 15 or inbox is None):
                    raise HTTPException(409, detail={"error": "watch_source_stale_or_unavailable"})
                pending_target_check_started_ns = time.perf_counter_ns()
                target_exists = await _priority_database_call(
                    app, inbox.has_pending_round_index, target_index)
                record_validation_stage("pending_target_lookup", pending_target_check_started_ns)
            else:
                target_exists = await _priority_database_call(app,
                    repository.has_committed_round_distribution_index,
                    target_stream_id, target_index)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, detail={"error": "shadow_target_index_validation_unavailable"}) from exc
        if target_exists:
            raise HTTPException(409, detail={"error": "shadow_target_round_already_completed"})
        record_validation_stage("target_identity", target_validation_started_ns)
    else:
        phase = getattr(app.state, "latest_provider_phase_observation", None)
        if (not isinstance(phase, dict) or phase.get("phase") != "BETTING_OPEN"
                or phase.get("source_round_id") != round_id
                or phase.get("canonical_target_round_id") != target_round_id
                or phase.get("target_identity_verified") is not True
                or phase.get("target_started") is not False):
            raise HTTPException(409, detail={"error": "authoritative_target_identity_or_open_window_unavailable"})
        observed = _timestamp(phase.get("observed_at"))
        deadline = _timestamp(phase.get("betting_deadline"))
        now = datetime.now(timezone.utc)
        if (observed is None or deadline is None
                or (now - observed).total_seconds() < -2
                or (now - observed).total_seconds() > MAX_PHASE_AGE_SECONDS
                or deadline <= now):
            raise HTTPException(409, detail={"error": "target_round_window_expired_or_stale"})
        try:
            source_index = int(phase.get("source_round_index"))
            target_index = int(phase.get("target_round_index"))
        except (TypeError, ValueError, OverflowError):
            source_index, target_index = 0, 0
        if source_index < 1 or target_index != source_index + 1:
            raise HTTPException(409, detail={"error": "authoritative_immediate_target_identity_required"})
        try:
            target_exists = await _priority_database_call(app,
                repository.has_committed_round_distribution_event, str(target_round_id))
        except Exception as exc:
            raise HTTPException(503, detail={"error": "round_identity_validation_unavailable"}) from exc
        if target_exists:
            raise HTTPException(409, detail={"error": "target_round_already_completed"})

    source_validation_started_ns = time.perf_counter_ns()
    source_exists = observed_source_verified
    if not source_exists:
        try:
            source_exists = await _priority_database_call(app,
                repository.has_committed_round_distribution_event, str(round_id))
        except Exception as exc:
            raise HTTPException(503, detail={"error": "round_identity_validation_unavailable"}) from exc
    if not source_exists and not observed_source_verified:
        raise HTTPException(409, detail={"error": "signal_source_round_is_not_committed"})
    if target_exists:
        raise HTTPException(409, detail={"error": "target_round_already_completed"})
    record_validation_stage("source_presence", source_validation_started_ns)

    signal = dict(raw)
    signal["target_round_index"] = target_index
    signal["signal_id"] = str(raw.get("signal_id") or "bet-" + hashlib.sha256(
        (str(session_id) + "\0" + str(target_round_id)).encode("utf-8")).hexdigest())
    if not SIGNAL_ID_RE.fullmatch(signal["signal_id"]):
        raise HTTPException(422, detail={"error": "invalid_signal_id"})
    decision_id = raw.get("decision_id")
    if decision_id is not None:
        if not isinstance(decision_id, str) or not SIGNAL_ID_RE.fullmatch(decision_id):
            raise HTTPException(422, detail={"error": "invalid_decision_id"})
        signal["decision_id"] = decision_id
    # Settlement is written only by the built-in bridge after the matching
    # Aviator round has a committed PostgreSQL event. Never accept a publisher's
    # claimed result as settlement evidence.
    signal["settlement_status"] = "PENDING"
    signal["settlement"] = None
    if index_only_shadow:
        signal["target_identity_verified"] = False
        signal["provider_target_id"] = None
        signal["timing_status"] = "UNVERIFIED"
    else:
        # This is an authoritative classification derived from the validated
        # provider phase. Never preserve a publisher-supplied index-only label.
        signal["target_identity_status"] = "PROVIDER_VERIFIED"
        signal["target_identity_verified"] = True
    return signal


class BetSignalHub:
    """One-process, non-blocking fanout with durable PostgreSQL idempotency."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._subscribers: set[asyncio.Queue] = set()
        self._seen_targets: set[tuple[str, str]] = set()
        self._signals: deque[dict[str, Any]] = deque(maxlen=1000)
        self._fast_signal_ids: set[str] = set()
        self._subscriber_kinds: dict[asyncio.Queue, str] = {}
        self._delivery_latencies: deque[float] = deque(maxlen=1000)
        self._failed_deliveries = 0

    def subscribe(self, kind: str = "unknown") -> asyncio.Queue:
        queue: asyncio.Queue = asyncio.Queue(maxsize=SIGNAL_QUEUE_SIZE)
        self._subscribers.add(queue)
        self._subscriber_kinds[queue] = kind
        return queue

    def unsubscribe(self, queue: asyncio.Queue) -> None:
        self._subscribers.discard(queue)
        self._subscriber_kinds.pop(queue, None)

    def consumer_counts(self) -> dict[str, int]:
        return {"websocket": sum(kind == "websocket" for kind in self._subscriber_kinds.values()),
                "sse": sum(kind == "sse" for kind in self._subscriber_kinds.values()),
                "dashboard": sum(kind == "dashboard" for kind in self._subscriber_kinds.values())}

    def metrics(self) -> dict[str, Any]:
        average = (sum(self._delivery_latencies) / len(self._delivery_latencies)
                   if self._delivery_latencies else None)
        return {"failed_deliveries": self._failed_deliveries,
                "average_delivery_latency_ms": round(average, 3) if average is not None else None,
                "delivery_latency_basis": "publisher_to_fanout_queue_enqueue",
                "pending_persistence_jobs": 0,
                "subscriber_queues": len(self._subscribers)}

    def _remember(self, signal: dict[str, Any]) -> None:
        key = (str(signal["session_id"]), str(signal["target_round_id"]))
        self._seen_targets.add(key)
        if not any(item.get("signal_id") == signal.get("signal_id") for item in self._signals):
            self._signals.append(dict(signal))

    def trusted_duplicate(self, raw: Any, selection_id: str) -> dict[str, Any] | None:
        """Return only an exact already-accepted trusted selection for replay."""
        if not isinstance(raw, dict):
            return None
        session_id = str(raw.get("session_id") or "")
        target_id = str(raw.get("target_round_id") or "")
        if not session_id or not target_id or (session_id, target_id) not in self._seen_targets:
            return None
        prior = next((item for item in reversed(self._signals)
                      if str(item.get("session_id") or "") == session_id
                      and str(item.get("target_round_id") or "") == target_id), None)
        if prior is None:
            return None
        identity_fields = ("signal_id", "decision_id", "round_id", "target_stream_id",
                           "source_stream_id", "target_round_index", "source_round_index",
                           "selected_rules", "matched_rules")
        if (str(raw.get("decision_id") or "") != str(selection_id)
                or any(raw.get(field) != prior.get(field) for field in identity_fields)):
            raise HTTPException(409, detail={"error": "signal_id_identity_conflict"})
        return dict(prior)

    def _broadcast(self, signal: dict[str, Any]) -> None:
        for queue in tuple(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self._failed_deliveries += 1
            try:
                queue.put_nowait(dict(signal))
            except asyncio.QueueFull:
                # REST history remains replayable if an individual consumer is too slow.
                continue

    async def accept_fast(self, signal: dict[str, Any], inbox, *, executor=None) -> tuple[bool, dict[str, Any]]:
        async with self._lock:
            signal["distribution_status"] = "ACCEPTED"
            call = partial(inbox.enqueue_fast_signal, signal)
            if executor is None:
                inserted, saved = await asyncio.to_thread(inbox.enqueue_fast_signal, signal)
            else:
                inserted, saved = await asyncio.get_running_loop().run_in_executor(executor, call)
            if inserted:
                self._fast_signal_ids.add(str(saved.get("signal_id") or ""))
                self._remember(saved)
                self._broadcast(saved)
            return inserted, saved

    async def accept(self, signal: dict[str, Any], repository, *, executor=None,
                     on_durable_accept=None) -> tuple[bool, dict[str, Any]]:
        key = (str(signal["session_id"]), str(signal["target_round_id"]))
        async with self._lock:
            if key in self._seen_targets:
                prior = next((item for item in reversed(self._signals)
                              if (str(item.get("session_id")), str(item.get("target_round_id"))) == key), signal)
                return False, dict(prior)
            signal["distribution_status"] = "ACCEPTED"
            atomic_accept = getattr(repository, "accept_bet_signal", None)
            if callable(atomic_accept):
                try:
                    loop = asyncio.get_running_loop()
                    call = partial(atomic_accept, signal)
                    inserted, stored = (await asyncio.to_thread(atomic_accept, signal)
                                         if executor is None else
                                         await loop.run_in_executor(executor, call))
                except Exception as exc:
                    self._failed_deliveries += 1
                    log.exception("BET signal atomic persistence failed signal_id=%s",
                                  signal.get("signal_id"))
                    raise HTTPException(503, detail={"error": "signal_persistence_unavailable"}) from exc
                authoritative = dict(stored or signal)
                if not inserted:
                    same_exact_target = (
                        str(authoritative.get("session_id") or "") == key[0]
                        and str(authoritative.get("target_round_id") or "") == key[1])
                    same_target = same_exact_target or (
                        authoritative.get("target_identity_status") in {
                            "INDEX_ONLY_SHADOW", "PROVIDER_VERIFIED"}
                        and signal.get("target_identity_status") in {
                            "INDEX_ONLY_SHADOW", "PROVIDER_VERIFIED"}
                        and isinstance(authoritative.get("target_round_index"), int)
                        and not isinstance(authoritative.get("target_round_index"), bool)
                        and isinstance(signal.get("target_round_index"), int)
                        and not isinstance(signal.get("target_round_index"), bool)
                        and bool(authoritative.get("target_stream_id"))
                        and str(authoritative.get("target_stream_id") or "")
                            == str(signal.get("target_stream_id") or "")
                        and authoritative.get("target_round_index") == signal.get("target_round_index"))
                    if (not same_exact_target and (
                            str(authoritative.get("signal_id") or "") != str(signal.get("signal_id") or "")
                            or not same_target
                            or str(authoritative.get("session_id") or "") != key[0]
                            or str(authoritative.get("decision_id") or "")
                                != str(signal.get("decision_id") or "")
                            or str(authoritative.get("round_id") or "")
                                != str(signal.get("round_id") or ""))):
                        raise HTTPException(409, detail={"error": "signal_id_identity_conflict"})
                    self._remember(authoritative)
                    return False, authoritative

                # Only advertise the signal after the transaction has committed.
                accepted_ns = time.perf_counter_ns()
                if on_durable_accept is not None:
                    try:
                        on_durable_accept(accepted_ns)
                    except Exception:
                        log.exception("signal acceptance timing callback failed signal_id=%s",
                                      signal.get("signal_id"))
                self._remember(authoritative)
                started = time.perf_counter()
                self._broadcast(authoritative)
                self._delivery_latencies.append((time.perf_counter() - started) * 1000)
                return True, authoritative

            try:
                existing = await asyncio.to_thread(repository.bet_signal_for_target, key[0], key[1])
            except Exception as exc:
                raise HTTPException(503, detail={"error": "signal_duplicate_check_unavailable"}) from exc
            if existing:
                self._remember(existing)
                return False, dict(existing)
            signal_lookup = getattr(repository, "bet_signal_by_id", None)
            if callable(signal_lookup):
                try:
                    existing_id = await asyncio.to_thread(signal_lookup, str(signal["signal_id"]))
                except Exception as exc:
                    raise HTTPException(503, detail={"error": "signal_duplicate_check_unavailable"}) from exc
                if existing_id:
                    same_target = (
                        str(existing_id.get("target_round_id") or "")
                        == str(signal.get("target_round_id") or "")
                        or (existing_id.get("target_identity_status") in {
                                "INDEX_ONLY_SHADOW", "PROVIDER_VERIFIED"}
                            and signal.get("target_identity_status") in {
                                "INDEX_ONLY_SHADOW", "PROVIDER_VERIFIED"}
                            and isinstance(existing_id.get("target_round_index"), int)
                            and not isinstance(existing_id.get("target_round_index"), bool)
                            and isinstance(signal.get("target_round_index"), int)
                            and not isinstance(signal.get("target_round_index"), bool)
                            and bool(existing_id.get("target_stream_id"))
                            and str(existing_id.get("target_stream_id") or "")
                                == str(signal.get("target_stream_id") or "")
                            and existing_id.get("target_round_index")
                                == signal.get("target_round_index"))
                    )
                    same_decision_target = (
                        str(existing_id.get("session_id") or "") == key[0]
                        and str(existing_id.get("decision_id") or "") == str(signal.get("decision_id") or "")
                        and str(existing_id.get("round_id") or "") == str(signal.get("round_id") or "")
                        and same_target
                    )
                    if not same_decision_target:
                        raise HTTPException(409, detail={"error": "signal_id_identity_conflict"})
                    self._remember(existing_id)
                    return False, dict(existing_id)
            delay = 0.1
            stored = None
            inserted = False
            # This marker is written only by the canonical distribution path,
            # in the same transaction as the signal. Readers can distinguish
            # legacy/manual payloads that were persisted without acceptance.
            for attempt in range(8):
                try:
                    inserted, stored = await asyncio.to_thread(repository.insert_bet_signal, signal)
                    break
                except Exception as exc:
                    if attempt == 7:
                        self._failed_deliveries += 1
                        log.exception("BET signal persistence retries exhausted signal_id=%s",
                                      signal.get("signal_id"))
                        raise HTTPException(503, detail={"error": "signal_persistence_unavailable"}) from exc
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 2.0)
            authoritative = dict(stored or signal)
            if not inserted:
                # The database uniqueness constraint is the cross-process
                # idempotency authority. A concurrent publisher may have won.
                self._remember(authoritative)
                return False, authoritative

            # Only advertise the signal after the durable insert succeeds.
            accepted_ns = time.perf_counter_ns()
            if on_durable_accept is not None:
                try:
                    on_durable_accept(accepted_ns)
                except Exception:
                    log.exception("signal acceptance timing callback failed signal_id=%s",
                                  signal.get("signal_id"))
            self._remember(authoritative)
            started = time.perf_counter()
            self._broadcast(authoritative)
            self._delivery_latencies.append((time.perf_counter() - started) * 1000)
            return True, authoritative

    def wait_for_persistence(self, timeout: float = 2.0) -> None:
        # Retained as a compatibility no-op for callers of the old async
        # persistence API. accept() now returns only after the insert commits.
        return None

    async def list_signals(self, repository, limit: int, session_id: str | None = None,
                           app=None) -> list[dict[str, Any]]:
        if self._signals and limit == 1:
            latest = next((item for item in reversed(self._signals)
                           if session_id is None or item.get("session_id") == session_id), None)
            timestamp = _timestamp(latest.get("timestamp")) if latest else None
            if (latest is not None and str(latest.get("signal_id") or "") in self._fast_signal_ids
                    and timestamp is not None and 0 <= (datetime.now(timezone.utc) - timestamp).total_seconds() <= MAX_SIGNAL_AGE_SECONDS):
                return [dict(latest)]
        try:
            # Repository adapters use synchronous database drivers. Keep the
            # canonical feed read off the ASGI event loop so slow PostgreSQL
            # reads do not stall the action API, dashboard, or collector
            # callbacks sharing this process.
            saved = await asyncio.to_thread(
                repository.list_bet_signals, limit, session_id=session_id)
        except Exception as exc:
            raise HTTPException(503, detail={"error": "signal_store_unavailable"}) from exc
        # accept() commits before remembering or broadcasting. The durable
        # repository order therefore is the canonical publication order; do
        # not re-sort it by the publisher-supplied decision timestamp or let a
        # stale in-memory copy override a persisted settlement update.
        accepted = [dict(item) for item in saved if item
                    and item.get("distribution_status") in (None, "ACCEPTED")]
        return accepted[:limit]


async def run_fast_signal_persistence(app: Any) -> None:
    """Retry locally reserved signals in order without delaying live fanout."""
    inbox = app.state.round_observation_inbox
    while True:
        try:
            pending = await asyncio.to_thread(inbox.pending_fast_signals)
            for signal in pending:
                repository = _repository(app)
                inserted, stored = await _priority_database_call(app, repository.accept_bet_signal, signal)
                if not inserted and (not isinstance(stored, dict)
                        or stored.get("signal_id") != signal.get("signal_id")):
                    raise RuntimeError("signal persistence identity conflict")
                await asyncio.to_thread(inbox.mark_fast_signal_persisted, signal["signal_id"])
            await asyncio.sleep(0.2 if pending else 1.0)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("fast signal persistence deferred")
            await asyncio.sleep(2.0)


async def publish_internal_signal(app: Any, raw: Any, *,
                                  allow_index_only_shadow: bool = False,
                                  observed_source_event: Mapping[str, Any] | None = None,
                                  decision_started_ns: int | None = None,
                                  backend_received_ns: int | None = None,
                                  collector_detection_at: str | None = None,
                                  collector_receipt_at: str | None = None,
                                  trusted_selection_id: str | None = None,
                                  trusted_behaviour_signal_id: str | None = None) -> dict[str, Any]:
    """Validate and durably distribute a signal for trusted in-process callers."""
    started_ns = time.perf_counter_ns()
    signal_hub = _hub(app)
    trusted_id = trusted_selection_id or trusted_behaviour_signal_id
    if trusted_id is not None:
        prior = signal_hub.trusted_duplicate(raw, trusted_id)
        if prior is not None:
            return {"ok": True, "accepted": False, "duplicate": True,
                    "delivery": "ALREADY_PUBLISHED", "persistence": "EXISTING",
                    "signal": prior}
    signal = await _validate_signal_body(raw, app,
        allow_index_only_shadow=allow_index_only_shadow,
        observed_source_event=observed_source_event,
        trusted_selection_id=trusted_selection_id,
        trusted_behaviour_signal_id=trusted_behaviour_signal_id)
    validation_finished_ns = time.perf_counter_ns()
    if signal is None:
        return {"ok": True, "accepted": False, "duplicate": False,
                "delivery": "NO_SIGNAL", "persistence": "NONE", "reason": "NO_TRUE_RULES",
                "signal": None}
    app.state.last_received_bet_signal = dict(signal)
    app.state.last_received_bet_signal_at = datetime.now(timezone.utc).isoformat()
    metrics = getattr(app.state, "round_distribution", None)
    record_metric = getattr(metrics, "record_priority_latency", None)
    correlation_id = str(signal.get("round_id") or signal.get("decision_id") or "")

    def record_durable_acceptance(accepted_ns: int) -> None:
        if not callable(record_metric) or not correlation_id:
            return
        if isinstance(collector_receipt_at, str):
            try:
                receipt = datetime.fromisoformat(collector_receipt_at.replace("Z", "+00:00"))
                if receipt.tzinfo is not None:
                    elapsed = (datetime.now(timezone.utc) - receipt.astimezone(timezone.utc)).total_seconds() * 1000.0
                    if elapsed >= 0:
                        record_metric("collector_receipt_to_signal_api_acceptance_ms",
                                      f"{correlation_id}:collector_receipt_to_signal_api_acceptance_ms", elapsed)
            except (TypeError, ValueError, OverflowError):
                log.warning("collector receipt timestamp invalid for signal_id=%s", signal.get("signal_id"))
        if isinstance(collector_detection_at, str):
            try:
                detected_at = datetime.fromisoformat(
                    collector_detection_at.replace("Z", "+00:00"))
                if detected_at.tzinfo is not None:
                    end_to_end_ms = (datetime.now(timezone.utc)
                                     - detected_at.astimezone(timezone.utc)).total_seconds() * 1000.0
                    if end_to_end_ms >= 0:
                        record_metric(
                            "collector_detection_to_signal_api_acceptance_ms",
                            f"{correlation_id}:collector_detection_to_signal_api_acceptance_ms",
                            end_to_end_ms)
            except (TypeError, ValueError, OverflowError):
                log.warning("collector detection timestamp invalid for signal_id=%s",
                            signal.get("signal_id"))
        if decision_started_ns is not None:
            record_metric(
                "decision_to_signal_api_acceptance_ms",
                f"{correlation_id}:decision_to_signal_api_acceptance_ms",
                max(0.0, (accepted_ns - decision_started_ns) / 1_000_000.0))
        if backend_received_ns is not None:
            record_metric(
                "backend_receive_to_signal_api_acceptance_ms",
                f"{correlation_id}:backend_receive_to_signal_api_acceptance_ms",
                max(0.0, (accepted_ns - backend_received_ns) / 1_000_000.0))

    metadata = (observed_source_event or {}).get("source_metadata")
    fast_watch = (isinstance(metadata, dict) and metadata.get("source") == "ORBIT_DATA_WATCH_API"
                  and observed_source_event.get("delivery_mode") == "LIVE")
    if fast_watch:
        inbox = getattr(app.state, "round_observation_inbox", None)
        if inbox is None:
            raise HTTPException(503, detail={"error": "signal_outbox_unavailable"})
        durable_lock_started_ns = time.perf_counter_ns()
        accepted, authoritative = await signal_hub.accept_fast(
            signal, inbox, executor=getattr(app.state, "signal_priority_executor", None))
        durable_lock_acknowledged_ns = time.perf_counter_ns()
        if accepted:
            record_durable_acceptance(durable_lock_acknowledged_ns)
    else:
        durable_lock_started_ns = time.perf_counter_ns()
        accepted, authoritative = await signal_hub.accept(
            signal, _repository(app),
            executor=getattr(app.state, "signal_priority_executor", None),
            on_durable_accept=record_durable_acceptance)
        durable_lock_acknowledged_ns = time.perf_counter_ns()
    acknowledged_ns = time.perf_counter_ns()
    durable_lock_acknowledged_at = datetime.now(timezone.utc).isoformat()
    if accepted:
        app.state.rules_publisher_last_delivery = datetime.now(timezone.utc).isoformat()
    if callable(record_metric) and correlation_id:
        record_metric("signal_validation_ms",
            f"{correlation_id}:signal_validation_ms",
            (validation_finished_ns - started_ns) / 1_000_000.0)
        record_metric("signal_persist_and_fanout_ack_ms",
            f"{correlation_id}:signal_persist_and_fanout_ack_ms",
            (acknowledged_ns - validation_finished_ns) / 1_000_000.0)
        record_metric("signal_create_to_durable_ack_ms",
            f"{correlation_id}:signal_create_to_durable_ack_ms",
            (acknowledged_ns - started_ns) / 1_000_000.0)
    log.info("[SIGNAL_DELIVERY_ACK] correlation_id=%s signal_id=%s accepted=%s persistence=%s latency_ms=%.3f",
             correlation_id, signal.get("signal_id"), bool(accepted),
             "PENDING_POSTGRES" if fast_watch and accepted else "COMMITTED" if accepted else "EXISTING",
             (acknowledged_ns - started_ns) / 1_000_000.0)
    return {"ok": True, "accepted": accepted, "duplicate": not accepted,
            "delivery": "PUBLISHED" if accepted else "ALREADY_PUBLISHED",
            "persistence": ("PENDING_POSTGRES" if fast_watch and accepted else
                            "COMMITTED" if accepted else "EXISTING"),
            "rules_engine_acceptance_to_durable_lock_ms": round(
                (durable_lock_acknowledged_ns - durable_lock_started_ns) / 1_000_000.0, 3),
            "durable_lock_acknowledged_at": (durable_lock_acknowledged_at if accepted else None),
            "signal": authoritative}


@router.post("/internal/behaviour/strategy-eligibility")
async def accept_behaviour_strategy_eligibility(request: Request):
    """Consume a read-only Behaviour eligibility fact and run the Rules strategy."""
    peer = getattr(getattr(request, "client", None), "host", "")
    try:
        local_peer = ipaddress.ip_address(peer).is_loopback
    except ValueError:
        local_peer = False
    configured_secret = os.getenv("BEHAVIOUR_SHADOW_SHARED_SECRET", "")
    supplied_secret = request.headers.get("x-behaviour-shadow-secret", "")
    if not local_peer or (configured_secret and not hmac.compare_digest(configured_secret, supplied_secret)):
        raise HTTPException(403, detail={"error": "behaviour_signal_source_not_trusted"})
    try:
        body = await request.json()
    except ClientDisconnect:
        # A worker may cancel a slow delivery and retry the same eligibility
        # identity. Treat it as cancellation rather than an application crash.
        log.info("[BEHAVIOUR_ELIGIBILITY] client disconnected before request body was read")
        return JSONResponse(status_code=499, content={"ok": False, "error": "client_disconnected"})
    except ValueError as exc:
        raise HTTPException(422, detail={"error": "invalid_behaviour_signal_json"}) from exc
    if not isinstance(body, dict):
        raise HTTPException(422, detail={"error": "behaviour_signal_must_be_an_object"})
    required = ("eligibility_id", "decision_round_identity", "decision_timestamp",
                "source_round_id", "direction")
    if any(not isinstance(body.get(key), str) or not body[key].strip() for key in required):
        raise HTTPException(422, detail={"error": "behaviour_eligibility_fields_missing"})
    eligible = body.get("eligibility") == "ELIGIBLE"
    if (body.get("direction") != "HIGH" or body.get("strategy_id") != "ROLLING_HIGH"
            or body.get("eligibility") not in {"ELIGIBLE", "INELIGIBLE"}):
        raise HTTPException(422, detail={"error": "behaviour_strategy_state_invalid"})
    raw_configuration_version = body.get("configuration_version", 0)
    if (isinstance(raw_configuration_version, bool)
            or not isinstance(raw_configuration_version, int)
            or raw_configuration_version < 0):
        raise HTTPException(422, detail={"error": "behaviour_configuration_version_invalid"})
    # Eligibility already encodes the Behaviour state machine's rule. Its
    # rolling dominance value is descriptive: Part 1 continues through LOW,
    # and Part 2 resumes from the latest individual HIGH result even when the
    # rolling state is LOW_DOMINANT or BALANCED.
    if eligible and any(not isinstance(body.get(key), str) or not body[key].strip()
                        for key in ("series_id", "part", "batch_id", "trigger_round_identity")):
        raise HTTPException(422, detail={"error": "eligible_behaviour_strategy_context_missing"})

    app = request.app
    repository = _repository(app)
    engine = getattr(app.state, "rules_engine", None)
    if engine is None:
        raise HTTPException(503, detail={"error": "rules_engine_unavailable"})
    if eligible:
        signal_id = "behaviour-high-" + hashlib.sha256("\0".join((
            str(body.get("series_id") or ""), str(body.get("part") or ""),
            str(body.get("batch_id") or ""), str(body.get("decision_round_identity") or ""),
        )).encode()).hexdigest()
        body = {**body, "signal_id": signal_id}

    active = engine.session_for_strategy("BEHAVIOUR_ROLLING_HIGH")
    if active is None:
        return {"ok": True, "status": "NOT_SELECTED", "eligibility": body.get("eligibility")}
    session_id = str(active["session_id"])
    if not eligible:
        # An ineligible fact cannot publish a signal. Keep its status update in
        # memory so stale callbacks never wait on the shared DB or API inbox.
        recorded = engine.record_strategy_eligibility(
            session_id, body, {"round_id": body["source_round_id"]}, persist=False)
        return {"ok": True, "status": "ELIGIBILITY_RECORDED",
                "session_id": session_id, "strategy": recorded}

    inbox = getattr(app.state, "round_observation_inbox", None)
    latest_observation = getattr(app.state, "latest_verified_live_observation", None)
    observed_envelope = getattr(app.state, "latest_live_observed_envelope", None)
    if (isinstance(observed_envelope, dict)
            and observed_envelope.get("event_type") == "round.observed"
            and observed_envelope.get("durability") == "PENDING"
            and observed_envelope.get("continuity_status") == "VERIFIED"
            and str(observed_envelope.get("delivery_mode") or "").upper() == "LIVE"):
        observed = observed_envelope
        latest_api_round = (latest_observation if isinstance(latest_observation, dict)
                            and latest_observation.get("continuity_status") == "VERIFIED"
                            and str(latest_observation.get("delivery_mode") or "").upper() == "LIVE"
                            else observed_envelope)
        source_is_memory_observation = True
    elif (isinstance(latest_observation, dict)
            and latest_observation.get("continuity_status") == "VERIFIED"
            and str(latest_observation.get("delivery_mode") or "").upper() == "LIVE"):
        observed = latest_observation
        latest_api_round = latest_observation
        source_is_memory_observation = True
    else:
        observed = (await _priority_database_call(app, inbox.latest_pending_verified_round)
                    if inbox is not None else None)
        latest_api_round = (await _priority_database_call(app, inbox.latest_verified_round_event)
                            if inbox is not None else None)
        source_is_memory_observation = False
    source_is_live_observed = bool(
        isinstance(observed, dict)
        and str(observed.get("round_id") or "") == body["source_round_id"]
        and str(observed.get("stream_id") or "") == str(getattr(app.state, "round_stream_id", "")))
    source_is_latest_api_round = bool(
        source_is_live_observed or
        (isinstance(latest_api_round, dict)
         and str(latest_api_round.get("round_id") or "") == body["source_round_id"]
         and str(latest_api_round.get("stream_id") or "") == str(getattr(app.state, "round_stream_id", ""))))
    if eligible and not source_is_latest_api_round:
        try:
            prior = await asyncio.to_thread(repository.bet_signal_by_id, str(body["signal_id"]))
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"error": "signal_store_unavailable"}) from exc
        if prior is not None:
            identity_fields = ("strategy_id", "series_id", "part", "batch_id",
                               "trigger_round_identity", "decision_round_identity",
                               "source_round_id", "direction", "trigger_horizon", "policy_version",
                               "configuration_version")
            if (prior.get("source_id") != "BEHAVIOUR_DETECTION"
                    or any(str(prior.get(field) or "") != str(body.get(field) or "")
                           for field in identity_fields)):
                raise HTTPException(409, detail={"error": "behaviour_signal_id_identity_conflict"})
            settled = prior.get("settlement") or {}
            result = str(settled.get("result") or "").upper()
            status = (f"SETTLED_{result}" if prior.get("settlement_status") == "COMMITTED"
                      and result in {"WIN", "LOSS"} else "ALREADY_ACCEPTED")
            return {"ok": True, "status": status, "session_id": prior.get("session_id"),
                    "signal": prior, "delivery": "ALREADY_PUBLISHED",
                    "persistence": "EXISTING", "signal_lock_status": "EXISTING_DURABLE_SIGNAL"}
    if source_is_latest_api_round:
        # The durable API inbox already verifies identity, order and freshness;
        # don't block a fresh or just-committed observation on PostgreSQL.
        source = observed if source_is_live_observed else latest_api_round
        source_time = _timestamp(source.get("observed_at") or source.get("stored_at"))
    else:
        try:
            source = await _priority_database_call(app, repository.committed_round_distribution_event_by_id,
                                                   body["source_round_id"])
            snapshot = await _priority_database_call(app, repository.round_distribution_snapshot)
        except Exception as exc:
            raise HTTPException(status_code=503, detail={"error": "behaviour_source_validation_unavailable"}) from exc
        latest = snapshot.get("latest") if isinstance(snapshot, dict) else None
        source_is_committed_latest = bool(
            isinstance(source, dict) and source.get("event_type") == "round.persisted"
            and source.get("durability") == "COMMITTED"
            and source.get("continuity_status") == "VERIFIED"
            and isinstance(latest, dict)
            and str(latest.get("round_id") or "") == body["source_round_id"]
            and source.get("stream_id") == getattr(app.state, "round_stream_id", source.get("stream_id")))
        if not source_is_committed_latest:
            raise HTTPException(409, detail={"error": "behaviour_source_not_latest_verified_round"})
        source_time = _timestamp(source.get("observed_at") or source.get("stored_at"))
    if not isinstance(source, dict):
        raise HTTPException(409, detail={"error": "behaviour_source_not_latest_verified_round"})
    parsed_source_time = source_time if isinstance(source_time, datetime) else _timestamp(source_time)
    if parsed_source_time is None or not -2 <= (datetime.now(timezone.utc) - parsed_source_time).total_seconds() <= MAX_SIGNAL_AGE_SECONDS:
        raise HTTPException(409, detail={"error": "behaviour_source_stale"})
    try:
        source_index = int(source.get("round_index"))
        target_index = source_index + 1
    except (TypeError, ValueError, OverflowError) as exc:
        raise HTTPException(409, detail={"error": "behaviour_source_index_unavailable"}) from exc
    inbox = getattr(app.state, "round_observation_inbox", None)
    if (source_is_latest_api_round and not source_is_memory_observation and inbox is not None
            and await _priority_database_call(app, inbox.has_pending_round_index, target_index)):
        raise HTTPException(409, detail={"error": "behaviour_target_already_observed"})
    if not source_is_latest_api_round:
        if await _priority_database_call(app, repository.has_committed_round_distribution_index,
                                         source["stream_id"], target_index):
            raise HTTPException(409, detail={"error": "behaviour_target_already_completed"})
    body = {**body, "source_id": "BEHAVIOUR_DETECTION",
            "source_label": "Behaviour Detection / Rolling HIGH",
            "session_id": session_id, "mode": "SHADOW", "action": "BET",
            "rule": "BEHAVIOUR_ROLLING_HIGH",
            "description": "Behaviour Detection Rolling HIGH verified strategy",
            "selected_rules": [], "matched_rules": ["BEHAVIOUR_ROLLING_HIGH"],
            "round_id": str(source["round_id"]), "source_stream_id": str(source["stream_id"]),
            "source_round_index": source_index, "target_stream_id": str(source["stream_id"]),
            "target_round_index": target_index,
            "target_round_id": f"shadow-index:{source['stream_id']}:{target_index}",
            "target_identity_status": "INDEX_ONLY_SHADOW", "target_identity_verified": False,
            "provider_target_id": None, "timestamp": body["decision_timestamp"],
            "decision_id": body["signal_id"]}
    selection_created = False
    try:
        engine.register_strategy_selection(session_id, body, {
            "round_id": source["round_id"], "round_index": source_index,
            "stream_id": source["stream_id"], "observation_id": source.get("observation_id"),
            "multiplier": source.get("multiplier"), "outcome": source.get("outcome")},
            persist=False)
        selection_created = True
        published = await publish_internal_signal(app, body, allow_index_only_shadow=True,
            trusted_behaviour_signal_id=str(body["signal_id"]),
            observed_source_event=(source if source_is_live_observed else None),
            backend_received_ns=time.perf_counter_ns())
    except ValueError as exc:
        raise HTTPException(409, detail={"error": str(exc)}) from exc
    except HTTPException as exc:
        if selection_created and exc.status_code < 500:
            reason = exc.detail.get("error") if isinstance(exc.detail, dict) else str(exc.detail)
            engine.cancel_strategy_selection(session_id, str(body["signal_id"]), str(reason))
        raise
    _schedule_rules_session_checkpoint(app, engine, session_id)
    signal = published.get("signal") if isinstance(published, dict) else None
    accepted_at = published.get("durable_lock_acknowledged_at")
    durable = published.get("persistence") in {"COMMITTED", "PENDING_POSTGRES"}
    duplicate_status = (published.get("duplicate")
                        and published.get("persistence") == "EXISTING")
    return {"ok": True, "status": "ACCEPTED" if published.get("accepted") else
            "ALREADY_ACCEPTED" if published.get("duplicate") else "REJECTED",
            "session_id": session_id, "signal": signal, "delivery": published.get("delivery"),
            "persistence": published.get("persistence"),
            "signal_lock_status": ("DURABLY_LOCKED" if durable and published.get("accepted")
                                    else "EXISTING_DURABLE_SIGNAL" if duplicate_status
                                    else "NOT_LOCKED"),
            "signal_id": signal.get("signal_id") if isinstance(signal, dict) else None,
            "signal_accepted_at": accepted_at if durable and published.get("accepted") else None,
            "signal_locked_at": accepted_at if durable and published.get("accepted") else None,
            "lock_acknowledged_at": accepted_at if durable and published.get("accepted") else None,
            "rules_engine_acceptance_to_durable_lock_ms": published.get(
                "rules_engine_acceptance_to_durable_lock_ms")}


@router.post("/signals", status_code=202, openapi_extra=SIGNAL_OPENAPI_EXTRA,
             summary="Distribute an authoritative SHADOW BET signal",
             description=("Accepts an authoritative Rules Engine result after at least one selected rule evaluated TRUE. "
                          "This endpoint validates and durably distributes signals; it does not evaluate rules or place bets."))
async def publish_signal(request: Request):
    _require_engine_auth(request)
    request.app.state.rules_publisher_last_seen = datetime.now(timezone.utc).isoformat()
    request.app.state.rules_publisher_queue_status = "UNKNOWN"
    try:
        raw = await request.json()
    except Exception as exc:
        raise HTTPException(400, detail={"error": "invalid_json"}) from exc
    return await publish_internal_signal(request.app, raw)


@router.post("/publisher/heartbeat")
async def publisher_heartbeat(request: Request):
    _require_engine_auth(request)
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(422, detail={"error": "heartbeat_body_must_be_an_object"})
    publisher_id = body.get("publisher_id")
    if not isinstance(publisher_id, str) or not PUBLISHER_ID_RE.fullmatch(publisher_id):
        raise HTTPException(422, detail={"error": "publisher_id_required"})
    shadow_status = body.get("shadow_status")
    if shadow_status not in {"ACTIVE", "STOPPED", "UNKNOWN"}:
        raise HTTPException(422, detail={"error": "shadow_status_must_be_active_stopped_or_unknown"})
    raw_depth = body.get("queue_depth", 0)
    if isinstance(raw_depth, bool) or not isinstance(raw_depth, int) or not 0 <= raw_depth <= 1_000_000:
        raise HTTPException(422, detail={"error": "queue_depth_must_be_an_integer_between_0_and_1000000"})
    failed_deliveries = body.get("delivery_failures", 0)
    if isinstance(failed_deliveries, bool) or not isinstance(failed_deliveries, int) or not 0 <= failed_deliveries <= 1_000_000:
        raise HTTPException(422, detail={"error": "delivery_failures_must_be_an_integer_between_0_and_1000000"})
    queue_status = body.get("queue_status", "IDLE" if raw_depth == 0 else "QUEUED")
    if queue_status not in {"IDLE", "QUEUED", "RETRYING", "DEGRADED"}:
        raise HTTPException(422, detail={"error": "invalid_publisher_queue_status"})
    received_at = datetime.now(timezone.utc).isoformat()
    request.app.state.rules_publisher_last_seen = received_at
    request.app.state.rules_publisher_queue_depth = raw_depth
    request.app.state.rules_publisher_queue_status = queue_status
    request.app.state.rules_publisher_heartbeat = {
        "publisher_id": publisher_id,
        "shadow_status": shadow_status,
        "queue_depth": raw_depth,
        "queue_status": queue_status,
        "delivery_failures": failed_deliveries,
        "received_at": received_at,
    }
    request.app.state.rules_publisher_last_auth_success_at = received_at
    return {"ok": True, "publisher": "CONNECTED", "received_at": received_at,
            "heartbeat_interval_seconds": 15,
            "stale_after_seconds": PUBLISHER_HEARTBEAT_STALE_SECONDS}


@router.post("/ws/ticket")
async def create_websocket_ticket(request: Request):
    """Issue a one-use, 30 second ticket for browser WebSocket authentication."""
    token = await _authenticate_consumer(request, required_scope="signals:read")
    authorization = request.headers.get("authorization", "")
    raw_token = authorization[7:].strip()
    origin = request.headers.get("origin")
    if origin and origin not in _allowed_browser_origins(request.app.state.settings):
        raise HTTPException(403, detail={"error": "websocket_origin_not_allowed"})

    ticket = secrets.token_urlsafe(32)
    ticket_hash = hashlib.sha256(ticket.encode("utf-8")).hexdigest()
    issued_at = time.monotonic()
    tickets = getattr(request.app.state, "rules_signal_ws_tickets", None)
    if tickets is None:
        tickets = {}
        request.app.state.rules_signal_ws_tickets = tickets
        request.app.state.rules_signal_ws_ticket_lock = threading.Lock()
    lock = request.app.state.rules_signal_ws_ticket_lock
    with lock:
        for key, value in list(tickets.items()):
            if value["expires_at"] <= issued_at:
                tickets.pop(key, None)
        tickets[ticket_hash] = {
            "token_hash": hashlib.sha256(raw_token.encode("utf-8")).hexdigest(),
            "token_id": token["token_id"],
            "origin": origin,
            "expires_at": issued_at + WS_TICKET_TTL_SECONDS,
        }
    return JSONResponse({"ok": True, "ticket": ticket, "expires_in_seconds": WS_TICKET_TTL_SECONDS},
                        headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@router.get("/signals/latest")
async def latest_signal(request: Request):
    await _authenticate_consumer(request, required_scope="signals:read")
    rows = await _latest_signal_feed(request, 1)
    return {"ok": True, "signal": rows[0] if rows else None}


async def _latest_signal_feed(request_or_app: Any, limit: int,
                              session_id: str | None = None) -> list[dict[str, Any]]:
    """Read the same canonical signal distribution used by the detailed feed."""
    return await _hub(request_or_app).list_signals(
        _repository(request_or_app), limit, session_id=session_id)


@router.get(
    "/signals/latest/action",
    response_model=LatestSignalActionResponse | LatestSignalNoDataResponse,
    response_model_exclude_none=False,
    summary="Get the latest Aviator signal action (public)",
    description=(
        "GET /api/v1/signals/latest/action is a read-only projection of the latest canonical "
        "published signal from the same feed, ordering, and session scope as /signals/latest "
        "and /signals. The feed preserves the repository's newest-first publication-time order. "
        "A published BET decision maps to OK; a genuine canonical published WAIT "
        "is preserved as WAIT. No Rules Engine or collector decision is run while polling. "
        "The response for a signal contains exactly action and index. The index is the actual "
        "target_round_index on that published signal, formatted as #<round number>; "
        "source_round_index is never substituted. When there is no eligible canonical action, "
        "the endpoint returns the same no-data response as /signals/latest: "
        '{"ok":true,"signal":null}. The current signal publisher accepts BET decisions; it '
        "does not publish WAIT decisions. Polling does not create or publish signals. "
        "This endpoint keeps its existing anonymous public-read policy: HTTPS is required "
        "outside loopback, browser origins must be configured in CORS_ORIGINS or "
        "ROUND_STREAM_ALLOWED_ORIGINS, and requests are limited to 120 per minute per source IP."
    ),
    response_description=("A published action contains exactly action and index. When there is "
                          "no eligible published action, the response matches /signals/latest's "
                          "no-data body."),
    responses={
        200: {
            "description": "Latest canonical action projection or the /signals/latest no-data response.",
            "content": {"application/json": {"examples": {
                "ok": {"summary": "Published BET decision", "value": {
                    "action": "OK", "index": "#20063"}},
                "wait": {"summary": "Published WAIT decision", "value": {
                    "action": "WAIT", "index": "#20064"}},
            }}},
        },
        403: {"description": "Browser origin is not allowed."},
        426: {"description": "HTTPS is required for remote callers."},
        429: {"description": "Public read rate limit exceeded; see Retry-After."},
        503: {"description": "Authoritative PostgreSQL signal storage is unavailable."},
    },
    openapi_extra={"x-public-access": True, "x-api-key-required": False},
)
async def latest_signal_action(
    request: Request,
) -> LatestSignalActionResponse | LatestSignalNoDataResponse:
    await round_distribution_api._authorize_public_round_read(request)
    response, diagnostic = await evaluate_latest_signal_action(request.app)
    # Keep this diagnostic free of signal, round, session, and rule identifiers.
    # The public action is only a projection of an already distributed record.
    log.info("signal_action_projection %s", json.dumps(diagnostic,
        sort_keys=True, separators=(",", ":")))
    return response or LatestSignalNoDataResponse(ok=True, signal=None)


def _positive_round_index(value: Any) -> int | None:
    """Accept only an actual positive integer target index; never infer one."""
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        return None
    return value


def _project_published_signal(signal: Any) -> LatestSignalActionResponse | None:
    """Project a canonical distributed decision without rerunning its validation."""
    if not isinstance(signal, dict):
        return None
    distribution_status = signal.get("distribution_status")
    if distribution_status not in (None, "ACCEPTED"):
        return None
    target_index = _positive_round_index(signal.get("target_round_index"))
    source_action = signal.get("action")
    if source_action == "BET":
        action = "OK"
    elif source_action == "OK" and distribution_status == "ACCEPTED":
        action = "OK"
    elif source_action == "WAIT" and distribution_status == "ACCEPTED":
        # WAIT is surfaced only when it exists as an actual published record.
        action = "WAIT"
    else:
        return None
    return LatestSignalActionResponse(action=action, index=f"#{target_index}" if target_index else None)


async def evaluate_latest_signal_action(
    app: Any,
) -> tuple[LatestSignalActionResponse | None, dict[str, Any]]:
    """Read and project the same latest canonical signal as the detailed feed."""
    rows = await _latest_signal_feed(app, 1)
    latest = rows[0] if rows else None
    response = _project_published_signal(latest)
    distribution_status = (latest.get("distribution_status")
                           if isinstance(latest, dict) else None)
    signal_accepted = bool(
        latest is not None and distribution_status in (None, "ACCEPTED"))
    target_index = _positive_round_index(
        latest.get("target_round_index") if isinstance(latest, dict) else None)

    if latest is None:
        reason = "NO_SIGNAL"
    elif not signal_accepted:
        reason = "SIGNAL_NOT_ACCEPTED"
    elif target_index is None:
        reason = "TARGET_ROUND_INDEX_UNAVAILABLE"
    elif response is None:
        reason = "UNSUPPORTED_PUBLISHED_ACTION"
    elif response.action == "WAIT":
        reason = "CANONICAL_WAIT_PUBLISHED"
    else:
        reason = "CANONICAL_SIGNAL_PUBLISHED"

    diagnostic = {
        "event": "signal_action_projection",
        "diagnostics_available": True,
        "action": response.action if response else None,
        "index": response.index if response else None,
        "reason": reason,
        "wait_reason": reason if response and response.action == "WAIT" else None,
        "decision_found": latest is not None,
        "signal_persisted": latest is not None,
        "signal_accepted": signal_accepted,
        "session_matches": None,
        "target_index_available": target_index is not None,
        "target_identity_verified": (
            latest.get("target_identity_verified") is True
            if isinstance(latest, dict) else False
        ),
        "timing_verified": (
            latest.get("timing_status") == "VERIFIED"
            if isinstance(latest, dict) else False
        ),
        "rules_matched": bool(
            isinstance(latest, dict) and latest.get("matched_rules")
        ),
        "validation_passed": response is not None,
        "index_source": "signal" if target_index is not None else "none",
    }
    return response, diagnostic


@router.get("/signals")
async def signals(request: Request, limit: int = Query(100, ge=1, le=1000),
                  session_id: str | None = None):
    await _authenticate_consumer(request, required_scope="signals:read")
    rows = await _latest_signal_feed(request, limit, session_id)
    return {"ok": True, "signals": rows, "count": len(rows)}


@router.get("/signals/{signal_id}")
async def signal_by_id(signal_id: str, request: Request):
    await _authenticate_consumer(request, required_scope="signals:read")
    repository = _repository(request)
    try:
        rows = await _hub(request).list_signals(repository, 1000)
        signal = next((item for item in rows if str(item.get("signal_id")) == signal_id), None)
    except Exception as exc:
        raise HTTPException(503, detail={"error": "signal_store_unavailable"}) from exc
    if signal is None:
        raise HTTPException(404, detail={"error": "signal_not_found"})
    return {"ok": True, "signal": signal}


@router.get("/rules/selected")
async def selected_rules(request: Request):
    await _authenticate_consumer(request, required_scope="rules:read")
    try:
        active, detail = _active_session(request)
    except HTTPException as exc:
        if exc.status_code == 409:
            return {"ok": True, "session_id": None, "selected_rules": []}
        raise
    selected = _currently_enabled_selected(request.app.state.rules_engine, detail)
    return {"ok": True, "session_id": active.get("session_id"), "mode": "SHADOW",
            "selected_rules": selected}


@router.get("/rules/matched")
async def matched_rules(request: Request):
    await _authenticate_consumer(request, required_scope="rules:read")
    rows = await _hub(request).list_signals(_repository(request), 1)
    signal = rows[0] if rows else None
    return {"ok": True, "session_id": signal.get("session_id") if signal else None,
            "round_id": signal.get("round_id") if signal else None,
            "target_round_id": signal.get("target_round_id") if signal else None,
            "matched_rules": list(signal.get("matched_rules") or []) if signal else []}


def _event_chunk(signal: dict[str, Any]) -> str:
    return (f"id: {signal['signal_id']}\n"
            "event: bet_signal\n"
            "data: " + json.dumps(signal, sort_keys=True, separators=(",", ":"), default=str) + "\n\n")


@router.get("/stream/signals")
async def stream_signals(request: Request, after_signal_id: str | None = None,
                         last_event_id: str | None = Header(default=None, alias="Last-Event-ID")):
    await _authenticate_consumer(request, required_scope="signals:read")
    repository = _repository(request)
    hub = _hub(request)
    cursor = after_signal_id or last_event_id
    queue = hub.subscribe("sse")
    try:
        # Subscribe before replay so signals published during the DB read are
        # queued and cannot fall between replay and live delivery. Perform the
        # read before sending response headers so storage failures remain 503s.
        history = await hub.list_signals(repository, 1000)
    except Exception:
        hub.unsubscribe(queue)
        raise
    history.reverse()
    replay = history
    if cursor:
        cursor_index = next((i for i, row in enumerate(history)
                             if row.get("signal_id") == cursor), None)
        if cursor_index is not None:
            replay = history[cursor_index + 1:]

    async def body():
        seen: set[str] = set()
        try:
            for signal in replay:
                signal_id = str(signal.get("signal_id") or "")
                if signal_id and signal_id not in seen:
                    seen.add(signal_id)
                    yield _event_chunk(signal)
            while True:
                try:
                    signal = await asyncio.wait_for(queue.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                signal_id = str(signal.get("signal_id") or "")
                if signal_id and signal_id not in seen:
                    seen.add(signal_id)
                    yield _event_chunk(signal)
        finally:
            hub.unsubscribe(queue)

    return StreamingResponse(body(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no",
    })


async def _authenticate_websocket(websocket: WebSocket) -> dict[str, Any]:
    peer = websocket.client.host if websocket.client else ""
    host = websocket.url.hostname or ""
    local = peer in {"127.0.0.1", "::1", "testclient"} and host in {"127.0.0.1", "::1", "localhost", "testserver"}
    if websocket.url.scheme != "wss" and not local:
        await websocket.close(code=4403, reason="HTTPS_REQUIRED")
        raise WebSocketDisconnect(code=4403)
    origin = websocket.headers.get("origin")
    settings = getattr(websocket.app.state, "settings", None)
    if origin and origin not in _allowed_browser_origins(settings):
        await websocket.close(code=4403, reason="ORIGIN_NOT_ALLOWED")
        raise WebSocketDisconnect(code=4403)
    authorization = websocket.headers.get("authorization", "")
    token_value = authorization[7:].strip() if authorization.startswith("Bearer ") else ""
    admin_key = _setting_secret(settings, "api_key")
    if token_value and admin_key and hmac.compare_digest(token_value, admin_key):
        return {"kind": "dashboard"}
    if not token_value:
        try:
            message = await asyncio.wait_for(websocket.receive_json(), timeout=WS_AUTH_TIMEOUT_SECONDS)
        except (asyncio.TimeoutError, WebSocketDisconnect, ValueError):
            await websocket.close(code=4401, reason="WEBSOCKET_TICKET_REQUIRED")
            raise WebSocketDisconnect(code=4401)
        ticket_value = message.get("ticket") if isinstance(message, dict) and message.get("type") == "authenticate" else None
        if not isinstance(ticket_value, str) or not 32 <= len(ticket_value) <= 128:
            await websocket.close(code=4401, reason="WEBSOCKET_TICKET_REQUIRED")
            raise WebSocketDisconnect(code=4401)
        ticket_hash = hashlib.sha256(ticket_value.encode("utf-8")).hexdigest()
        tickets = getattr(websocket.app.state, "rules_signal_ws_tickets", {})
        lock = getattr(websocket.app.state, "rules_signal_ws_ticket_lock", None)
        ticket_record = None
        if lock is not None:
            with lock:
                ticket_record = tickets.pop(ticket_hash, None)
        if ticket_record is None:
            await websocket.close(code=4401, reason="WEBSOCKET_TICKET_INVALID_OR_USED")
            raise WebSocketDisconnect(code=4401)
        if ticket_record["expires_at"] <= time.monotonic():
            await websocket.close(code=4401, reason="WEBSOCKET_TICKET_EXPIRED")
            raise WebSocketDisconnect(code=4401)
        if origin != ticket_record.get("origin"):
            await websocket.close(code=4403, reason="WEBSOCKET_TICKET_ORIGIN_MISMATCH")
            raise WebSocketDisconnect(code=4403)
        repository = _repository(websocket)  # type: ignore[arg-type]
        try:
            token = await asyncio.to_thread(repository.external_project_token, ticket_record["token_hash"])
        except Exception:
            await websocket.close(code=1013, reason="PROJECT_TOKEN_STORE_UNAVAILABLE")
            raise WebSocketDisconnect(code=1013)
        if (not token or token.get("token_id") != ticket_record.get("token_id")
                or token.get("key_type", "consumer") != "consumer" or _token_is_expired(token)):
            await websocket.close(code=4401, reason="PROJECT_TOKEN_REVOKED_OR_EXPIRED")
            raise WebSocketDisconnect(code=4401)
        scopes = token.get("scopes", ["signals:read"])
        if isinstance(scopes, str):
            try:
                scopes = json.loads(scopes)
            except (TypeError, json.JSONDecodeError):
                scopes = []
        if "signals:read" not in scopes:
            await websocket.close(code=4403, reason="SIGNALS_READ_SCOPE_REQUIRED")
            raise WebSocketDisconnect(code=4403)
        allowed, _retry = websocket.app.state.round_distribution.allow_request(token["token_id"])
        if not allowed:
            await websocket.close(code=4429, reason="PROJECT_RATE_LIMIT_EXCEEDED")
            raise WebSocketDisconnect(code=4429)
        touch = getattr(repository, "touch_external_project_token", None)
        if callable(touch):
            await asyncio.to_thread(touch, token["token_id"])
        return {"kind": "websocket", "token_id": token["token_id"]}
    if not 32 <= len(token_value) <= 256:
        await websocket.close(code=4401, reason="INVALID_PROJECT_TOKEN")
        raise WebSocketDisconnect(code=4401)
    repository = _repository(websocket)  # type: ignore[arg-type]
    digest = hashlib.sha256(token_value.encode("utf-8")).hexdigest()
    try:
        token = await asyncio.to_thread(repository.external_project_token, digest)
    except Exception:
        await websocket.close(code=1013, reason="PROJECT_TOKEN_STORE_UNAVAILABLE")
        raise WebSocketDisconnect(code=1013)
    scopes = token.get("scopes", ["signals:read"]) if token else []
    if isinstance(scopes, str):
        try:
            scopes = json.loads(scopes)
        except (TypeError, json.JSONDecodeError):
            scopes = []
    if (not token or token.get("key_type", "consumer") != "consumer"
            or _token_is_expired(token)):
        await websocket.close(code=4401, reason="INVALID_OR_REVOKED_PROJECT_TOKEN")
        raise WebSocketDisconnect(code=4401)
    if "signals:read" not in scopes:
        await websocket.close(code=4403, reason="SIGNALS_READ_SCOPE_REQUIRED")
        raise WebSocketDisconnect(code=4403)
    allowed, _retry = websocket.app.state.round_distribution.allow_request(token["token_id"])
    if not allowed:
        await websocket.close(code=4429, reason="PROJECT_RATE_LIMIT_EXCEEDED")
        raise WebSocketDisconnect(code=4429)
    touch = getattr(repository, "touch_external_project_token", None)
    if callable(touch):
        touch(token["token_id"])
    return {"kind": "websocket", "token_id": token["token_id"]}


@router.websocket("/ws/signals")
async def websocket_signals(websocket: WebSocket):
    await websocket.accept()
    try:
        auth = await _authenticate_websocket(websocket)
    except WebSocketDisconnect:
        return
    repository = _repository(websocket)  # type: ignore[arg-type]
    hub = getattr(websocket.app.state, "bet_signal_hub", None)
    if hub is None:
        hub = BetSignalHub()
        websocket.app.state.bet_signal_hub = hub
    queue = hub.subscribe(auth.get("kind", "websocket"))
    try:
        await websocket.send_json({"type": "ready", "stream": "bet-signals"})
        await websocket.send_json({"type": "heartbeat", "timestamp": datetime.now(timezone.utc).isoformat()})
        history = await hub.list_signals(repository, 1000)
        history.reverse()
        sent = set()
        for signal in history:
            signal_id = str(signal.get("signal_id") or "")
            if signal_id and signal_id not in sent:
                sent.add(signal_id)
                await websocket.send_json({"type": "signal", "signal": signal})
        while True:
            try:
                signal = await asyncio.wait_for(queue.get(), timeout=15.0)
            except asyncio.TimeoutError:
                await websocket.send_json({"type": "heartbeat", "timestamp": datetime.now(timezone.utc).isoformat()})
                continue
            signal_id = str(signal.get("signal_id") or "")
            if signal_id and signal_id not in sent:
                sent.add(signal_id)
                await websocket.send_json({"type": "signal", "signal": signal})
    except WebSocketDisconnect:
        pass
    finally:
        hub.unsubscribe(queue)
