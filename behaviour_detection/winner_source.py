from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from sqlalchemy import bindparam, create_engine, func, select, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from . import settings
from .analysis import classify_multiplier
from .models import (BehaviourApiRound, CanonicalRound, CollectorActivity,
                     SystemHealth, WinnerPredictSyncState)

log = logging.getLogger("behaviour.winner_source")
SOURCE_NAME = "winner_predict_database"
ANCHOR_OVERLAP_ROUNDS = 100
BATCH_SIZE = 500


def make_source_engine() -> Engine:
    if not settings.WINNER_PREDICT_DATABASE_URL:
        raise RuntimeError("Winner Predict DATABASE_URL is not configured")
    url = make_url(settings.WINNER_PREDICT_DATABASE_URL)
    if url.get_backend_name() != "postgresql":
        raise RuntimeError("Winner Predict source must be PostgreSQL")
    if url.drivername in {"postgresql", "postgres"}:
        url = url.set(drivername="postgresql+psycopg")
    return create_engine(
        url,
        pool_pre_ping=True,
        connect_args={"connect_timeout": 5,
                      "options": "-c default_transaction_read_only=on"},
    )


def _iso(value: datetime | None) -> str | None:
    normalized = _as_datetime(value)
    return normalized.isoformat() if normalized is not None else None


def _as_datetime(value: datetime | str | None) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _source_meta(source_engine: Engine) -> tuple[int, int, datetime | None, dict | None, bool]:
    with source_engine.connect() as connection:
        row = connection.execute(text("""SELECT COUNT(*) AS row_count,
            COALESCE(MAX(round_index),0) AS latest_round_index,
            MAX(COALESCE(stored_at,created_at)) AS latest_stored_at
            FROM aviator_rounds""")).mappings().one()
        latest = connection.execute(text("""SELECT round_id,round_index,multiplier,
            platform_timestamp,observed_at,COALESCE(stored_at,created_at) AS stored_at,
            continuity_verified,gap_before FROM aviator_rounds
            ORDER BY round_index DESC LIMIT 1""")).mappings().first()
        suffix = connection.execute(text("""SELECT round_index,continuity_verified,gap_before
            FROM aviator_rounds ORDER BY round_index DESC LIMIT 50""")).mappings().all()
    suffix_clean = len(suffix) == 50 and all(
        bool(item["continuity_verified"]) and not bool(item["gap_before"])
        for item in suffix
    ) and all(int(left["round_index"]) == int(right["round_index"]) + 1
              for left, right in zip(suffix, suffix[1:]))
    return (int(row["row_count"] or 0), int(row["latest_round_index"] or 0),
            _as_datetime(row["latest_stored_at"]), dict(latest) if latest else None, suffix_clean)


def _overlap_anchor(target: Session, source_engine: Engine) -> tuple[dict, int] | None:
    phase1_rows = target.scalars(select(CanonicalRound).where(
        CanonicalRound.import_snapshot_id.is_not(None)
    ).order_by(CanonicalRound.analysis_position.desc()).limit(ANCHOR_OVERLAP_ROUNDS)).all()
    if len(phase1_rows) < ANCHOR_OVERLAP_ROUNDS:
        return None
    phase1_rows.reverse()
    ids = [row.round_id for row in phase1_rows if row.round_id]
    if len(ids) != ANCHOR_OVERLAP_ROUNDS:
        return None
    statement = text("""SELECT round_id,round_index,multiplier,continuity_verified,gap_before
        FROM aviator_rounds WHERE round_id IN :round_ids ORDER BY round_index""").bindparams(
            bindparam("round_ids", expanding=True))
    with source_engine.connect() as connection:
        source_rows = connection.execute(statement, {"round_ids": ids}).mappings().all()
    source_by_id = {str(row["round_id"]): row for row in source_rows}
    pairs = []
    for phase in phase1_rows:
        source = source_by_id.get(str(phase.round_id))
        if source is None:
            return None
        try:
            same_multiplier = Decimal(str(phase.multiplier_raw)) == Decimal(str(source["multiplier"]))
        except (InvalidOperation, TypeError, ValueError):
            return None
        if not same_multiplier:
            return None
        pairs.append((phase, source))
    for (left_phase, left_source), (right_phase, right_source) in zip(pairs, pairs[1:]):
        if (int(right_phase.analysis_position) != int(left_phase.analysis_position) + 1
                or int(right_source["round_index"]) != int(left_source["round_index"]) + 1
                or not bool(right_source["continuity_verified"])
                or bool(right_source["gap_before"])):
            return None
    latest_phase, latest_source = pairs[-1]
    latest_imported_position = target.scalar(select(func.max(CanonicalRound.analysis_position)).where(
        CanonicalRound.import_snapshot_id.is_not(None)))
    if int(latest_phase.analysis_position) != int(latest_imported_position or 0):
        return None
    return {"round_id": str(latest_source["round_id"]),
            "round_index": int(latest_source["round_index"])}, len(pairs)


def _health(session: Session, state: str, details: dict, now: datetime) -> None:
    session.merge(SystemHealth(component=SOURCE_NAME, state=state,
                               checked_at=now, details=details))


def _sync_health_details(*, status: str, row_count: int, latest_index: int,
                         latest_stored_at: datetime | None, state: WinnerPredictSyncState | None,
                         inserted: int = 0, continuity_status: str | None = None,
                         error_code: str | None = None, source_suffix_verified: bool = False,
                         preserved_gap_rounds: int = 0) -> dict:
    return {
        "status": status,
        "source_table": "aviator_rounds",
        "source_round_count": row_count,
        "source_latest_round_index": latest_index,
        "source_latest_stored_at": _iso(latest_stored_at),
        "sync_cursor_round_index": state.last_round_index if state else None,
        "sync_cursor_round_id": state.last_round_id if state else None,
        "synced_round_count": state.synced_round_count if state else 0,
        "new_rounds_this_sync": inserted,
        "anchor_round_index": state.anchor_round_index if state else None,
        "overlap_rounds": state.overlap_rounds if state else 0,
        "source_suffix_verified": source_suffix_verified,
        "preserved_gap_rounds": preserved_gap_rounds,
        "continuity_status": continuity_status or (
            "VERIFIED_BY_100_SHARED_ROUND_IDS" if state else "WAITING_FOR_PHASE1_OVERLAP"),
        "last_sync_at": _iso(state.updated_at) if state else None,
        "error_code": error_code,
    }


def sync_once(target_engine: Engine, source_engine: Engine | None = None) -> dict:
    owns_source = source_engine is None
    source_engine = source_engine or make_source_engine()
    now = datetime.now(timezone.utc)
    try:
        row_count, latest_index, latest_stored_at, latest_row, source_suffix_verified = _source_meta(source_engine)
        with Session(target_engine, expire_on_commit=False) as session, session.begin():
            if target_engine.dialect.name == "postgresql":
                session.connection().exec_driver_sql(
                    "SELECT pg_advisory_xact_lock(hashtext('advanced-crash-behaviour-winner-source-sync'))")
            state = session.get(WinnerPredictSyncState, 1)
            if state is None:
                anchor = _overlap_anchor(session, source_engine)
                if anchor is None:
                    details = _sync_health_details(status="PHASE1_OVERLAP_UNVERIFIED",
                        row_count=row_count, latest_index=latest_index,
                        latest_stored_at=latest_stored_at, state=None,
                        error_code="PHASE1_ROUND_ID_OVERLAP_NOT_VERIFIED",
                        source_suffix_verified=source_suffix_verified)
                    _health(session, "DEGRADED", details, now)
                    return {"status": "degraded", "inserted": 0, **details}
                state = WinnerPredictSyncState(
                    singleton_id=1,
                    last_round_index=anchor[0]["round_index"],
                    last_round_id=anchor[0]["round_id"],
                    anchor_round_index=anchor[0]["round_index"],
                    anchor_round_id=anchor[0]["round_id"],
                    overlap_rounds=anchor[1], synced_round_count=0,
                    source_latest_round_index=latest_index,
                    source_latest_stored_at=latest_stored_at, updated_at=now)
                session.add(state)
                session.add(CollectorActivity(occurred_at=now,
                    event_type="winner_predict_database_overlap_verified",
                    details={"round_index": state.anchor_round_index,
                             "round_id": state.anchor_round_id,
                             "shared_round_ids": state.overlap_rounds}))
                details = _sync_health_details(status="OVERLAP_VERIFIED",
                    row_count=row_count, latest_index=latest_index,
                    latest_stored_at=latest_stored_at, state=state,
                    source_suffix_verified=source_suffix_verified)
                _health(session, "HEALTHY" if source_suffix_verified else "DEGRADED", details, now)
                return {"status": "anchored", "inserted": 0, **details}

            if latest_index < state.last_round_index:
                details = _sync_health_details(status="SOURCE_INDEX_REGRESSION",
                    row_count=row_count, latest_index=latest_index,
                    latest_stored_at=latest_stored_at, state=state,
                    error_code="SOURCE_INDEX_REGRESSION")
                _health(session, "DEGRADED", details, now)
                return {"status": "degraded", "inserted": 0, **details}

            with source_engine.connect() as connection:
                anchor_row = connection.execute(text("""SELECT round_id,multiplier FROM aviator_rounds
                    WHERE round_index=:round_index"""), {"round_index": state.last_round_index}).mappings().first()
            if (anchor_row is None or str(anchor_row["round_id"]) != state.last_round_id):
                details = _sync_health_details(status="SOURCE_CURSOR_IDENTITY_CHANGED",
                    row_count=row_count, latest_index=latest_index,
                    latest_stored_at=latest_stored_at, state=state,
                    error_code="SOURCE_CURSOR_IDENTITY_CHANGED")
                _health(session, "DEGRADED", details, now)
                return {"status": "degraded", "inserted": 0, **details}

            with source_engine.connect() as connection:
                rows = connection.execute(text("""SELECT round_id,round_index,multiplier,
                    platform_timestamp,observed_at,COALESCE(stored_at,created_at) AS stored_at,
                    continuity_verified,gap_before,identity_confidence,round_identity_type,
                    round_index_source,continuity_proof FROM aviator_rounds
                    WHERE round_index>:round_index ORDER BY round_index ASC LIMIT :batch_size"""),
                    {"round_index": state.last_round_index, "batch_size": BATCH_SIZE}).mappings().all()

            inserted = 0
            expected_index = state.last_round_index
            max_position = int(session.scalar(select(func.max(CanonicalRound.analysis_position))) or 0)
            gap_detected = False
            for source_row in rows:
                source_index = int(source_row["round_index"])
                if source_index != expected_index + 1:
                    details = _sync_health_details(status="SOURCE_ROUND_INDEX_GAP",
                        row_count=row_count, latest_index=latest_index,
                        latest_stored_at=latest_stored_at, state=state,
                        inserted=inserted, continuity_status="GAP_HELD_AT_LAST_CONTIGUOUS_INDEX",
                        error_code="SOURCE_ROUND_INDEX_GAP")
                    _health(session, "DEGRADED", details, now)
                    return {"status": "degraded", "inserted": inserted, **details}
                round_id = str(source_row["round_id"])
                multiplier = Decimal(str(source_row["multiplier"]))
                try:
                    outcome = classify_multiplier(multiplier)
                except ValueError:
                    details = _sync_health_details(status="INVALID_SOURCE_MULTIPLIER",
                        row_count=row_count, latest_index=latest_index,
                        latest_stored_at=latest_stored_at, state=state,
                        inserted=inserted, error_code="INVALID_SOURCE_MULTIPLIER")
                    _health(session, "DEGRADED", details, now)
                    return {"status": "degraded", "inserted": inserted, **details}
                source_gap = bool(source_row["gap_before"])
                continuity_verified = bool(source_row["continuity_verified"])
                if source_gap or not continuity_verified:
                    quality = "winner_predict_order_uncertain_source_gap"
                    gap_detected = True
                else:
                    quality = "winner_predict_canonical_order_verified"
                identity_key = f"winner-predict:{round_id}"
                existing = session.get(CanonicalRound, identity_key)
                if existing is not None:
                    if (existing.source_name != SOURCE_NAME
                            or Decimal(str(existing.multiplier_raw)) != multiplier):
                        details = _sync_health_details(status="SOURCE_IDENTITY_CONTENT_CONFLICT",
                            row_count=row_count, latest_index=latest_index,
                            latest_stored_at=latest_stored_at, state=state,
                            inserted=inserted, error_code="SOURCE_IDENTITY_CONTENT_CONFLICT")
                        _health(session, "DEGRADED", details, now)
                        return {"status": "degraded", "inserted": inserted, **details}
                    staged_api_round = session.scalar(select(BehaviourApiRound).where(
                        BehaviourApiRound.round_id == round_id,
                        BehaviourApiRound.round_index == source_index,
                        BehaviourApiRound.status == "STAGED").limit(1))
                    if staged_api_round is not None:
                        staged_api_round.status = "RECONCILED"
                else:
                    event_time = _as_datetime(source_row["platform_timestamp"])
                    observed_time = _as_datetime(source_row["observed_at"])
                    stored_time = _as_datetime(source_row["stored_at"])
                    timestamp_quality = ("platform_event_time" if event_time is not None
                                         else "observation_time_only" if observed_time is not None
                                         else "source_time_unavailable")
                    max_position += 1
                    session.add(CanonicalRound(
                        identity_key=identity_key, round_id=round_id,
                        source_round_index=str(source_index), source_sequence=source_index,
                        analysis_position=max_position, multiplier_raw=str(multiplier),
                        multiplier_value=multiplier, outcome=outcome,
                        event_timestamp=event_time, source_observed_at=observed_time,
                        ingestion_timestamp=stored_time, source_name=SOURCE_NAME,
                        timestamp_quality=timestamp_quality, order_quality=quality,
                        import_snapshot_id=None, inserted_at=now))
                    inserted += 1
                state.last_round_index = source_index
                state.last_round_id = round_id
                expected_index = source_index
            state.source_latest_round_index = latest_index
            state.source_latest_stored_at = latest_stored_at
            state.synced_round_count += inserted
            state.updated_at = now
            preserved_gap_rounds = int(session.scalar(select(func.count()).select_from(CanonicalRound).where(
                CanonicalRound.source_name == SOURCE_NAME,
                CanonicalRound.order_quality == "winner_predict_order_uncertain_source_gap")) or 0)
            status = ("SYNCED_WITH_PRESERVED_GAPS" if inserted and (gap_detected or preserved_gap_rounds)
                      else "SYNCED" if inserted else
                      "WAITING_WITH_PRESERVED_GAPS" if preserved_gap_rounds else "WAITING_FOR_NEW_ROUNDS")
            continuity_status = ("VERIFIED_LATEST_SUFFIX_PRIOR_GAPS_PRESERVED" if preserved_gap_rounds
                                 else "VERIFIED_BY_100_SHARED_ROUND_IDS")
            details = _sync_health_details(status=status,
                row_count=row_count, latest_index=latest_index,
                latest_stored_at=latest_stored_at, state=state,
                inserted=inserted,
                continuity_status=continuity_status,
                source_suffix_verified=source_suffix_verified,
                preserved_gap_rounds=preserved_gap_rounds)
            if latest_row:
                details["source_latest_round_id"] = str(latest_row["round_id"])
                details["source_latest_multiplier"] = str(latest_row["multiplier"])
                details["source_latest_observed_at"] = _iso(latest_row["observed_at"])
            _health(session, "HEALTHY" if source_suffix_verified else "DEGRADED", details, now)
            if inserted:
                session.add(CollectorActivity(occurred_at=now,
                    event_type="winner_predict_rounds_synced",
                    details={"first_round_index": state.last_round_index - inserted + 1,
                             "last_round_index": state.last_round_index,
                             "rounds": inserted, "status": status}))
            return {"status": "synced" if inserted else "waiting", "inserted": inserted, **details}
    except Exception as exc:
        log.exception("Winner Predict database sync failed")
        try:
            with Session(target_engine) as session, session.begin():
                _health(session, "DEGRADED", {
                    "status": "SOURCE_DATABASE_UNAVAILABLE",
                    "source_table": "aviator_rounds",
                    "error_code": type(exc).__name__,
                }, now)
        except Exception:
            log.exception("could not persist Winner Predict source health")
        return {"status": "degraded", "inserted": 0, "error_code": type(exc).__name__}
    finally:
        if owns_source:
            source_engine.dispose()


def insert_verified_api_observation(target_engine: Engine, event: dict) -> str:
    """Durably stage an ordered verified round, including explicit gap boundaries."""
    delivery_mode = str(event.get("delivery_mode") or "").upper()
    if (event.get("event_type") != "round.observed"
            or event.get("durability") != "PENDING"
            or event.get("continuity_status") not in {"VERIFIED", "GAP_BEFORE"}
            or delivery_mode not in {"LIVE", "RECOVERED"}):
        raise ValueError("behaviour_api_observation_not_verified_or_gap_boundary")
    round_id = str(event.get("round_id") or "").strip()
    try:
        round_index = int(event.get("round_index"))
        multiplier = Decimal(str(event.get("multiplier")))
        canonical_sequence = int(event.get("canonical_sequence", round_index))
    except (TypeError, ValueError, InvalidOperation, OverflowError):
        raise ValueError("behaviour_api_observation_identity_invalid") from None
    round_uid = str(event.get("round_uid") or round_id).strip()
    if (not round_id or round_uid != round_id or canonical_sequence != round_index
            or round_index < 1 or not multiplier.is_finite() or multiplier < 1):
        raise ValueError("behaviour_api_observation_identity_invalid")
    stream_id = str(event.get("stream_id") or "").strip()
    if not stream_id:
        raise ValueError("behaviour_api_observation_stream_missing")
    identity_key = f"{stream_id}:{round_id}"
    outcome = classify_multiplier(multiplier)
    now = datetime.now(timezone.utc)
    event_time = _as_datetime(event.get("platform_timestamp"))
    observed_time = _as_datetime(event.get("observed_at"))
    result = "STAGED_GAP"
    with Session(target_engine, expire_on_commit=False) as session, session.begin():
        if target_engine.dialect.name == "postgresql":
            session.connection().exec_driver_sql(
                "SELECT pg_advisory_xact_lock(hashtext('advanced-crash-behaviour-winner-source-sync'))")
        staged = session.get(BehaviourApiRound, identity_key)
        if staged is not None:
            if staged.round_index != round_index or Decimal(staged.multiplier_raw) != multiplier:
                raise ValueError("behaviour_api_observation_identity_conflict")
            result = "DUPLICATE"
        else:
            conflicting_index = session.scalar(select(BehaviourApiRound.identity_key).where(
                BehaviourApiRound.stream_id == stream_id,
                BehaviourApiRound.round_index == round_index).limit(1))
            if conflicting_index and conflicting_index != identity_key:
                raise ValueError("behaviour_api_observation_index_conflict")
            staged = BehaviourApiRound(
                identity_key=identity_key, stream_id=stream_id, round_id=round_id,
                round_index=round_index, multiplier_raw=str(multiplier),
                observed_at=observed_time, platform_timestamp=event_time,
                received_at=now, payload=dict(event), status="STAGED")
            session.add(staged)
            session.flush()
            result = "STAGED"

        existing = session.get(CanonicalRound, f"winner-predict:{round_id}")
        if existing is not None and (existing.source_name != SOURCE_NAME
                or Decimal(str(existing.multiplier_raw)) != multiplier
                or int(existing.source_sequence or 0) != round_index):
            raise ValueError("behaviour_api_observation_identity_conflict")

        # The live consumer checkpoint belongs to this stream's durable API
        # inbox. A background PostgreSQL mirror can already contain a later
        # source index while an earlier local observation is still replaying;
        # using MAX(canonical_rounds.source_sequence) here then permanently
        # head-blocks Behaviour on that missing round.
        previous_index = int(session.scalar(select(func.max(BehaviourApiRound.round_index)).where(
            BehaviourApiRound.stream_id == stream_id,
            BehaviourApiRound.status.in_({"PROMOTED", "RECONCILED"}))) or 0)
        max_source_index = int(session.scalar(select(func.max(CanonicalRound.source_sequence)).where(
            CanonicalRound.source_name == SOURCE_NAME)) or 0)
        max_position = int(session.scalar(select(func.max(CanonicalRound.analysis_position))) or 0)
        pending = session.scalars(select(BehaviourApiRound).where(
            BehaviourApiRound.stream_id == stream_id,
            BehaviourApiRound.status == "STAGED",
            BehaviourApiRound.round_index > previous_index
        ).order_by(BehaviourApiRound.round_index).limit(500)).all()
        expected_index = previous_index
        for item in pending:
            item_payload = item.payload if isinstance(item.payload, dict) else {}
            explicit_gap_boundary = item_payload.get("continuity_status") == "GAP_BEFORE"
            if item.round_index != expected_index + 1 and not explicit_gap_boundary:
                break
            item_multiplier = Decimal(item.multiplier_raw)
            item_outcome = classify_multiplier(item_multiplier)
            canonical = session.get(CanonicalRound, f"winner-predict:{item.round_id}")
            reconciled_existing_canonical = canonical is not None
            if canonical is not None and (canonical.source_name != SOURCE_NAME
                    or int(canonical.source_sequence or 0) != item.round_index
                    or Decimal(str(canonical.multiplier_raw)) != item_multiplier):
                raise ValueError("behaviour_api_observation_identity_conflict")
            # Preserve chronological analysis_position values. If the async
            # source mirror has already moved beyond this sequence, the live
            # reducer uses BehaviourApiRound directly and waits for ordered
            # reporting reconciliation instead of inserting an old row after
            # a newer descriptive snapshot.
            if canonical is None and item.round_index > max_source_index:
                max_position += 1
                canonical = CanonicalRound(
                    identity_key=f"winner-predict:{item.round_id}", round_id=item.round_id,
                    source_round_index=str(item.round_index), source_sequence=item.round_index,
                    analysis_position=max_position, multiplier_raw=item.multiplier_raw,
                    multiplier_value=item_multiplier, outcome=item_outcome,
                    event_timestamp=item.platform_timestamp, source_observed_at=item.observed_at,
                    ingestion_timestamp=item.received_at, source_name=SOURCE_NAME,
                    timestamp_quality=("platform_event_time" if item.platform_timestamp is not None
                                       else "observation_time_only" if item.observed_at is not None
                                       else "source_time_unavailable"),
                    order_quality="winner_predict_canonical_order_verified",
                    import_snapshot_id=None, inserted_at=now)
                session.add(canonical)
                max_source_index = item.round_index
            # Creating a reporting mirror row here is not reconciliation with
            # an independently persisted canonical row. The Behaviour live
            # inbox owns this durable promotion; report whether it matched an
            # already existing mirror separately.
            item.status = ("RECONCILED" if reconciled_existing_canonical
                           else "PROMOTED")
            expected_index = item.round_index
            if item.identity_key == identity_key:
                result = item.status
        if staged.status in {"PROMOTED", "RECONCILED"} and result == "DUPLICATE":
            result = "DUPLICATE"
        elif staged.status == "STAGED":
            result = "STAGED_GAP"
        session.add(CollectorActivity(occurred_at=now,
            event_type="winner_predict_round_api_staged",
            details={"round_id": round_id, "round_index": round_index,
                     "stream_id": stream_id, "status": result,
                     "observed_at": _iso(observed_time)}))
    return result


def run_source_collector(database_url: str, *, poll_seconds: float = 1.0) -> None:
    from .db import make_engine, migrate
    import psycopg

    target = make_engine(database_url)
    source = make_source_engine()

    def catch_up() -> None:
        # The persisted cursor and identity check make overlapping wakeups and
        # reconnect replay idempotent. Bound each pass to avoid monopolizing CPU.
        for _ in range(100):
            result = sync_once(target, source)
            if result.get("status") == "degraded":
                log.warning("Winner Predict source status=%s error=%s",
                            result.get("status"), result.get("error_code"))
                break
            if result.get("inserted"):
                log.info("synced %s Winner Predict rounds through index %s",
                         result["inserted"], result.get("sync_cursor_round_index"))
            if int(result.get("inserted") or 0) < BATCH_SIZE:
                break

    try:
        migrate(target)
        log.info("Winner Predict committed-round subscription starting")
        while True:
            try:
                url = source.url
                with psycopg.connect(host=url.host, port=url.port, dbname=url.database,
                                     user=url.username, password=url.password,
                                     connect_timeout=5, autocommit=True) as connection:
                    connection.execute("LISTEN winner_predict_verified_round")
                    # Subscribe first, then query the durable store. Any round
                    # committed during the query is covered by the cursor or
                    # delivered as a notification, never silently skipped.
                    catch_up()
                    while True:
                        for notice in connection.notifies(timeout=15.0):
                            if notice.channel == "winner_predict_verified_round":
                                catch_up()
                        # Covers missed NOTIFY, a writer without this hint, and
                        # stale health while the source is quiet.
                        catch_up()
            except KeyboardInterrupt:
                raise
            except Exception:
                log.exception("Winner Predict round subscription disconnected; reconnecting")
                try:
                    catch_up()
                except Exception:
                    log.exception("Winner Predict source replay failed after subscription loss")
                time.sleep(max(.5, poll_seconds))
    finally:
        source.dispose()
        target.dispose()
