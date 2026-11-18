from __future__ import annotations

from datetime import datetime, timezone
import uuid
from pathlib import Path

from sqlalchemy import Engine, create_engine, event, inspect, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from .hybrid import HIGH_DOMINANCE_2X_BATCH_SIZE as HIGH_DOMINANCE_BATCH_SIZE
from .high_series import new_state as new_high_series_state
from .models import (Base, BehaviourApiRound, BehaviourStrategySettlement, CanonicalRound,
                     DominanceWindow, HighDominance2xBatch, HighDominanceSeriesState,
                     HighDominanceStateTest, HybridCohort, HybridCohortRound,
                     MotifResearchEvidence, SchemaMigration, WinnerPredictSyncState)


def make_engine(database_url: str) -> Engine:
    if not database_url:
        raise ValueError("BEHAVIOUR_DATABASE_URL is required; do not fall back to Winner Predict")
    url = make_url(database_url)
    kwargs = {"pool_pre_ping": True, "future": True}
    if url.get_backend_name() == "sqlite":
        kwargs["connect_args"] = {"check_same_thread": False}
        if url.database and url.database != ":memory:":
            Path(url.database).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(database_url, **kwargs)
    if url.get_backend_name() == "sqlite":
        @event.listens_for(engine, "connect")
        def configure_sqlite_connection(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.execute("PRAGMA synchronous=NORMAL")
            cursor.close()
        # journal_mode is persistent for a SQLite database. Reapplying this
        # locking pragma on every pooled connection can stall API readers while
        # the analysis worker is writing. Only initialize WAL when needed.
        with engine.connect() as connection:
            journal_mode = connection.exec_driver_sql("PRAGMA journal_mode").scalar()
            if str(journal_mode or "").lower() != "wal":
                connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    return engine


def make_live_journal_engine(database_url: str) -> Engine:
    """Open the shared local ingress journal with durable live-state commits."""
    engine = make_engine(database_url)
    if engine.dialect.name == "sqlite":
        @event.listens_for(engine, "connect")
        def configure_live_journal(connection, _record):
            cursor = connection.cursor()
            cursor.execute("PRAGMA busy_timeout=1500")
            cursor.execute("PRAGMA synchronous=FULL")
            cursor.close()
    return engine


def migrate(engine: Engine) -> None:
    """Transactional, versioned, idempotent migrations for this isolated database."""
    with engine.begin() as connection:
        if engine.dialect.name == "postgresql":
            # API and worker can start together on a fresh database. Serialize
            # schema checks so only one process applies each version at a time.
            connection.exec_driver_sql(
                "SELECT pg_advisory_xact_lock(hashtext('advanced-crash-behaviour-schema'))")
        SchemaMigration.__table__.create(connection, checkfirst=True)
        applied = connection.execute(select(SchemaMigration.version)).scalars().all()
        if 1 not in applied:
            Base.metadata.create_all(connection)
            connection.execute(SchemaMigration.__table__.insert().values(
                version=1, applied_at=datetime.now(timezone.utc)))
            applied.append(1)
        if 2 not in applied:
            MotifResearchEvidence.__table__.create(connection, checkfirst=True)
            connection.execute(SchemaMigration.__table__.insert().values(
                version=2, applied_at=datetime.now(timezone.utc)))
            applied.append(2)
        if 3 not in applied:
            if engine.dialect.name == "postgresql":
                connection.exec_driver_sql(
                    "ALTER TABLE rule_definitions ALTER COLUMN validation_status TYPE VARCHAR(160)")
            connection.execute(SchemaMigration.__table__.insert().values(
                version=3, applied_at=datetime.now(timezone.utc)))
            applied.append(3)
        if 4 not in applied:
            columns = {column["name"] for column in inspect(connection).get_columns("research_imports")}
            if "input_files_sha256" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE research_imports ADD COLUMN input_files_sha256 VARCHAR(64)")
            # Phase 1 emits 1-based reference positions. The initial importer
            # added one a second time; repair only its frozen reference rows.
            connection.exec_driver_sql(
                "UPDATE forecast_reference_blocks "
                "SET first_position = first_position - 1, last_position = last_position - 1 "
                "WHERE source = 'phase1_frozen_disjoint_100' "
                "AND last_position = first_position + 99")
            connection.execute(SchemaMigration.__table__.insert().values(
                version=4, applied_at=datetime.now(timezone.utc)))
            applied.append(4)
        if 5 not in applied:
            columns = {column["name"] for column in inspect(connection).get_columns("cumulative_round_statistics")}
            if "log_multiplier_square_sum" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE cumulative_round_statistics "
                    "ADD COLUMN log_multiplier_square_sum NUMERIC(30,12) NOT NULL DEFAULT 0")
            if "tail_counts" not in columns:
                connection.exec_driver_sql(
                    "ALTER TABLE cumulative_round_statistics ADD COLUMN tail_counts JSON NOT NULL DEFAULT '{}' ")
            connection.execute(SchemaMigration.__table__.insert().values(
                version=5, applied_at=datetime.now(timezone.utc)))
            applied.append(5)
        if 6 not in applied:
            columns = {column["name"] for column in inspect(connection).get_columns("canonical_rounds")}
            if "source_observed_at" not in columns:
                observed_type = "TIMESTAMPTZ" if engine.dialect.name == "postgresql" else "DATETIME"
                connection.exec_driver_sql(
                    f"ALTER TABLE canonical_rounds ADD COLUMN source_observed_at {observed_type}")
            WinnerPredictSyncState.__table__.create(connection, checkfirst=True)
            connection.execute(SchemaMigration.__table__.insert().values(
                version=6, applied_at=datetime.now(timezone.utc)))
            applied.append(6)
        if 7 not in applied:
            HybridCohort.__table__.create(connection, checkfirst=True)
            HybridCohortRound.__table__.create(connection, checkfirst=True)
            DominanceWindow.__table__.create(connection, checkfirst=True)
            # The first live cohort begins now; persisted history is deliberately
            # not backfilled into this prospective local sequence.
            activated = datetime.now(timezone.utc)
            connection.execute(HybridCohort.__table__.insert().prefix_with("OR IGNORE")
                .values(singleton_id=1, cohort_id=f"prospective-{uuid.uuid4().hex}",
                        activated_at=activated, last_cohort_position=0,
                        confirmed_state="INSUFFICIENT_DATA", candidate_state="INSUFFICIENT_DATA",
                        candidate_count=0, confirmed_momentum="INSUFFICIENT_DATA",
                        candidate_momentum="INSUFFICIENT_DATA", momentum_candidate_count=0,
                        updated_at=activated)) if engine.dialect.name == "sqlite" else None
            if engine.dialect.name != "sqlite":
                from sqlalchemy.dialects.postgresql import insert as pg_insert
                connection.execute(pg_insert(HybridCohort).values(
                    singleton_id=1, cohort_id=f"prospective-{uuid.uuid4().hex}",
                    activated_at=activated, last_cohort_position=0,
                    confirmed_state="INSUFFICIENT_DATA", candidate_state="INSUFFICIENT_DATA",
                    candidate_count=0, confirmed_momentum="INSUFFICIENT_DATA",
                    candidate_momentum="INSUFFICIENT_DATA", momentum_candidate_count=0,
                    updated_at=activated).on_conflict_do_nothing(index_elements=["singleton_id"]))
            connection.execute(SchemaMigration.__table__.insert().values(
                version=7, applied_at=activated))
            applied.append(7)
        if 8 not in applied:
            HighDominance2xBatch.__table__.create(connection, checkfirst=True)
            connection.execute(SchemaMigration.__table__.insert().values(
                version=8, applied_at=datetime.now(timezone.utc)))
            applied.append(8)
        if 9 not in applied:
            cohort_columns = {column["name"] for column in inspect(connection).get_columns(
                "hybrid_behaviour_cohorts")}
            if "rolling_horizon_states" not in cohort_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE hybrid_behaviour_cohorts ADD COLUMN rolling_horizon_states JSON")
            batch_columns = {column["name"] for column in inspect(connection).get_columns(
                "high_dominance_2x_batches")}
            if "trigger_horizon" not in batch_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE high_dominance_2x_batches ADD COLUMN trigger_horizon VARCHAR(10)")
            if "phase" not in batch_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE high_dominance_2x_batches "
                    "ADD COLUMN phase VARCHAR(20) NOT NULL DEFAULT 'INITIAL'")
            connection.execute(SchemaMigration.__table__.insert().values(
                version=9, applied_at=datetime.now(timezone.utc)))
            applied.append(9)
        if 10 not in applied:
            # A still-empty initial batch created by v2 has not consumed any
            # future result yet; retain the decision as an anchor only.
            batch_table = HighDominance2xBatch.__table__
            pending_initial = connection.execute(select(
                batch_table.c.batch_id, batch_table.c.decision_position,
                batch_table.c.outcomes,
            ).where(batch_table.c.phase == "INITIAL",
                    batch_table.c.lifecycle_status == "COLLECTING")).mappings().all()
            for batch in pending_initial:
                if not batch["outcomes"]:
                    connection.execute(batch_table.update().where(
                        batch_table.c.batch_id == batch["batch_id"]
                    ).values(start_position=batch["decision_position"] + 1))
            connection.execute(SchemaMigration.__table__.insert().values(
                version=10, applied_at=datetime.now(timezone.utc)))
            applied.append(10)
        if 11 not in applied:
            # Earlier v3 batches could include the decision observation.
            batch_table = HighDominance2xBatch.__table__
            active_initial = connection.execute(select(batch_table).where(
                batch_table.c.phase == "INITIAL",
                batch_table.c.lifecycle_status == "COLLECTING",
            )).mappings().all()
            for batch in active_initial:
                decision_position = int(batch["decision_position"])
                outcomes = list(batch["outcomes"] or [])
                retained = [item for item in outcomes
                            if int(item.get("cohort_position", -1)) != decision_position]
                if retained != outcomes:
                    wins = sum(item.get("result_at_2x") == "WIN" for item in retained)
                    losses = sum(item.get("result_at_2x") == "LOSS" for item in retained)
                    connection.execute(batch_table.update().where(
                        batch_table.c.batch_id == batch["batch_id"]
                    ).values(outcomes=retained, wins=wins, losses=losses))
                connection.execute(batch_table.update().where(
                    batch_table.c.batch_id == batch["batch_id"]
                ).values(start_position=decision_position + 1))
            connection.execute(SchemaMigration.__table__.insert().values(
                version=11, applied_at=datetime.now(timezone.utc)))
            applied.append(11)
        if 12 not in applied:
            # Rebase only the latest live series created by the decision-inclusive
            # policy. Move its first continuation observation into the corrected
            # initial fifth slot, then start continuation after that round.
            batch_table = HighDominance2xBatch.__table__
            active = connection.execute(select(batch_table).where(
                batch_table.c.lifecycle_status == "COLLECTING"
            ).order_by(batch_table.c.opened_at.desc()).limit(1)).mappings().first()
            if active:
                series_rows = connection.execute(select(batch_table).where(
                    batch_table.c.cohort_id == active["cohort_id"],
                    batch_table.c.series_anchor_position == active["series_anchor_position"],
                ).order_by(batch_table.c.batch_number)).mappings().all()
                initial = next((row for row in series_rows if row["phase"] == "INITIAL"), None)
                if initial:
                    decision_position = int(initial["decision_position"])
                    initial_outcomes = list(initial["outcomes"] or [])
                    old_decision = next((item for item in initial_outcomes
                                         if int(item.get("cohort_position", -1)) == decision_position), None)
                    initial_outcomes = [item for item in initial_outcomes
                                        if int(item.get("cohort_position", -1)) != decision_position]
                    active_is_continuation = active["phase"] == "CONTINUATION"
                    continuation_outcomes = list(active["outcomes"] or []) if active_is_continuation else []
                    replacement_position = decision_position + HIGH_DOMINANCE_BATCH_SIZE
                    replacement = next((item for item in continuation_outcomes
                                        if int(item.get("cohort_position", -1)) == replacement_position), None)
                    if old_decision and replacement:
                        initial_outcomes.append(replacement)
                        continuation_outcomes = [item for item in continuation_outcomes
                                                  if int(item.get("cohort_position", -1)) != replacement_position]
                    initial_outcomes.sort(key=lambda item: int(item.get("cohort_position", 0)))
                    initial_wins = sum(item.get("result_at_2x") == "WIN" for item in initial_outcomes)
                    initial_losses = sum(item.get("result_at_2x") == "LOSS" for item in initial_outcomes)
                    initial_end = max((int(item.get("cohort_position", 0))
                                       for item in initial_outcomes), default=None)
                    initial_complete = len(initial_outcomes) >= HIGH_DOMINANCE_BATCH_SIZE
                    connection.execute(batch_table.update().where(
                        batch_table.c.batch_id == initial["batch_id"]
                    ).values(start_position=decision_position + 1,
                             outcomes=initial_outcomes, wins=initial_wins, losses=initial_losses,
                             end_position=initial_end,
                             lifecycle_status="COMPLETED" if initial_complete else "COLLECTING",
                             completed_at=(initial["completed_at"] or datetime.now(timezone.utc)
                                           if initial_complete else None)))

                    if (active_is_continuation and active["batch_id"] != initial["batch_id"]
                            and old_decision):
                        if initial_complete and replacement:
                            new_decision_position = initial_end
                            decision_round = replacement
                            cohort = connection.execute(select(
                                HybridCohort.__table__.c.rolling_horizon_states,
                            ).where(HybridCohort.__table__.c.singleton_id == 1)).mappings().first()
                            current_states = (cohort or {}).get("rolling_horizon_states") or {}
                            trigger_horizon = active["trigger_horizon"] or initial["trigger_horizon"]
                            trigger_state = current_states.get(trigger_horizon)
                            remaining = [item for item in continuation_outcomes
                                         if int(item.get("cohort_position", -1)) > new_decision_position]
                            remaining.sort(key=lambda item: int(item.get("cohort_position", 0)))
                            continuation_wins = sum(item.get("result_at_2x") == "WIN" for item in remaining)
                            continuation_losses = sum(item.get("result_at_2x") == "LOSS" for item in remaining)
                            stopped = trigger_state != "HIGH_DOMINANT"
                            connection.execute(batch_table.update().where(
                                batch_table.c.batch_id == active["batch_id"]
                            ).values(
                                decision_position=new_decision_position,
                                decision_identity_key=decision_round.get("identity_key"),
                                decision_round_id=decision_round.get("round_id"),
                                decision_source_name=decision_round.get("source_name"),
                                decision_source_sequence=decision_round.get("source_sequence"),
                                start_position=new_decision_position + 1,
                                outcomes=remaining, wins=continuation_wins,
                                losses=continuation_losses,
                                end_position=(int(remaining[-1]["cohort_position"]) if remaining else None),
                                state_at_decision=trigger_state or "HIGH_DOMINANT",
                                state_at_end=(trigger_state if not remaining else active["state_at_end"]),
                                lifecycle_status=("STOPPED_LOW_DOMINANT" if trigger_state == "LOW_DOMINANT"
                                                  else "STOPPED_STATE_EXIT" if stopped else "COLLECTING"),
                                completed_at=datetime.now(timezone.utc) if stopped else None,
                                coverage_issue=None,
                                updated_at=datetime.now(timezone.utc)))
                        else:
                            connection.execute(batch_table.update().where(
                                batch_table.c.batch_id == active["batch_id"]
                            ).values(outcomes=[], wins=0, losses=0, end_position=None,
                                     lifecycle_status="REBASED_WAITING_FOR_INITIAL",
                                     coverage_issue="INITIAL_BATCH_REANCHORED",
                                     completed_at=datetime.now(timezone.utc),
                                     updated_at=datetime.now(timezone.utc)))
            connection.execute(SchemaMigration.__table__.insert().values(
                version=12, applied_at=datetime.now(timezone.utc)))
            applied.append(12)
        if 13 not in applied:
            HighDominanceSeriesState.__table__.create(connection, checkfirst=True)
            connection.execute(SchemaMigration.__table__.insert().values(
                version=13, applied_at=datetime.now(timezone.utc)))
            applied.append(13)
        if 14 not in applied:
            state_table = HighDominanceSeriesState.__table__
            state_table.create(connection, checkfirst=True)
            batch_table = HighDominance2xBatch.__table__
            cohort_table = HybridCohort.__table__
            cohorts = connection.execute(select(
                cohort_table.c.cohort_id, cohort_table.c.last_cohort_position,
                cohort_table.c.last_identity_key,
            )).mappings().all()
            for cohort in cohorts:
                if connection.execute(select(state_table.c.cohort_id).where(
                        state_table.c.cohort_id == cohort["cohort_id"])).first():
                    continue
                batches = connection.execute(select(batch_table).where(
                    batch_table.c.cohort_id == cohort["cohort_id"]
                ).order_by(batch_table.c.series_anchor_position, batch_table.c.batch_number)).mappings().all()
                machine = new_high_series_state()
                machine["state"] = "ARMED_FOR_HIGH"
                machine["last_processed_position"] = int(cohort["last_cohort_position"] or 0)
                machine["last_processed_identity"] = cohort["last_identity_key"]
                if batches:
                    anchor = max(int(item["series_anchor_position"]) for item in batches)
                    current = [item for item in batches if int(item["series_anchor_position"]) == anchor]
                    initial = next((item for item in current if item["phase"] == "INITIAL"), None)
                    continuation = [item for item in current if item["phase"] == "CONTINUATION"]
                    latest = continuation[-1] if continuation else None

                    def convert_outcomes(values):
                        converted = []
                        for outcome in values or []:
                            result = outcome.get("result_at_2x") or outcome.get("result")
                            converted.append({"identity_key": outcome.get("identity_key"),
                                "round_id": outcome.get("round_id"),
                                "position": int(outcome.get("cohort_position") or outcome.get("position") or 0),
                                "multiplier": str(outcome.get("multiplier") or "0"),
                                "outcome": outcome.get("classification") or outcome.get("outcome"),
                                "result": result,
                                "timestamp": outcome.get("observed_at")})
                        return converted

                    def performance(values):
                        rounds = convert_outcomes(values)
                        wins = sum(item["result"] == "WIN" for item in rounds)
                        losses = sum(item["result"] == "LOSS" for item in rounds)
                        return {"rounds": rounds, "wins": wins, "losses": losses}

                    if initial:
                        part1 = performance(initial["outcomes"])
                        decision_at = initial["opened_at"]
                        machine["series"] = {"series_id": f"legacy:{cohort['cohort_id']}:{anchor}",
                            "trigger_round_identity": initial["decision_identity_key"],
                            "trigger_round_id": initial["decision_round_id"],
                            "trigger_position": int(initial["decision_position"] or anchor),
                            "trigger_timestamp": decision_at.isoformat() if decision_at else None,
                            "trigger_horizon": int(initial["trigger_horizon"] or 5),
                            "part1": part1}
                        if initial["lifecycle_status"] == "COLLECTING":
                            machine["state"] = "PART1_COLLECTING"
                        elif latest is None and initial["state_at_end"] == "HIGH_DOMINANT":
                            machine["state"] = "PART2_ACTIVE"
                            machine["series"]["part2"] = {"batch_number": 1, "batches": [],
                                "current_batch": {"rounds": [], "wins": 0, "losses": 0},
                                "pause_count": 0, "pause_reason": None,
                                "pause_started_round_identity": None, "pause_started_at": None,
                                "pause_intervals": [], "total_pause_rounds": 0, "resume_count": 0}
                        elif latest is not None:
                            completed_parts = [performance(item["outcomes"]) for item in continuation[:-1]]
                            if latest["lifecycle_status"] == "COLLECTING":
                                machine["state"] = "PART2_ACTIVE"
                                current_batch = performance(latest["outcomes"])
                            elif latest["lifecycle_status"] in {"STOPPED_LOW_DOMINANT", "STOPPED_STATE_EXIT"}:
                                machine["state"] = ("PART2_PAUSED_LOW_DOMINANT"
                                    if latest["state_at_end"] == "LOW_DOMINANT"
                                    else "PART2_PAUSED_NON_HIGH")
                                current_batch = performance(latest["outcomes"])
                            elif latest["lifecycle_status"] == "COMPLETED":
                                completed_parts.append(performance(latest["outcomes"]))
                                current_batch = {"rounds": [], "wins": 0, "losses": 0}
                                machine["state"] = ("PART2_ACTIVE" if latest["state_at_end"] == "HIGH_DOMINANT"
                                    else "PART2_PAUSED_LOW_DOMINANT" if latest["state_at_end"] == "LOW_DOMINANT"
                                    else "PART2_PAUSED_NON_HIGH")
                            else:
                                machine["state"] = "DATA_STALE"
                                current_batch = performance(latest["outcomes"])
                            pause_start = latest["completed_at"]
                            pause_identity = ((current_batch["rounds"][-1].get("identity_key")
                                               if current_batch["rounds"] else None)
                                              or latest["decision_identity_key"])
                            machine["series"]["part2"] = {"batch_number": int(latest["batch_number"])
                                    + int(latest["lifecycle_status"] == "COMPLETED"),
                                "batches": completed_parts, "current_batch": current_batch,
                                "pause_count": 0, "pause_reason": latest["state_at_end"]
                                    if machine["state"].startswith("PART2_PAUSED") else None,
                                "pause_started_round_identity": pause_identity,
                                "pause_started_at": pause_start.isoformat()
                                    if machine["state"].startswith("PART2_PAUSED") and pause_start else None,
                                "pause_intervals": [], "total_pause_rounds": 0, "resume_count": 0}
                        else:
                            machine["state"] = "COOLDOWN"
                            machine["series"]["stop_reason"] = "STOPPED_PART1_NO_HIGH"
                            machine["series"]["terminal_state"] = "STOPPED_PART1_NO_HIGH"
                            machine["cooldown_started_at"] = (initial["completed_at"].isoformat()
                                if initial["completed_at"] else None)
                            machine["cooldown_progress"] = 0
                    else:
                        machine["state"] = "IDLE"
                machine["events"].append({"type": "EXISTING_SERIES_RECOVERED",
                    "round_identity": machine.get("last_processed_identity"),
                    "round_id": None, "position": machine.get("last_processed_position"),
                    "timestamp": datetime.now(timezone.utc).isoformat()})
                connection.execute(state_table.insert().values(cohort_id=cohort["cohort_id"],
                    payload=machine, updated_at=datetime.now(timezone.utc)))
            connection.execute(SchemaMigration.__table__.insert().values(
                version=14, applied_at=datetime.now(timezone.utc)))
            applied.append(14)
        if 13 not in applied:
            HighDominanceStateTest.__table__.create(connection, checkfirst=True)
            connection.execute(SchemaMigration.__table__.insert().values(
                version=13, applied_at=datetime.now(timezone.utc)))
            applied.append(13)
        if 15 not in applied:
            BehaviourApiRound.__table__.create(connection, checkfirst=True)
            connection.execute(SchemaMigration.__table__.insert().values(
                version=15, applied_at=datetime.now(timezone.utc)))
            applied.append(15)
        if 16 not in applied:
            # The live API inbox checks the reporting mirror for an existing
            # source position on every round. Keep both sequence lookups
            # bounded/indexed as the descriptive canonical table grows.
            # Use the table-bound metadata Index objects. Bare Index objects
            # with string column names have no table attached and cannot be
            # created by SQLAlchemy's DDL visitor.
            next(index for index in CanonicalRound.__table__.indexes
                 if index.name == "canonical_rounds_source_sequence_idx").create(
                     connection, checkfirst=True)
            next(index for index in BehaviourApiRound.__table__.indexes
                 if index.name == "behaviour_api_round_stream_status_index_idx").create(
                     connection, checkfirst=True)
            connection.execute(SchemaMigration.__table__.insert().values(
                version=16, applied_at=datetime.now(timezone.utc)))
            applied.append(16)
        if 17 not in applied:
            BehaviourStrategySettlement.__table__.create(connection, checkfirst=True)
            connection.execute(SchemaMigration.__table__.insert().values(
                version=17, applied_at=datetime.now(timezone.utc)))
            applied.append(17)


def make_session_factory(engine: Engine):
    return sessionmaker(bind=engine, expire_on_commit=False, autoflush=False, future=True)
