"""Explicitly delete old, unreferenced raw round rows from PostgreSQL.

This command is dry-run by default. It never deletes decisions, sessions,
execution/transaction evidence, or audit records. Referenced rounds block the
whole operation so those records remain meaningful.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from app.core.config import get_settings  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402
import psycopg  # noqa: E402


ROUND_TABLES = ("aviator_rounds", "rounds", "demo_rounds", "demo_round_observations")
PROTECTED_REFERENCES = (
    ("bet_executions", "target_round_id", "target_round_index"),
    ("reconciliation_records", "round_id", None),
    ("rule_target_outcomes", "target_round_id", "target_round_index"),
    ("rule_selections", "source_round_id", None),
    ("rule_selections", "target_round_id", "target_round_index"),
    ("rule_evaluations", "source_round_id", None),
    ("rules_engine_evaluations", "source_round_id", None),
    ("demo_rule_evaluations", "source_round_id", None),
    ("demo_rule_decisions", "source_round_id", None),
    ("demo_execution_attempts", "target_round_id", "target_round_index"),
    ("collector_round_observation_events", "round_id", None),
)


def _parse_before(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("--before must be an ISO-8601 timestamp") from exc
    if result.tzinfo is None:
        raise argparse.ArgumentTypeError("--before must include a timezone")
    return result.astimezone(timezone.utc)


def _table_columns(conn, table: str) -> set[str]:
    rows = conn.execute("""SELECT column_name FROM information_schema.columns
        WHERE table_schema='public' AND table_name=%s""", (table,)).fetchall()
    return {str(row["column_name"]) for row in rows}


def _exists(conn, table: str) -> bool:
    return bool(conn.execute("""SELECT 1 FROM information_schema.tables
        WHERE table_schema='public' AND table_name=%s""", (table,)).fetchone())


def _time_expression(columns: set[str]) -> sql.Composable:
    parts = [name for name in ("stored_at", "created_at", "observed_at", "timestamp") if name in columns]
    if not parts:
        raise RuntimeError("ROUND_HISTORY_TIMESTAMP_COLUMN_UNAVAILABLE")
    return sql.SQL("COALESCE({})").format(sql.SQL(",").join(
        sql.SQL("NULLIF({}::text,'')::timestamptz").format(sql.Identifier(name))
        for name in parts))


def _counts(conn, before: datetime, tables: list[tuple[str, set[str]]]) -> dict[str, int]:
    result = {}
    for table, columns in tables:
        if not ({"round_id", "round_index"} & columns):
            raise RuntimeError(f"ROUND_HISTORY_IDENTITY_COLUMN_UNAVAILABLE:{table}")
        expression = _time_expression(columns)
        row = conn.execute(sql.SQL("SELECT COUNT(*) AS count FROM {} WHERE {} < %s").format(
            sql.Identifier(table), expression), (before,)).fetchone()
        result[table] = int(row["count"] or 0)
    return result


def _referenced_counts(conn, before: datetime,
                        tables: list[tuple[str, set[str]]]) -> list[dict[str, object]]:
    refs = []
    for table, columns in tables:
        if not ({"round_id", "round_index"} & columns):
            continue
        round_id = sql.Identifier("round_id") if "round_id" in columns else sql.SQL("NULL::text AS round_id")
        round_index = sql.Identifier("round_index") if "round_index" in columns else sql.SQL("NULL::bigint AS round_index")
        old = sql.SQL("SELECT {},{} FROM {} WHERE {} < %s").format(
            round_id, round_index,
            sql.Identifier(table), _time_expression(columns))
        for protected_table, id_column, index_column in PROTECTED_REFERENCES:
            if not _exists(conn, protected_table):
                continue
            protected_columns = _table_columns(conn, protected_table)
            if id_column not in protected_columns and (not index_column or index_column not in protected_columns):
                continue
            matches = []
            if id_column in protected_columns:
                matches.append(sql.SQL("protected.{} = old.round_id").format(sql.Identifier(id_column)))
            if index_column and index_column in protected_columns:
                matches.append(sql.SQL("protected.{} = old.round_index").format(sql.Identifier(index_column)))
            condition = sql.SQL(" OR ").join(matches)
            statement = sql.SQL("SELECT COUNT(*) AS count FROM ({}) old JOIN {} protected ON ({})").format(
                old, sql.Identifier(protected_table), condition)
            count = int(conn.execute(statement, (before,)).fetchone()["count"] or 0)
            if count:
                refs.append({"history_table": table, "protected_table": protected_table,
                             "matching_records": count})

    # Unknown foreign-key relationships are also treated as a hard stop.
    for table, _ in tables:
        rows = conn.execute("""SELECT fk.conrelid::regclass::text AS child_table
            FROM pg_constraint fk WHERE fk.contype='f' AND fk.confrelid=to_regclass(%s)""",
            (f"public.{table}",)).fetchall()
        if rows:
            refs.append({"history_table": table, "foreign_key_dependencies":
                         sorted({str(row["child_table"]) for row in rows})})
    return refs


def _apply(conn, before: datetime, tables: list[tuple[str, set[str]]]) -> dict[str, int]:
    deleted = {}
    for table, columns in tables:
        expression = _time_expression(columns)
        row = conn.execute(sql.SQL("DELETE FROM {} WHERE {} < %s RETURNING 1").format(
            sql.Identifier(table), expression), (before,))
        deleted[table] = sum(1 for _ in row)
    return deleted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", required=True, type=_parse_before,
                        help="timezone-aware ISO timestamp; only earlier rounds qualify")
    parser.add_argument("--confirm-count", type=int,
                        help="must equal the freshly recounted total number of old round rows")
    parser.add_argument("--confirm-text",
                        help="must equal DELETE ROUND HISTORY BEFORE <the exact --before value>")
    parser.add_argument("--apply", action="store_true", help="apply the confirmed deletion; default is dry-run")
    args = parser.parse_args(argv)
    if args.apply and args.confirm_count is None:
        parser.error("--apply requires --confirm-count")

    settings = get_settings()
    if not settings.database_url:
        parser.error("DATABASE_URL must point to PostgreSQL")
    with psycopg.connect(settings.database_url, row_factory=dict_row) as conn:
        if not conn.execute("SELECT 1 FROM pg_database WHERE datname=current_database()").fetchone():
            raise RuntimeError("POSTGRESQL_DATABASE_UNAVAILABLE")
        existing = [(name, _table_columns(conn, name)) for name in ROUND_TABLES if _exists(conn, name)]
        if not existing:
            print(json.dumps({"mode": "DRY_RUN", "eligible": {}, "total": 0,
                              "message": "No raw round-history tables exist."}, indent=2))
            return 0

        if args.apply:
            # Serialize cleanup and block concurrent table writers while counts,
            # references, and deletion run in this single transaction.
            conn.execute("SELECT pg_advisory_xact_lock(hashtext('winner-round-history-cleanup'))")
            for name, _ in existing:
                conn.execute(sql.SQL("LOCK TABLE {} IN ACCESS EXCLUSIVE MODE").format(sql.Identifier(name)))
        eligible = _counts(conn, args.before, existing)
        total = sum(eligible.values())
        refs = _referenced_counts(conn, args.before, existing)
        confirmation = f"DELETE ROUND HISTORY BEFORE {args.before.isoformat()}"
        report = {"mode": "APPLY" if args.apply else "DRY_RUN", "before": args.before.isoformat(),
                  "eligible": eligible, "total": total, "protected_references": refs,
                  "required_confirm_text": confirmation,
                  "preserved_tables": ["rules_engine_evaluations", "demo_rule_sessions",
                      "demo_rule_evaluations", "demo_rule_decisions", "demo_execution_attempts",
                      "bet_executions", "reconciliation_records", "financial_transactions", "audit_logs",
                      "round_distribution_events"]}
        if not args.apply:
            print(json.dumps(report, indent=2))
            return 0
        if refs:
            raise RuntimeError("ROUND_HISTORY_REFERENCED_EVIDENCE_BLOCKS_CLEANUP")
        if args.confirm_count != total:
            raise RuntimeError("ROUND_HISTORY_CONFIRM_COUNT_MISMATCH")
        if args.confirm_text != confirmation:
            raise RuntimeError("ROUND_HISTORY_CONFIRM_TEXT_MISMATCH")
        deleted = _apply(conn, args.before, existing)
        if deleted != eligible:
            raise RuntimeError("ROUND_HISTORY_DELETE_COUNT_CHANGED")
        report["deleted"] = deleted
        conn.commit()
        print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
