"""Import archived round datasets into an isolated PostgreSQL analysis table.

This script never writes to ``aviator_rounds``. It tags each row by its source
file so overlapping indexes from the dump and JSON history stay distinguishable.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from psycopg import connect
from psycopg.types.json import Jsonb

from app.core.config import get_settings


DUMP_COLUMNS = (
    "id", "round_id", "round_index", "multiplier", "timestamp", "source", "raw",
    "created_at", "updated_at", "platform_timestamp", "observed_at", "stored_at",
    "round_identity_type", "round_index_source", "continuity_verified", "gap_before",
    "continuity_proof", "platform_round_id", "platform_round_index", "local_round_index",
    "identity_confidence", "history_epoch",
)


def _copy_unescape(value: str) -> str:
    """Decode PostgreSQL text-COPY escapes emitted by pg_restore."""
    out: list[str] = []
    i = 0
    simple = {"b": "\b", "f": "\f", "n": "\n", "r": "\r",
              "t": "\t", "v": "\v", "\\": "\\"}
    while i < len(value):
        char = value[i]
        if char != "\\" or i + 1 >= len(value):
            out.append(char)
            i += 1
            continue
        code = value[i + 1]
        if code in simple:
            out.append(simple[code])
            i += 2
            continue
        if code == "x" and i + 3 < len(value) and re.fullmatch(r"[0-9a-fA-F]{2}", value[i + 2:i + 4]):
            out.append(chr(int(value[i + 2:i + 4], 16)))
            i += 4
            continue
        if code in "01234567":
            j = i + 1
            while j < min(len(value), i + 4) and value[j] in "01234567":
                j += 1
            out.append(chr(int(value[i + 1:j], 8)))
            i = j
            continue
        out.append(code)
        i += 2
    return "".join(out)


def _copy_value(value: str) -> str | None:
    return None if value == r"\N" else _copy_unescape(value)


def _integer(value: str | None) -> int | None:
    return None if value is None else int(value)


def _boolean(value: str | None) -> bool | None:
    return None if value is None else value == "t"


def _load_dump_rows(path: Path) -> list[dict[str, Any]]:
    restored = subprocess.run(
        ["pg_restore", "--data-only", "--table=aviator_rounds", "--file=-", str(path)],
        check=True, capture_output=True, text=True,
    ).stdout.splitlines()
    copy_line = next((i for i, line in enumerate(restored)
                      if line.startswith("COPY public.aviator_rounds ")), None)
    if copy_line is None:
        raise ValueError(f"{path} does not contain public.aviator_rounds data")
    end = restored.index(r"\.", copy_line + 1)
    result: list[dict[str, Any]] = []
    for fields in csv.reader(restored[copy_line + 1:end], delimiter="\t"):
        if len(fields) != len(DUMP_COLUMNS):
            raise ValueError(f"unexpected aviator_rounds row width: {len(fields)}")
        row = dict(zip(DUMP_COLUMNS, (_copy_value(field) for field in fields)))
        if row["id"] is None or row["round_index"] is None or row["multiplier"] is None:
            raise ValueError("dump contains a round without its required ID, index, or multiplier")
        row["id"] = int(row["id"])
        row["round_index"] = int(row["round_index"])
        row["multiplier"] = float(row["multiplier"])
        for key in ("platform_round_index", "local_round_index", "history_epoch"):
            row[key] = _integer(row[key])
        for key in ("continuity_verified", "gap_before"):
            row[key] = _boolean(row[key])
        row["raw"] = json.loads(row["raw"] or "{}")
        result.append(row)
    return result


def _load_history_rows(path: Path) -> list[dict[str, Any]]:
    values = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(values, list):
        raise ValueError(f"{path} must contain a JSON array")
    rows: list[dict[str, Any]] = []
    for position, value in enumerate(values):
        if not isinstance(value, dict):
            raise ValueError(f"roundhistory row {position} is not an object")
        index = value.get("round_index")
        multiplier = value.get("multiplier")
        timestamp = value.get("timestamp")
        if isinstance(index, bool) or not isinstance(index, int):
            raise ValueError(f"roundhistory row {position} has an invalid round_index")
        if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)):
            raise ValueError(f"roundhistory row {position} has an invalid multiplier")
        if not isinstance(timestamp, str):
            raise ValueError(f"roundhistory row {position} has no timestamp")
        rows.append({
            "source_row_key": str(index), "round_index": index,
            "multiplier": float(multiplier), "timestamp": timestamp,
            "payload": value,
        })
    return rows


def _database_row(dataset: str, table_name: str, row: dict[str, Any]) -> tuple[Any, ...]:
    if dataset == "data/aviator_rounds_backup.dump":
        payload = dict(row)
        return (
            dataset, str(row["id"]), table_name, row["id"], row["round_id"],
            row["round_index"], row["multiplier"], row["timestamp"], row["source"],
            row["observed_at"], row["stored_at"], row["platform_timestamp"],
            row["platform_round_id"], row["platform_round_index"], row["local_round_index"],
            row["round_identity_type"], row["round_index_source"],
            row["identity_confidence"], row["continuity_verified"], row["gap_before"],
            row["continuity_proof"], row["history_epoch"], Jsonb(row["raw"]), Jsonb(payload),
        )
    payload = row["payload"]
    return (
        dataset, row["source_row_key"], table_name, None, None, row["round_index"],
        row["multiplier"], row["timestamp"], "roundhistory.json", None, None, None,
        None, None, row["round_index"], "LEGACY_JSON_HISTORY", "roundhistory.json",
        "UNKNOWN", False, False, "LEGACY_UNVERIFIED", None,
        Jsonb(payload), Jsonb(payload),
    )


CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS public.analysis_rounds (
    id bigint GENERATED BY DEFAULT AS IDENTITY PRIMARY KEY,
    dataset_source text NOT NULL,
    source_row_key text NOT NULL,
    source_table text NOT NULL,
    source_row_id bigint,
    round_id text,
    round_index bigint NOT NULL,
    multiplier numeric(12,2) NOT NULL CHECK (multiplier >= 1),
    round_timestamp timestamptz,
    source text,
    observed_at timestamptz,
    stored_at timestamptz,
    platform_timestamp timestamptz,
    platform_round_id text,
    platform_round_index bigint,
    local_round_index bigint,
    round_identity_type text,
    round_index_source text,
    identity_confidence text,
    continuity_verified boolean,
    gap_before boolean,
    continuity_proof text,
    history_epoch bigint,
    raw jsonb NOT NULL DEFAULT '{}'::jsonb,
    original_payload jsonb NOT NULL,
    imported_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT analysis_rounds_dataset_row_unique UNIQUE (dataset_source, source_row_key)
)
"""


INSERT_ROW = """
INSERT INTO public.analysis_rounds (
    dataset_source, source_row_key, source_table, source_row_id, round_id, round_index,
    multiplier, round_timestamp, source, observed_at, stored_at, platform_timestamp,
    platform_round_id, platform_round_index, local_round_index, round_identity_type,
    round_index_source, identity_confidence, continuity_verified, gap_before,
    continuity_proof, history_epoch, raw, original_payload
) VALUES (
    %s, %s, %s, %s, %s, %s, %s, %s::timestamptz, %s, %s::timestamptz,
    %s::timestamptz, %s::timestamptz, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
    %s, %s
) ON CONFLICT (dataset_source, source_row_key) DO NOTHING
"""


def import_analysis_rounds(dump_path: Path, history_path: Path) -> dict[str, int]:
    if not dump_path.is_file():
        raise FileNotFoundError(dump_path)
    if not history_path.is_file():
        raise FileNotFoundError(history_path)
    dump_rows = _load_dump_rows(dump_path)
    history_rows = _load_history_rows(history_path)
    dump_source = dump_path.relative_to(ROOT).as_posix()
    history_source = history_path.relative_to(ROOT).as_posix()
    rows = [*(_database_row(dump_source, "aviator_rounds", row) for row in dump_rows),
            *(_database_row(history_source, "roundhistory.json", row) for row in history_rows)]

    settings = get_settings()
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required; no live database rows were changed")
    with connect(settings.database_url) as conn:
        with conn.cursor() as cur:
            cur.execute(CREATE_TABLE)
            cur.execute("CREATE INDEX IF NOT EXISTS analysis_rounds_source_index_idx "
                        "ON public.analysis_rounds(dataset_source, round_index)")
            cur.execute("CREATE INDEX IF NOT EXISTS analysis_rounds_timestamp_idx "
                        "ON public.analysis_rounds(round_timestamp)")
            cur.executemany(INSERT_ROW, rows)
            cur.execute("""SELECT dataset_source, count(*)
                FROM public.analysis_rounds
                WHERE dataset_source IN (%s, %s)
                GROUP BY dataset_source""", (dump_source, history_source))
            counts = {source: count for source, count in cur.fetchall()}
            expected = {dump_source: len(dump_rows), history_source: len(history_rows)}
            if any(counts.get(source, 0) != count for source, count in expected.items()):
                raise RuntimeError(f"analysis table count mismatch: expected {expected}, got {counts}")
            cur.execute("SELECT count(*) FROM public.aviator_rounds")
            live_count = cur.fetchone()[0]
    return {**counts, "live_aviator_rounds_count": live_count}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", type=Path, default=ROOT / "data" / "aviator_rounds_backup.dump")
    parser.add_argument("--history", type=Path, default=ROOT / "data" / "roundhistory.json")
    args = parser.parse_args()
    dump_path = args.dump.resolve()
    history_path = args.history.resolve()
    counts = import_analysis_rounds(dump_path, history_path)
    print(json.dumps({"table": "public.analysis_rounds", "counts": counts}, sort_keys=True))


if __name__ == "__main__":
    main()
