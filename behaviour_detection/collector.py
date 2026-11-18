from __future__ import annotations

import hashlib
import json
import logging
import time
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .analysis import classify_multiplier
from .models import (
    ApiError, CanonicalRound, CollectorActivity, OrbitReading, OrbitRoundMapping,
    OrbitWatchState, SystemHealth,
)
from .orbit_client import (
    PAYOUT_SELECTOR, OrbitContractError, OrbitDataWatchClient, ParsedReading,
    parse_reading, select_watch, validate_history_page, validate_latest_snapshot,
)

log = logging.getLogger("behaviour.collector")


def _normal(value: str) -> str:
    from decimal import Decimal
    return str(Decimal(value).normalize())


def _coalesce_exact_duplicate_halves(items: list[dict] | list[str]) -> tuple[list, bool]:
    """Collapse only an exact duplicate ordered DOM payout list, preserving both indices."""
    if (items and all(isinstance(item, dict) for item in items)
            and any(len(item.get("source_item_indices", [])) > 1 for item in items)):
        return items, True
    if len(items) < 40 or len(items) % 2:
        return items, False
    if not items or not all(isinstance(item, dict) for item in items):
        return items, False
    half = len(items) // 2
    if any(item.get("item_index") != index or item.get("visible") is not True
           or len(item.get("source_item_indices", [])) > 1
           for index, item in enumerate(items)):
        return items, False
    for left, right in zip(items[:half], items[half:]):
        if (left.get("multiplier_normalized") != right.get("multiplier_normalized")
                or left.get("tag_name") != right.get("tag_name")):
            return items, False
    collapsed = []
    for index, item in enumerate(items[:half]):
        entry = dict(item)
        entry["source_item_indices"] = [index, index + half]
        collapsed.append(entry)
    return collapsed, True


def _visible_dom(parsed: ParsedReading) -> list[dict]:
    items = [{"item_index": item.item_index, "multiplier_raw": item.multiplier_raw,
              "multiplier_normalized": _normal(item.multiplier_raw), "visible": True,
              "tag_name": item.tag_name}
             for item in parsed.items if item.visible]
    return _coalesce_exact_duplicate_halves(items)[0]


def _match_delta(previous: list[dict] | list[str], current: list[dict], min_overlap: int = 10) -> dict:
    """Infer a delta only from a unique overlap after exact duplicate DOM coalescing."""
    previous, previous_coalesced = _coalesce_exact_duplicate_halves(previous)
    current, current_coalesced = _coalesce_exact_duplicate_halves(current)
    if not previous or not current:
        return {"status": "CONTINUITY_UNVERIFIED", "new_items": [], "overlap": 0}
    prev = [p["multiplier_normalized"] if isinstance(p, dict) else _normal(str(p)) for p in previous]
    cur = [item["multiplier_normalized"] for item in current]
    if prev == cur or (len(cur) <= len(prev) and cur == prev[:len(cur)]):
        return {"status": "VERIFIED_NO_CHANGE", "new_items": [], "overlap": len(cur),
                "previous_duplicate_halves_coalesced": previous_coalesced,
                "current_duplicate_halves_coalesced": current_coalesced}
    matches = []
    for shift in range(1, max(1, len(cur) - min_overlap + 1)):
        overlap = min(len(prev), len(cur) - shift)
        if overlap >= min_overlap and prev[:overlap] == cur[shift:shift + overlap]:
            matches.append((shift, overlap))
    if len(matches) != 1:
        return {"status": "CONTINUITY_UNVERIFIED", "new_items": [],
                "overlap": max((x[1] for x in matches), default=0),
                "candidate_shifts": [x[0] for x in matches],
                "previous_duplicate_halves_coalesced": previous_coalesced,
                "current_duplicate_halves_coalesced": current_coalesced}
    shift, overlap = matches[0]
    return {"status": "VERIFIED_DELTA", "new_items": list(reversed(current[:shift])),
            "overlap": overlap, "new_count": shift,
            "previous_duplicate_halves_coalesced": previous_coalesced,
            "current_duplicate_halves_coalesced": current_coalesced}


def _json_reading(raw: dict) -> dict:
    return raw if isinstance(raw, dict) else {"invalid_payload_type": type(raw).__name__}


def _payload_hash(raw: dict) -> str:
    return hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(",", ":"),
                                    default=str).encode("utf-8")).hexdigest()


def _archive_pre_anchor(engine, watch_id: str, raw: dict, parsed: ParsedReading) -> None:
    """Preserve provider history before a uniquely verified Phase 1 handoff."""
    now = datetime.now(timezone.utc)
    incoming_hash = _payload_hash(raw)
    with Session(engine) as session, session.begin():
        existing = session.scalar(select(OrbitReading).where(
            OrbitReading.watch_id == watch_id, OrbitReading.reading_id == parsed.reading_id
        ))
        if existing:
            existing_hash = _payload_hash(existing.raw_payload)
            if existing_hash == incoming_hash:
                return
            conflict_id = f"conf-{incoming_hash[:32]}"
            if not session.scalar(select(OrbitReading.id).where(
                    OrbitReading.watch_id == watch_id, OrbitReading.reading_id == conflict_id)):
                session.add(OrbitReading(watch_id=watch_id, reading_id=conflict_id,
                                         checked_at=parsed.checked_at, received_at=now,
                                         raw_payload=_json_reading(raw),
                                         status="quarantined_identity_conflict",
                                         validation_error="READING_ID_CONTENT_CONFLICT"))
            session.add(ApiError(occurred_at=now, component="orbit_collector",
                                 error_code="READING_ID_CONTENT_CONFLICT",
                                 details={"watch_id": watch_id,
                                          "provider_reading_id": parsed.reading_id,
                                          "archived_conflict_id": conflict_id,
                                          "original_payload_sha256": existing_hash,
                                          "incoming_payload_sha256": incoming_hash}))
            return
        session.add(OrbitReading(watch_id=watch_id, reading_id=parsed.reading_id,
                                 checked_at=parsed.checked_at, received_at=now,
                                 raw_payload=_json_reading(raw), status="quarantined_pre_anchor",
                                 validation_error="NO_PHASE1_SEQUENCE_OVERLAP"))


def _bootstrap_from_history(engine, client: OrbitDataWatchClient, watch_id: str,
                            latest_cursor: str, latest_raw: dict,
                            latest_parsed: ParsedReading, latest_dom: list[dict],
                            *, page_size: int, source_watch: dict | None = None) -> dict:
    """Page Orbit's available history, anchoring only at a proven Phase 1 overlap."""
    history_tail = _historical_tail(engine, 500)
    history_dom = list(reversed([_normal(v) for v in history_tail]))
    scan_after = "0"
    page_count = 0
    new_rounds = 0
    anchored = False
    pre_anchor_snapshots = 0
    while int(scan_after) < int(latest_cursor):
        raw_page = client.history(watch_id, scan_after, page_size)
        try:
            page = validate_history_page(raw_page, watch_id, scan_after)
        except OrbitContractError as exc:
            _archive_invalid(engine, watch_id, _json_reading(raw_page), "", exc.code)
            raise
        page_count += 1
        if page_count > 1000:
            raise OrbitContractError("INITIAL_HISTORY_PAGE_LIMIT")
        reached_target = False
        for raw in page["readings"]:
            try:
                parsed = parse_reading(raw)
            except OrbitContractError as exc:
                rid = str(raw.get("id", "")) if isinstance(raw, dict) else ""
                _archive_invalid(engine, watch_id, _json_reading(raw), rid, exc.code)
                raise
            if int(parsed.reading_id) > int(latest_cursor):
                reached_target = True
                break
            dom = _visible_dom(parsed)
            if parsed.truncated:
                _persist_reading(engine, watch_id, raw, parsed, dom,
                                 previous_snapshot=history_dom, initial_alignment=not anchored)
                raise OrbitContractError("TRUNCATED_READING_BLOCKS_INITIAL_BACKFILL")
            if not anchored:
                delta = _match_delta(history_dom, dom)
                if delta["status"] not in ("VERIFIED_NO_CHANGE", "VERIFIED_DELTA"):
                    # Preserve the snapshot but do not create canonical rounds until
                    # a sequence uniquely joins the frozen research history.
                    if parsed.reading_id != latest_cursor:
                        _archive_pre_anchor(engine, watch_id, raw, parsed)
                        pre_anchor_snapshots += 1
                    continue
                result = _persist_reading(engine, watch_id, raw, parsed, dom,
                                          previous_snapshot=history_dom, initial_alignment=True)
                if result["status"] not in ("VERIFIED_NO_CHANGE", "VERIFIED_DELTA"):
                    raise OrbitContractError("ROUND_OVERLAP_UNVERIFIED")
                anchored = True
                new_rounds += result.get("new_rounds", 0)
                continue

            state = _load_state(engine)
            if state is None:
                raise RuntimeError("Orbit history anchor was not durably stored")
            if int(parsed.reading_id) <= int(state.cursor):
                continue
            result = _persist_reading(engine, watch_id, raw, parsed, dom,
                                      previous_snapshot=state.last_snapshot)
            if result["status"] not in ("VERIFIED_NO_CHANGE", "VERIFIED_DELTA"):
                raise OrbitContractError("ROUND_OVERLAP_UNVERIFIED")
            new_rounds += result.get("new_rounds", 0)

        scan_after = page["nextCursor"]
        if reached_target or int(scan_after) >= int(latest_cursor):
            break
        if not page["hasMore"]:
            break

    state = _load_state(engine)
    if not anchored:
        # History retention may begin after the Phase 1 endpoint. Use the latest
        # reading to establish only a durable, explicitly unverified boundary.
        result = _persist_reading(engine, watch_id, latest_raw, latest_parsed, latest_dom,
                                  previous_snapshot=history_dom, initial_alignment=True)
        if result["status"] == "quarantined_identity_conflict":
            raise OrbitContractError("READING_ID_CONTENT_CONFLICT")
        source_state, source_details = _watch_source_health(source_watch or {})
        _save_health(engine, source_state, {"status": "CROSS_SOURCE_OVERLAP_UNVERIFIED",
                                            "watch_id": watch_id, "cursor": latest_cursor,
                                            "latest_context_rounds": len(latest_dom),
                                            "source_visible_items": sum(
                                                item.visible for item in latest_parsed.items),
                                            "context_at_least_50": len(latest_dom) >= 50,
                                            "continuity_status": "cross_source_overlap_unverified",
                                            **source_details})
        return {**result, "status": "cross_source_overlap_unverified",
                "new_rounds": 0, "pages": page_count,
                "pre_anchor_snapshots": pre_anchor_snapshots,
                "latest_context_rounds": len(latest_dom)}

    state = _load_state(engine)
    if state is None:
        raise RuntimeError("Orbit watch state missing after history bootstrap")
    if int(state.cursor) > int(latest_cursor):
        raise OrbitContractError("LATEST_CURSOR_REGRESSION")
    if int(state.cursor) < int(latest_cursor):
        # A history retention boundary or a concurrent latest update can leave a
        # short tail outside the page result. Recover it only by unique overlap.
        result = _persist_reading(engine, watch_id, latest_raw, latest_parsed, latest_dom,
                                  previous_snapshot=state.last_snapshot)
        if result["status"] not in ("VERIFIED_NO_CHANGE", "VERIFIED_DELTA"):
            raise OrbitContractError("ROUND_OVERLAP_UNVERIFIED")
        new_rounds += result.get("new_rounds", 0)
    state = _load_state(engine)
    if state is None or state.cursor != latest_cursor:
        raise OrbitContractError("INITIAL_HISTORY_DID_NOT_REACH_LATEST")
    source_state, source_details = _watch_source_health(source_watch or {})
    _save_health(engine, source_state, {"status": "INITIAL_HISTORY_BACKFILLED",
                                     "watch_id": watch_id, "cursor": latest_cursor,
                                     "pages": page_count, "new_rounds": new_rounds,
                                     "anchor": "phase1_sequence_overlap_verified",
                                     "latest_context_rounds": len(latest_dom),
                                     "source_visible_items": sum(
                                         item.visible for item in latest_parsed.items),
                                     "context_at_least_50": len(latest_dom) >= 50,
                                     "identical_sequence_halves_coalesced": any(
                                         len(item.get("source_item_indices", [])) > 1
                                         for item in latest_dom),
                                     **source_details})
    return {"status": "initial_history_backfilled", "new_rounds": new_rounds,
            "pages": page_count, "cursor": latest_cursor,
            "anchor": "phase1_sequence_overlap_verified",
            "latest_context_rounds": len(latest_dom)}


def _save_error(engine, component: str, error_code: str, *, status: int | None = None,
                details: dict | None = None) -> None:
    now = datetime.now(timezone.utc)
    with Session(engine) as session, session.begin():
        safe_details = dict(details or {})
        if status is not None:
            safe_details["http_status"] = status
        session.add(ApiError(occurred_at=now, component=component, error_code=error_code,
                             details=safe_details))
        session.merge(SystemHealth(component=component, state="ERROR", checked_at=now,
                                   details={"error_code": error_code, **safe_details}))


def _save_health(engine, state: str, details: dict) -> None:
    with Session(engine) as session, session.begin():
        session.merge(SystemHealth(component="orbit_collector", state=state,
                                   checked_at=datetime.now(timezone.utc), details=details))


def _watch_source_health(watch: dict) -> tuple[str, dict]:
    """Distinguish the Orbit API response from its underlying browser collector."""
    raw_status = watch.get("collectorStatus")
    if not isinstance(raw_status, str) or not raw_status.strip():
        return "HEALTHY", {"upstream_collector_status": "UNKNOWN"}
    status = raw_status.strip().lower()
    if status in {"running", "connected", "healthy"}:
        return "HEALTHY", {"upstream_collector_status": status}
    error = watch.get("lastError")
    message = error.lower() if isinstance(error, str) else ""
    code = ("CHROME_CONNECTION_OFFLINE" if "chrome" in message and "offline" in message
            else "UPSTREAM_COLLECTOR_UNAVAILABLE")
    return "DEGRADED", {"upstream_collector_status": status,
                        "upstream_error_code": code}


def _historical_tail(engine, limit: int = 500) -> list[str]:
    with Session(engine) as session:
        values = session.scalars(select(CanonicalRound.multiplier_raw)
                                 .where(CanonicalRound.analysis_position > 0)
                                 .order_by(CanonicalRound.analysis_position.desc()).limit(limit)).all()
    return list(reversed(values))


def _persist_reading(engine, watch_id: str, raw: dict, parsed: ParsedReading,
                     current_dom: list[dict], *, previous_snapshot: list[dict] | None,
                     initial_alignment: bool = False) -> dict:
    now = datetime.now(timezone.utc)
    state_result = {"status": "quarantined", "new_rounds": 0}
    with Session(engine, expire_on_commit=False) as session:
        with session.begin():
            if engine.dialect.name == "postgresql":
                session.connection().exec_driver_sql(
                    "SELECT pg_advisory_xact_lock(hashtext('advanced-crash-behaviour-round-writer'))")
            existing = session.scalar(select(OrbitReading).where(
                OrbitReading.watch_id == watch_id, OrbitReading.reading_id == parsed.reading_id
            ))
            reprocess_existing = False
            if existing:
                incoming_hash = _payload_hash(raw)
                existing_hash = _payload_hash(existing.raw_payload)
                if incoming_hash != existing_hash:
                    conflict_id = f"conf-{incoming_hash[:32]}"
                    if not session.scalar(select(OrbitReading.id).where(
                            OrbitReading.watch_id == watch_id, OrbitReading.reading_id == conflict_id)):
                        session.add(OrbitReading(watch_id=watch_id, reading_id=conflict_id,
                                                 checked_at=parsed.checked_at, received_at=now,
                                                 raw_payload=_json_reading(raw),
                                                 status="quarantined_identity_conflict",
                                                 validation_error="READING_ID_CONTENT_CONFLICT"))
                    session.add(ApiError(occurred_at=now, component="orbit_collector",
                                         error_code="READING_ID_CONTENT_CONFLICT",
                                         details={"watch_id": watch_id,
                                                  "provider_reading_id": parsed.reading_id,
                                                  "archived_conflict_id": conflict_id,
                                                  "original_payload_sha256": existing_hash,
                                                  "incoming_payload_sha256": incoming_hash}))
                    return {"status": "quarantined_identity_conflict", "new_rounds": 0}
                if existing.status == "quarantined_ambiguous_delta":
                    # A previously held reading can be replayed after a safe
                    # normalization improvement. Its raw payload hash must still match.
                    reprocess_existing = True
                else:
                    return {"status": existing.status, "new_rounds": 0, "duplicate_reading": True}
            state = session.get(OrbitWatchState, 1)
            if state and state.watch_id != watch_id:
                raise OrbitContractError("WATCH_ID_CHANGED_REQUIRES_RECOVERY")
            if parsed.truncated:
                if reprocess_existing:
                    existing.status = "quarantined_truncated"
                    existing.validation_error = "ITEM_COUNT_EXCEEDS_RETURNED_ITEMS"
                else:
                    session.add(OrbitReading(watch_id=watch_id, reading_id=parsed.reading_id,
                                             checked_at=parsed.checked_at, received_at=now,
                                             raw_payload=_json_reading(raw), status="quarantined_truncated",
                                             validation_error="ITEM_COUNT_EXCEEDS_RETURNED_ITEMS"))
                session.add(ApiError(occurred_at=now, component="orbit_collector",
                                     error_code="TRUNCATED_ORBIT_SNAPSHOT",
                                     details={"watch_id": watch_id, "reading_id": parsed.reading_id,
                                              "returned_count": parsed.returned_count,
                                              "match_count": parsed.match_count}))
                session.merge(SystemHealth(component="orbit_collector", state="DEGRADED",
                                           checked_at=now, details={"status": "quarantined_truncated",
                                                                    "reading_id": parsed.reading_id,
                                                                    "returned_count": parsed.returned_count,
                                                                    "match_count": parsed.match_count}))
                return {"status": "quarantined_truncated", "new_rounds": 0}

            if initial_alignment:
                previous = previous_snapshot or []
            else:
                previous = state.last_snapshot if state else []
            delta = _match_delta(previous, current_dom)
            if delta["status"] == "CONTINUITY_UNVERIFIED":
                # Archive every response and preserve a boundary. Once a baseline
                # snapshot exists, later within-watch deltas may be observed, but
                # none are silently merged into the historic sequence here.
                status = "quarantined_continuity" if initial_alignment else "quarantined_ambiguous_delta"
                if reprocess_existing:
                    existing.status = status
                    existing.validation_error = "NO_UNIQUE_OVERLAP"
                else:
                    session.add(OrbitReading(watch_id=watch_id, reading_id=parsed.reading_id,
                                             checked_at=parsed.checked_at, received_at=now,
                                             raw_payload=_json_reading(raw), status=status,
                                             validation_error="NO_UNIQUE_OVERLAP"))
                if not reprocess_existing:
                    session.add(ApiError(occurred_at=now, component="orbit_collector",
                                         error_code="ROUND_OVERLAP_UNVERIFIED",
                                         details={"watch_id": watch_id, "reading_id": parsed.reading_id,
                                                  "initial_alignment": initial_alignment,
                                                  "candidate_shifts": delta.get("candidate_shifts", [])}))
                if initial_alignment:
                    max_position = session.scalar(select(func.max(CanonicalRound.analysis_position)) ) or 0
                    session.add(OrbitWatchState(singleton_id=1, watch_id=watch_id,
                                                cursor=parsed.reading_id,
                                                last_snapshot=current_dom,
                                                source_position=max_position,
                                                continuity_status="cross_source_overlap_unverified",
                                                updated_at=now))
                elif state:
                    # Leave cursor and snapshot untouched. History is replayed until
                    # this exact reading can be aligned or an operator investigates.
                    session.merge(SystemHealth(component="orbit_collector", state="DEGRADED",
                                               checked_at=now, details={"status": status,
                                                                        "reading_id": parsed.reading_id,
                                                                        "cursor_held": state.cursor}))
                return {"status": status, "new_rounds": 0,
                        "candidate_shifts": delta.get("candidate_shifts", [])}

            if reprocess_existing:
                reading = existing
                reading.status = delta["status"]
                reading.validation_error = None
            else:
                reading = OrbitReading(watch_id=watch_id, reading_id=parsed.reading_id,
                                       checked_at=parsed.checked_at, received_at=now,
                                       raw_payload=_json_reading(raw), status=delta["status"],
                                       validation_error=None)
                session.add(reading)
            session.flush()
            max_position = session.scalar(select(func.max(CanonicalRound.analysis_position)) ) or 0
            previous_status = state.continuity_status if state else "cross_source_overlap_verified"
            cross_source_unknown = previous_status != "cross_source_overlap_verified"
            inserted = []
            parsed_by_index = {p.item_index: p for p in parsed.items}
            for item in delta["new_items"]:
                try:
                    parsed_item = parsed_by_index[item["item_index"]]
                    outcome = classify_multiplier(parsed_item.multiplier_raw)
                except ValueError as exc:
                    raise OrbitContractError("INVALID_MULTIPLIER", retryable=False) from exc
                except KeyError as exc:
                    raise OrbitContractError("INVALID_READING_ITEM", retryable=False) from exc
                max_position += 1
                source_identity_raw = f"{watch_id}:{parsed.reading_id}:{item['item_index']}"
                digest = hashlib.sha256(source_identity_raw.encode()).hexdigest()[:32]
                identity_key = f"orbit:{digest}"
                order_quality = ("verified_orbit_delta_cross_source_overlap_unknown"
                                 if cross_source_unknown else "verified_orbit_delta_aligned_to_phase1")
                round_row = CanonicalRound(
                    identity_key=identity_key, round_id=None, source_round_index=None,
                    source_sequence=None, analysis_position=max_position,
                    multiplier_raw=parsed_item.multiplier_raw, multiplier_value=parsed_item.multiplier,
                    outcome=outcome, event_timestamp=None, ingestion_timestamp=parsed.checked_at,
                    source_name="orbit_data_watch", timestamp_quality="per_round_event_time_unavailable",
                    order_quality=order_quality, import_snapshot_id=None, inserted_at=now)
                session.add(round_row)
                session.flush()
                source_indices = item.get("source_item_indices", [item["item_index"]])
                for source_index in source_indices:
                    source_identity = f"{watch_id}:{parsed.reading_id}:{source_index}"
                    if source_index == item["item_index"]:
                        source_identity_key = identity_key
                    else:
                        source_identity_key = f"orbit:{hashlib.sha256(source_identity.encode()).hexdigest()[:32]}"
                    session.add(OrbitRoundMapping(identity_key=identity_key, orbit_reading_id=reading.id,
                                                  item_index=source_index,
                                                  source_identity=source_identity_key,
                                                  cross_source_overlap="unknown" if cross_source_unknown else "verified"))
                inserted.append(identity_key)
            if state is None:
                continuity = ("cross_source_overlap_unverified" if initial_alignment
                              and delta["status"] != "VERIFIED_NO_CHANGE" and not delta["new_items"]
                              else "cross_source_overlap_verified")
                if initial_alignment and delta["status"] == "VERIFIED_NO_CHANGE":
                    continuity = "cross_source_overlap_verified"
                elif initial_alignment and delta["new_items"]:
                    continuity = "cross_source_overlap_verified"
            else:
                continuity = state.continuity_status
            if initial_alignment and delta["status"] not in ("VERIFIED_NO_CHANGE", "VERIFIED_DELTA"):
                continuity = "cross_source_overlap_unverified"
            if state is None:
                state = OrbitWatchState(singleton_id=1, watch_id=watch_id,
                                        cursor=parsed.reading_id, last_snapshot=current_dom,
                                        source_position=max_position, continuity_status=continuity,
                                        updated_at=now)
                session.add(state)
            else:
                state.cursor = parsed.reading_id
                state.last_snapshot = current_dom
                state.source_position = max_position
                state.updated_at = now
            session.add(CollectorActivity(occurred_at=now, event_type="orbit_reading_archived",
                                          details={"watch_id": watch_id, "reading_id": parsed.reading_id,
                                                   "delta_status": delta["status"],
                                                   "new_rounds": len(inserted),
                                                   "visible_snapshot_items": len(current_dom),
                                                   "source_visible_snapshot_items": sum(
                                                       item.visible for item in parsed.items),
                                                   "identical_sequence_halves_coalesced": delta.get(
                                                       "current_duplicate_halves_coalesced", False),
                                                   "context_at_least_50": len(current_dom) >= 50,
                                                   "cross_source_overlap": continuity}))
            session.merge(SystemHealth(component="orbit_collector", state="HEALTHY",
                                       checked_at=now,
                                       details={"watch_id": watch_id, "cursor": parsed.reading_id,
                                                "last_checked_at": parsed.checked_at.isoformat(),
                                                "ingested_at": now.isoformat(),
                                                "latest_context_rounds": len(current_dom),
                                                "source_visible_items": sum(
                                                    item.visible for item in parsed.items),
                                                "identical_sequence_halves_coalesced": delta.get(
                                                    "current_duplicate_halves_coalesced", False),
                                                "context_at_least_50": len(current_dom) >= 50,
                                                "continuity_status": continuity,
                                                "new_rounds": len(inserted)}))
            state_result = {"status": delta["status"], "new_rounds": len(inserted),
                            "identities": inserted, "overlap": delta["overlap"],
                            "continuity_status": continuity}
    return state_result


def _archive_invalid(engine, watch_id: str, raw: dict, reading_id: str, code: str) -> None:
    now = datetime.now(timezone.utc)
    raw_payload = _json_reading(raw)
    canonical = json.dumps(raw_payload, sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    archive_id = f"invalid-{digest}"
    with Session(engine) as session, session.begin():
        if session.scalar(select(OrbitReading.id).where(OrbitReading.watch_id == watch_id,
                                                        OrbitReading.reading_id == archive_id)):
            return
        session.add(OrbitReading(watch_id=watch_id, reading_id=archive_id,
                                 checked_at=None, received_at=now, raw_payload=raw_payload,
                                 status="quarantined_invalid", validation_error=code))
        session.add(ApiError(occurred_at=now, component="orbit_collector",
                             error_code=code, details={"watch_id": watch_id,
                                                       "provider_reading_id": reading_id or None,
                                                       "archive_id": archive_id}))


def _archive_latest_history_mismatch(engine, watch_id: str, history_raw: dict,
                                    latest_raw: dict, history_parsed: ParsedReading,
                                    latest_parsed: ParsedReading, *, cursor_held: str | None,
                                    source_details: dict | None = None) -> dict:
    """Preserve both versions and hold the cursor when Orbit endpoints disagree."""
    now = datetime.now(timezone.utc)
    history_hash = _payload_hash(history_raw)
    latest_hash = _payload_hash(latest_raw)
    history_archive_id = f"conf-{history_hash[:32]}"
    latest_archive_id = f"conf-{latest_hash[:32]}"
    with Session(engine) as session, session.begin():
        if engine.dialect.name == "postgresql":
            session.connection().exec_driver_sql(
                "SELECT pg_advisory_xact_lock(hashtext('advanced-crash-behaviour-round-writer'))")
        existing = session.scalar(select(OrbitReading).where(
            OrbitReading.watch_id == watch_id,
            OrbitReading.reading_id == history_parsed.reading_id))
        if existing is None:
            session.add(OrbitReading(
                watch_id=watch_id, reading_id=history_parsed.reading_id,
                checked_at=history_parsed.checked_at, received_at=now,
                raw_payload=_json_reading(history_raw),
                status="quarantined_latest_history_mismatch",
                validation_error="LATEST_HISTORY_PAYLOAD_MISMATCH"))
        elif _payload_hash(existing.raw_payload) == history_hash:
            if existing.status.startswith("quarantined"):
                existing.status = "quarantined_latest_history_mismatch"
                existing.validation_error = "LATEST_HISTORY_PAYLOAD_MISMATCH"
        elif not session.scalar(select(OrbitReading.id).where(
                OrbitReading.watch_id == watch_id,
                OrbitReading.reading_id == history_archive_id)):
            session.add(OrbitReading(
                watch_id=watch_id, reading_id=history_archive_id,
                checked_at=history_parsed.checked_at, received_at=now,
                raw_payload=_json_reading(history_raw),
                status="quarantined_latest_history_mismatch",
                validation_error="LATEST_HISTORY_PAYLOAD_MISMATCH"))

        has_latest_archive = bool(session.scalar(select(OrbitReading.id).where(
            OrbitReading.watch_id == watch_id,
            OrbitReading.reading_id == latest_archive_id)))
        if not has_latest_archive:
            session.add(OrbitReading(
                watch_id=watch_id, reading_id=latest_archive_id,
                checked_at=latest_parsed.checked_at, received_at=now,
                raw_payload=_json_reading(latest_raw),
                status="quarantined_latest_history_mismatch",
                validation_error="LATEST_HISTORY_PAYLOAD_MISMATCH"))
            session.add(ApiError(
                occurred_at=now, component="orbit_collector",
                error_code="LATEST_HISTORY_PAYLOAD_MISMATCH",
                details={"watch_id": watch_id,
                         "provider_reading_id": latest_parsed.reading_id,
                         "cursor_held": cursor_held,
                         "history_payload_sha256": history_hash,
                         "latest_payload_sha256": latest_hash,
                         "history_archive_id": history_archive_id,
                         "latest_archive_id": latest_archive_id}))
        session.merge(SystemHealth(
            component="orbit_collector", state="DEGRADED", checked_at=now,
            details={"status": "CURSOR_HELD_LATEST_HISTORY_MISMATCH",
                     "error_code": "LATEST_HISTORY_PAYLOAD_MISMATCH",
                     "watch_id": watch_id, "cursor_held": cursor_held,
                     "latest_source_cursor": latest_parsed.reading_id,
                     "latest_source_checked_at": latest_parsed.checked_at.isoformat(),
                     "history_payload_sha256": history_hash,
                     "latest_payload_sha256": latest_hash,
                     **(source_details or {})}))
    return {"status": "quarantined_latest_history_mismatch", "new_rounds": 0,
            "cursor_held": cursor_held,
            "latest_source_cursor": latest_parsed.reading_id,
            "latest_source_checked_at": latest_parsed.checked_at.isoformat()}


def _load_state(engine) -> OrbitWatchState | None:
    with Session(engine) as session:
        return session.get(OrbitWatchState, 1)


def _find_watch(response: dict, watch_id: str) -> dict | None:
    if not isinstance(response, dict) or not isinstance(response.get("watches"), list):
        return None
    return next((item for item in response["watches"] if isinstance(item, dict)
                 and item.get("id") == watch_id), None)


def _prepare_watch_switch_reanchor(engine, saved: OrbitWatchState, target_watch: dict,
                                   old_source_details: dict, new_source_details: dict) -> bool:
    """Archive the previous checkpoint before a verified healthy-source reanchor.

    The old cursor, continuity state and snapshot remain in durable activity
    history. Deleting only the singleton pointer lets the normal history
    bootstrap prove a new Phase 1 overlap or quarantine the unknown boundary.
    """
    now = datetime.now(timezone.utc)
    with Session(engine, expire_on_commit=False) as session, session.begin():
        current = session.get(OrbitWatchState, 1)
        if (current is None or current.watch_id != saved.watch_id
                or current.cursor != saved.cursor):
            return False
        session.add(CollectorActivity(
            occurred_at=now, event_type="orbit_watch_switch_reanchor_started",
            details={
                "previous_watch_id": current.watch_id,
                "previous_cursor": current.cursor,
                "previous_source_position": current.source_position,
                "previous_continuity_status": current.continuity_status,
                "previous_last_snapshot": current.last_snapshot,
                "previous_source_health": old_source_details,
                "target_watch_id": target_watch["id"],
                "target_source_health": new_source_details,
                "intervening_round_count": None,
                "intervening_rounds_status": "UNKNOWN_AND_NOT_INFERRED",
            }))
        session.delete(current)
        session.merge(SystemHealth(
            component="orbit_collector", state="DEGRADED", checked_at=now,
            details={"status": "CROSS_WATCH_REANCHORING",
                     "previous_watch_id": saved.watch_id,
                     "previous_cursor": saved.cursor,
                     "watch_id": target_watch["id"],
                     "intervening_round_count": None,
                     "intervening_rounds_status": "UNKNOWN_AND_NOT_INFERRED",
                     **new_source_details}))
    return True


def _find_stopped_watch(response: dict, preferred_watch_id: str | None) -> dict | None:
    """Return only the configured, correctly targeted watch when it is stopped."""
    if not isinstance(response, dict) or not isinstance(response.get("watches"), list):
        return None
    watches = [item for item in response["watches"] if isinstance(item, dict)]
    if preferred_watch_id:
        matches = [item for item in watches if item.get("id") == preferred_watch_id]
    else:
        matches = [item for item in watches if item.get("selector") == PAYOUT_SELECTOR]
    if (len(matches) != 1 or matches[0].get("selector") != PAYOUT_SELECTOR
            or matches[0].get("status") != "stopped"):
        return None
    return matches[0]


def _reanchor_after_unverified_gap(engine, client, watch: dict, state: OrbitWatchState,
                                   latest_cursor: str, latest_raw: dict,
                                   latest_parsed: ParsedReading, latest_dom: list[dict],
                                   *, page_size: int, source_details: dict) -> dict | None:
    """Resume from a healthy source snapshot after preserving every gap reading.

    No payout in the unverified interval is added to canonical analysis. Its raw
    provider record is retained with an explicit quarantine status. The latest
    snapshot becomes a new within-watch anchor so subsequent unique overlaps can
    be collected without looping forever on the first ambiguous reading.
    """
    if (source_details.get("upstream_collector_status") not in {"running", "connected", "healthy"}
            or len(latest_dom) < 20 or int(latest_cursor) <= int(state.cursor)):
        return None

    target = int(latest_cursor)
    after = state.cursor
    gap_readings: dict[str, tuple[dict, ParsedReading]] = {}
    page_count = 0
    while int(after) < target:
        page = validate_history_page(client.history(watch["id"], after, page_size), watch["id"], after)
        page_count += 1
        if page_count > 1000 or not page["readings"]:
            return None
        for raw in page["readings"]:
            parsed = parse_reading(raw)
            reading_id = int(parsed.reading_id)
            if reading_id > target:
                break
            gap_readings[parsed.reading_id] = (raw, parsed)
        next_cursor = int(page["nextCursor"])
        if next_cursor <= int(after):
            return None
        if next_cursor > target:
            break
        after = str(next_cursor)
        if not page["hasMore"] and int(after) < target:
            return None
    if latest_cursor not in gap_readings:
        # Require the history endpoint to account for the target reading too.
        return None
    history_latest, history_latest_parsed = gap_readings[latest_cursor]
    if (_payload_hash(history_latest) != _payload_hash(latest_raw)
            or history_latest_parsed != latest_parsed
            or _visible_dom(history_latest_parsed) != latest_dom):
        # The history and latest endpoints must agree exactly before we can use
        # the latest snapshot as a new live baseline.
        return None

    now = datetime.now(timezone.utc)
    current_hashes = {reading_id: _payload_hash(raw) for reading_id, (raw, _parsed) in gap_readings.items()}
    with Session(engine, expire_on_commit=False) as session, session.begin():
        if engine.dialect.name == "postgresql":
            session.connection().exec_driver_sql(
                "SELECT pg_advisory_xact_lock(hashtext('advanced-crash-behaviour-round-writer'))")
        persisted_state = session.get(OrbitWatchState, 1)
        if (persisted_state is None or persisted_state.watch_id != watch["id"]
                or persisted_state.cursor != state.cursor):
            return None
        existing_by_reading = {
            row.reading_id: row for row in session.scalars(select(OrbitReading).where(
                OrbitReading.watch_id == watch["id"],
                OrbitReading.reading_id.in_(list(gap_readings)),
            )).all()
        }
        if any(_payload_hash(existing_by_reading[reading_id].raw_payload) != current_hashes[reading_id]
               for reading_id in existing_by_reading):
            return None
        for reading_id, (raw, parsed) in gap_readings.items():
            existing = existing_by_reading.get(reading_id)
            if existing:
                existing.checked_at = parsed.checked_at
                existing.received_at = now
                existing.status = ("REANCHORED_BASELINE" if reading_id == latest_cursor
                                   else "quarantined_reanchor_gap")
                existing.validation_error = (None if reading_id == latest_cursor
                                             else "CONTINUITY_UNVERIFIED_REANCHOR")
            else:
                session.add(OrbitReading(
                    watch_id=watch["id"], reading_id=reading_id, checked_at=parsed.checked_at,
                    received_at=now, raw_payload=_json_reading(raw),
                    status=("REANCHORED_BASELINE" if reading_id == latest_cursor
                            else "quarantined_reanchor_gap"),
                    validation_error=(None if reading_id == latest_cursor
                                     else "CONTINUITY_UNVERIFIED_REANCHOR")))
        persisted_state.cursor = latest_cursor
        persisted_state.last_snapshot = latest_dom
        persisted_state.source_position = session.scalar(
            select(func.max(CanonicalRound.analysis_position))) or 0
        persisted_state.continuity_status = "within_watch_reanchored_after_unverified_gap"
        persisted_state.updated_at = now
        gap_ids = sorted(gap_readings, key=int)
        session.add(CollectorActivity(
            occurred_at=now, event_type="orbit_cursor_reanchored_after_gap",
            details={"watch_id": watch["id"], "from_cursor": state.cursor,
                     "to_cursor": latest_cursor, "quarantined_reading_ids": [
                         reading_id for reading_id in gap_ids if reading_id != latest_cursor],
                     "baseline_reading_id": latest_cursor,
                     "new_context_rounds": len(latest_dom),
                     "canonical_rounds_added": 0,
                     "reason": "No unique payout overlap; raw gap readings preserved and excluded from canonical sequence."}))
        session.add(ApiError(
            occurred_at=now, component="orbit_collector", error_code="ROUND_GAP_QUARANTINED_REANCHOR",
            details={"watch_id": watch["id"], "from_cursor": state.cursor,
                     "to_cursor": latest_cursor, "quarantined_reading_count": max(0, len(gap_ids) - 1),
                     "canonical_rounds_added": 0}))
        session.merge(SystemHealth(
            component="orbit_collector", state="HEALTHY", checked_at=now,
            details={"status": "REANCHORED_AFTER_QUARANTINED_GAP",
                     "watch_id": watch["id"], "cursor": latest_cursor,
                     "quarantined_reading_count": max(0, len(gap_ids) - 1),
                     "latest_context_rounds": len(latest_dom),
                     "latest_source_checked_at": latest_parsed.checked_at.isoformat(),
                     "context_at_least_50": len(latest_dom) >= 50,
                     "continuity_status": persisted_state.continuity_status,
                     **source_details}))
    return {"status": "reanchored_after_unverified_gap", "new_rounds": 0,
            "cursor": latest_cursor, "latest_source_checked_at": latest_parsed.checked_at.isoformat(),
            "quarantined_readings": max(0, len(gap_readings) - 1)}


def collect_once(engine, client: OrbitDataWatchClient, watch: dict, *, page_size: int = 100) -> dict:
    watch_id = watch["id"]
    source_state, source_details = _watch_source_health(watch)
    state = _load_state(engine)
    if state and state.watch_id != watch_id:
        raise OrbitContractError("WATCH_ID_CHANGED_REQUIRES_RECOVERY")
    latest = client.latest(watch_id)
    try:
        cursor, latest_raw = validate_latest_snapshot(latest, watch)
    except OrbitContractError as exc:
        _archive_invalid(engine, watch_id, _json_reading(latest), "", exc.code)
        raise
    if latest_raw is None:
        waiting_state = "WAITING" if source_state == "HEALTHY" else source_state
        _save_health(engine, waiting_state, {"status": "WAITING_FOR_READING",
                                             "watch_id": watch_id, **source_details})
        return {"status": "waiting_for_reading", "new_rounds": 0}
    latest_parsed = parse_reading(latest_raw)
    latest_dom = _visible_dom(latest_parsed)
    if latest_parsed.truncated:
        return _persist_reading(engine, watch_id, latest_raw, latest_parsed, latest_dom,
                                previous_snapshot=state.last_snapshot if state else None,
                                initial_alignment=state is None)
    if state is None:
        return _bootstrap_from_history(engine, client, watch_id, cursor, latest_raw,
                                       latest_parsed, latest_dom, page_size=page_size,
                                       source_watch=watch)
    if int(cursor) < int(state.cursor):
        raise OrbitContractError("LATEST_CURSOR_REGRESSION")
    if int(cursor) == int(state.cursor):
        # A stable cursor does not prove that the provider has kept the payload
        # immutable. Recheck its archived content before reporting a healthy poll.
        result = _persist_reading(engine, watch_id, latest_raw, latest_parsed, latest_dom,
                                  previous_snapshot=state.last_snapshot)
        if result["status"] == "quarantined_identity_conflict":
            _save_health(engine, "DEGRADED", {"status": result["status"],
                                               "watch_id": watch_id, "cursor": cursor,
                                               **source_details})
            return {**result, "cursor": cursor}
        if str(result["status"]).startswith("quarantined"):
            _save_health(engine, "DEGRADED", {"status": result["status"],
                                               "watch_id": watch_id, "cursor": cursor,
                                               "continuity_status": state.continuity_status,
                                               **source_details})
        else:
            no_new_status = ("CONNECTED_NO_NEW_READING" if source_state == "HEALTHY"
                             else "SOURCE_WAITING_FOR_CONNECTION")
            _save_health(engine, source_state, {"status": no_new_status,
                                              "watch_id": watch_id, "cursor": cursor,
                                              "continuity_status": state.continuity_status,
                                              "latest_context_rounds": len(latest_dom),
                                              "source_visible_items": sum(
                                                  item.visible for item in latest_parsed.items),
                                              "context_at_least_50": len(latest_dom) >= 50,
                                              "identical_sequence_halves_coalesced": any(
                                                  len(item.get("source_item_indices", [])) > 1
                                                  for item in latest_dom),
                                              "last_checked_at": latest_parsed.checked_at.isoformat(),
                                              **source_details})
        return {"status": "connected_no_new_reading", "new_rounds": 0,
                "duplicate_reading": result.get("duplicate_reading", False)}

    after = state.cursor
    page_count = 0
    new_rounds = 0
    while int(after) < int(cursor):
        raw_page = client.history(watch_id, after, page_size)
        try:
            page = validate_history_page(raw_page, watch_id, after)
        except OrbitContractError as exc:
            _archive_invalid(engine, watch_id, _json_reading(raw_page), "", exc.code)
            raise
        page_count += 1
        if page_count > 1000:
            raise OrbitContractError("HISTORY_BACKFILL_PAGE_LIMIT")
        readings_beyond_target = False
        for raw in page["readings"]:
            try:
                parsed = parse_reading(raw)
            except OrbitContractError as exc:
                rid = str(raw.get("id", "")) if isinstance(raw, dict) else ""
                _archive_invalid(engine, watch_id, _json_reading(raw), rid, exc.code)
                raise
            if int(parsed.reading_id) > int(cursor):
                # The provider advanced after latest() returned. Do not persist
                # beyond that cursor; replay the same history page next poll.
                readings_beyond_target = True
                break
            dom = _visible_dom(parsed)
            if parsed.truncated:
                _persist_reading(engine, watch_id, raw, parsed, dom,
                                 previous_snapshot=state.last_snapshot)
                raise OrbitContractError("TRUNCATED_READING_BLOCKS_CURSOR")
            result = _persist_reading(engine, watch_id, raw, parsed, dom,
                                      previous_snapshot=state.last_snapshot)
            if result["status"] == "quarantined_ambiguous_delta":
                reanchored = _reanchor_after_unverified_gap(
                    engine, client, watch, state, cursor, latest_raw, latest_parsed, latest_dom,
                    page_size=page_size, source_details=source_details)
                if reanchored is not None:
                    return reanchored
                _save_health(engine, "DEGRADED", {
                    "status": "CURSOR_HELD_UNVERIFIED_OVERLAP",
                    "error_code": "ROUND_OVERLAP_UNVERIFIED",
                    "watch_id": watch_id,
                    "cursor_held": state.cursor,
                    "unverified_reading_id": parsed.reading_id,
                    "latest_source_cursor": cursor,
                    "latest_source_context_rounds": len(latest_dom),
                    "latest_source_checked_at": latest_parsed.checked_at.isoformat(),
                    "last_verified_context_rounds": len(state.last_snapshot),
                    "continuity_status": state.continuity_status,
                    **source_details,
                })
                return {"status": "quarantined_ambiguous_delta", "new_rounds": new_rounds,
                        "cursor_held": state.cursor, "latest_source_cursor": cursor}
            if result["status"] == "quarantined_identity_conflict":
                _save_health(engine, "DEGRADED", {
                    "status": "CURSOR_HELD_READING_IDENTITY_CONFLICT",
                    "error_code": "READING_ID_CONTENT_CONFLICT",
                    "watch_id": watch_id,
                    "cursor_held": state.cursor,
                    "conflicted_reading_id": parsed.reading_id,
                    "latest_source_cursor": cursor,
                    "latest_source_context_rounds": len(latest_dom),
                    "latest_source_checked_at": latest_parsed.checked_at.isoformat(),
                    "last_verified_context_rounds": len(state.last_snapshot),
                    "continuity_status": state.continuity_status,
                    **source_details,
                })
                return {"status": "quarantined_identity_conflict", "new_rounds": new_rounds,
                        "cursor_held": state.cursor, "latest_source_cursor": cursor}
            new_rounds += result.get("new_rounds", 0)
            state = _load_state(engine)
            after = state.cursor if state else after
        if readings_beyond_target:
            break
        if not page["hasMore"]:
            break
        if int(page["nextCursor"]) <= int(after):
            raise OrbitContractError("HISTORY_CURSOR_STALLED")
    if int(after) != int(cursor):
        raise OrbitContractError("HISTORY_DID_NOT_REACH_LATEST_CURSOR")
    sync_status = "SYNCED" if source_state == "HEALTHY" else "SYNCED_SOURCE_WAITING_FOR_CONNECTION"
    _save_health(engine, source_state, {"status": sync_status, "watch_id": watch_id,
                                     "cursor": cursor, "pages": page_count,
                                     "new_rounds": new_rounds,
                                     "latest_context_rounds": len(latest_dom),
                                     "source_visible_items": sum(
                                         item.visible for item in latest_parsed.items),
                                     "context_at_least_50": len(latest_dom) >= 50,
                                     "identical_sequence_halves_coalesced": any(
                                         len(item.get("source_item_indices", [])) > 1
                                         for item in latest_dom),
                                     "continuity_status": state.continuity_status if state else "UNKNOWN",
                                     **source_details})
    return {"status": "synced", "new_rounds": new_rounds, "pages": page_count,
            "cursor": cursor}


def run_collector(database_url: str, base_url: str, api_key: str, *,
                  poll_seconds: float = 2, page_size: int = 100,
                  connect_timeout: float = 5, request_timeout: float = 15) -> None:
    from .db import make_engine, migrate
    engine = make_engine(database_url)
    migrate(engine)
    lock_connection = None
    collector_lock_acquired = False
    client = None
    try:
        if engine.dialect.name == "postgresql":
            lock_connection = engine.connect()
            collector_lock_acquired = bool(lock_connection.exec_driver_sql(
                "SELECT pg_try_advisory_lock(hashtext('advanced-crash-behaviour-orbit-collector'))"
            ).scalar())
            if not collector_lock_acquired:
                raise RuntimeError("an independent Behaviour Orbit collector is already running")
        with Session(engine) as session:
            imported = session.scalar(select(func.count()).select_from(CanonicalRound)) or 0
            if not imported:
                raise RuntimeError("Phase 1 historical import is required before Orbit collection starts")
        client = OrbitDataWatchClient(base_url, api_key, connect_timeout=connect_timeout,
                                      request_timeout=request_timeout)
        log.info("independent Orbit collector starting")
        stopped_watch_signature = None
        while True:
            try:
                saved = _load_state(engine)
                watch_response = client.list_watches()
                try:
                    # A fresh collector follows the first-running Watch API
                    # selection. Keep an established cursor on its saved Watch
                    # unless that source is degraded and a healthy replacement
                    # passes both latest and history validation.
                    first_running = select_watch(watch_response)
                    selected = first_running
                    if saved and saved.watch_id != first_running["id"]:
                        saved_watch = _find_watch(watch_response, saved.watch_id)
                        old_source_state, old_source_details = _watch_source_health(
                            saved_watch or {})
                        if (saved_watch is None or saved_watch.get("status") != "running"):
                            old_source_state = "DEGRADED"
                            old_source_details = {**old_source_details,
                                                  "saved_watch_status": (saved_watch or {}).get("status", "missing")}
                        new_source_state, new_source_details = _watch_source_health(first_running)
                        if old_source_state == "HEALTHY":
                            selected = select_watch(watch_response, saved.watch_id,
                                                    allow_fallback=False)
                        elif new_source_state != "HEALTHY":
                            if saved_watch is None:
                                _save_health(engine, "DEGRADED", {
                                    "status": "SAVED_WATCH_NOT_FOUND",
                                    "watch_id": saved.watch_id, "cursor": saved.cursor,
                                    "first_running_watch_id": first_running["id"],
                                    "upstream_collector_status": new_source_details.get(
                                        "upstream_collector_status"),
                                    "action_required": "Restore the saved Watch or reconnect its Chrome source."})
                                selected = None
                            else:
                                selected = select_watch(watch_response, saved.watch_id,
                                                        allow_fallback=False)
                        else:
                            latest_response = client.latest(first_running["id"])
                            target_cursor, latest_raw = validate_latest_snapshot(
                                latest_response, first_running)
                            parsed = parse_reading(latest_raw) if latest_raw else None
                            context = _visible_dom(parsed) if parsed else []
                            history = validate_history_page(
                                client.history(first_running["id"], "0", page_size),
                                first_running["id"], "0")
                            if (latest_raw is not None
                                    and int(target_cursor) <= int(history["nextCursor"])):
                                history_latest = next((item for item in history["readings"]
                                                       if str(item.get("id", "")) == target_cursor), None)
                                if history_latest is None or history_latest != latest_raw:
                                    raise OrbitContractError("REANCHOR_LATEST_HISTORY_MISMATCH")
                            if (latest_raw is None or parsed is None or len(context) < 50):
                                _save_health(engine, "DEGRADED", {
                                    "status": "WAITING_FOR_REPLACEMENT_WATCH_CONTEXT",
                                    "previous_watch_id": saved.watch_id,
                                    "previous_cursor": saved.cursor,
                                    "watch_id": first_running["id"],
                                    "latest_cursor": target_cursor,
                                    "latest_context_rounds": len(context),
                                    "history_readings": len(history["readings"]),
                                    "source_visible_items": len(parsed.items) if parsed else 0,
                                    **new_source_details})
                                selected = select_watch(watch_response, saved.watch_id,
                                                        allow_fallback=False)
                            elif _prepare_watch_switch_reanchor(
                                    engine, saved, first_running,
                                    old_source_details, new_source_details):
                                log.warning(
                                    "saved Orbit Watch %s is degraded; reanchoring to healthy first running Watch %s; gap remains unknown",
                                    saved.watch_id, first_running["id"])
                                saved = None
                            else:
                                selected = select_watch(watch_response, saved.watch_id,
                                                        allow_fallback=False)
                    elif saved:
                        selected = first_running
                except OrbitContractError as exc:
                    stopped = (_find_stopped_watch(
                        watch_response, saved.watch_id if saved else None)
                               if exc.code == "WATCH_NOT_RUNNING_OR_INVALID" else None)
                    if stopped is None:
                        raise
                    signature = (stopped.get("id"), stopped.get("status"),
                                 stopped.get("collectorStatus"))
                    if signature != stopped_watch_signature:
                        _save_health(engine, "DEGRADED", {
                            "status": "ORBIT_WATCH_STOPPED",
                            "error_code": "ORBIT_WATCH_STOPPED",
                            "watch_id": stopped["id"],
                            "watch_status": stopped.get("status"),
                            "upstream_collector_status": stopped.get("collectorStatus"),
                            "last_verified_cursor": saved.cursor if saved else None,
                            "last_verified_context_rounds": len(saved.last_snapshot) if saved else 0,
                            "action_required": "Reopen the saved Data Watch page or frame in Chrome to resume collection.",
                        })
                        log.warning("Orbit saved watch is stopped; waiting for Chrome page/frame recovery")
                        stopped_watch_signature = signature
                    time.sleep(max(1.0, poll_seconds))
                    continue
                if selected is None:
                    time.sleep(max(1.0, poll_seconds))
                    continue
                stopped_watch_signature = None
                result = collect_once(engine, client, selected, page_size=page_size)
                if result.get("new_rounds"):
                    log.info("Orbit readings delivered new_rounds=%d cursor=%s",
                             result["new_rounds"], result.get("cursor", "current"))
            except OrbitContractError as exc:
                _save_error(engine, "orbit_collector", exc.code, status=exc.status)
                log.error("Orbit collector state=%s status=%s", exc.code, exc.status or "unknown")
            except Exception as exc:
                code = type(exc).__name__
                _save_error(engine, "orbit_collector", "UNEXPECTED_COLLECTOR_ERROR",
                            details={"exception_type": code})
                log.exception("Orbit collector cycle failed")
            time.sleep(max(1.0, poll_seconds))
    finally:
        if client is not None:
            client.close()
        if lock_connection is not None:
            if collector_lock_acquired:
                try:
                    lock_connection.exec_driver_sql(
                        "SELECT pg_advisory_unlock(hashtext('advanced-crash-behaviour-orbit-collector'))")
                except Exception:
                    log.exception("could not release Orbit collector advisory lock")
            lock_connection.close()
        engine.dispose()
