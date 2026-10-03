"""One-time, idempotent migration from the legacy SQLite/JSON stores.

Run after stopping the backend and collector. DATABASE_URL must point to an
empty, dedicated PostgreSQL database. Existing PostgreSQL rows always win.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

from psycopg import sql

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

from app.database.repository import Repository  # noqa: E402
from app.services.dataset_service import DatasetService  # noqa: E402


def migrate(sqlite_path: Path, history_path: Path, database_url: str) -> dict[str, int]:
    if not sqlite_path.is_file() or not history_path.is_file():
        raise FileNotFoundError("SQLite database and roundhistory.json are both required")
    source = sqlite3.connect(f"file:{sqlite_path}?mode=ro", uri=True)
    source.row_factory = sqlite3.Row
    destination = Repository(BACKEND / "unused-migration.sqlite3", database_url, require_postgres=True)
    destination.init()
    counts: dict[str, int] = {}
    try:
        rounds, quality, quarantine = DatasetService(history_path).load_validate()
        if quarantine:
            raise ValueError(f"history contains {len(quarantine)} invalid or duplicate rows")
        sqlite_rounds = source.execute("SELECT COUNT(*) FROM rounds").fetchone()[0]
        if len(rounds) != sqlite_rounds:
            raise ValueError(f"history count {len(rounds)} differs from SQLite count {sqlite_rounds}")
        if quality.get("latest_contiguous_rounds", 0) < 1:
            raise ValueError("history has no valid continuity window")

        # aviator_rounds is canonical. The old SQLite rounds table had lost
        # stable IDs, so take these rows from the validated collector file.
        records = [
            {"round_id": str(row.round_id), "round_index": int(row.round_index),
             "multiplier": float(row.multiplier), "timestamp": row.timestamp}
            for row in rounds.itertuples(index=False)
        ]
        counts["aviator_rounds_attempted"] = destination.import_legacy_rounds(records)
        if len(destination.load_rounds()) != len(records):
            raise RuntimeError("PostgreSQL round count does not match the migration snapshot")

        tables = [row[0] for row in source.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        with destination.connect() as connection:
            for table in tables:
                if table == "rounds":
                    continue
                columns = [row[1] for row in source.execute(f'PRAGMA table_info("{table}")')]
                rows = source.execute(f'SELECT * FROM "{table}"').fetchall()
                if not rows:
                    counts[table] = 0
                    continue
                statement = sql.SQL("INSERT INTO {} ({}) VALUES ({}) ON CONFLICT DO NOTHING").format(
                    sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, columns)),
                    sql.SQL(", ").join(sql.Placeholder() for _ in columns),
                )
                with connection.raw.cursor() as cursor:
                    cursor.executemany(statement, [tuple(row[column] for column in columns) for row in rows])
                counts[table] = len(rows)
                if "id" in columns:
                    sequence = connection.execute(
                        "SELECT pg_get_serial_sequence(?, 'id') AS sequence", (table,)
                    ).fetchone()["sequence"]
                    if sequence:
                        maximum = connection.raw.execute(sql.SQL("SELECT MAX(id) AS maximum FROM {}").format(
                            sql.Identifier(table))).fetchone()["maximum"]
                        if maximum is not None:
                            connection.execute("SELECT setval(?, ?, true)", (sequence, maximum))
        return counts
    finally:
        source.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sqlite", type=Path, default=BACKEND / "winner_predict.sqlite3")
    parser.add_argument("--history", type=Path, default=ROOT / "data" / "roundhistory.json")
    args = parser.parse_args()
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        parser.error("DATABASE_URL must be set")
    counts = migrate(args.sqlite, args.history, database_url)
    for table, count in counts.items():
        print(f"{table}: {count}")


if __name__ == "__main__":
    main()
