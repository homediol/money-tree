"""Streaming round-history export and fail-closed epoch reset helpers."""
from __future__ import annotations

import csv
import hashlib
import json
import os
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "winner-predict-round-history-v1"
EXPORT_FIELDS = [
    "source_table", "id", "round_id", "round_index", "local_round_index", "history_epoch",
    "multiplier", "timestamp", "platform_timestamp", "platform_round_id", "platform_round_index",
    "observed_at", "stored_at", "collector_session_id", "round_identity_type", "round_index_source",
    "identity_confidence", "continuity_verified", "gap_before", "continuity_proof", "source",
    "source_metadata", "raw", "created_at", "updated_at", "target",
]


class HistoryAdminError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 409, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = details or {}


def _json_value(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.hex()
    return value


def _round_record(row: Any, *, source_table: str, epoch: int) -> dict[str, Any]:
    values = {key: _json_value(value) for key, value in dict(row).items()}
    raw = values.get("raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            pass
    raw_map = raw if isinstance(raw, dict) else {}
    metadata = raw_map.get("source_metadata") or raw_map.get("metadata")
    values.update({
        "source_table": source_table,
        "local_round_index": values.get("local_round_index") or values.get("round_index"),
        "history_epoch": int(values.get("history_epoch") or epoch),
        "collector_session_id": (raw_map.get("collector_session_id") or raw_map.get("session_id")
                                  or raw_map.get("collectorSessionId")),
        "source_metadata": metadata,
        "raw": raw,
    })
    return {field: values.get(field) for field in EXPORT_FIELDS}


def _row_json(row: dict[str, Any]) -> str:
    return json.dumps(row, ensure_ascii=False, separators=(",", ":"), default=_json_value)


def _csv_cell(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=_json_value)
    return value


def _export_root(data_path: Path) -> Path:
    return Path(data_path).resolve().parent / "history_exports"


def _epoch_from_connection(conn) -> int:
    try:
        row = conn.execute("SELECT epoch FROM history_epochs WHERE singleton=1").fetchone()
        return max(1, int(row["epoch"] if row else 1))
    except Exception:
        return 1


def _history_table(repository) -> str:
    return "aviator_rounds" if repository.database_url else "rounds"


def _history_tables(conn, repository) -> list[str]:
    tables = [_history_table(repository)]
    # PostgreSQL deployments may still contain rows in the pre-canonical table.
    # Include those in exports and reset counts rather than silently omitting
    # them from a canonical-table-only download.
    if repository.database_url and _table_exists(conn, repository, "rounds"):
        tables.append("rounds")
    return tables


def _table_round_count(conn, table: str) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
    return int(row["n"] or 0)


def _projection(repository, table: str) -> str:
    if repository.database_url and table == "aviator_rounds":
        return "id,round_id,round_index,local_round_index,history_epoch,multiplier,timestamp::text AS timestamp,platform_timestamp::text AS platform_timestamp,platform_round_id,platform_round_index,observed_at::text AS observed_at,COALESCE(stored_at,created_at)::text AS stored_at,round_identity_type,round_index_source,identity_confidence,continuity_verified,gap_before,continuity_proof,source,raw,created_at::text AS created_at,updated_at::text AS updated_at,NULL::BIGINT AS target"
    if repository.database_url:
        return "id,round_id,round_index,round_index AS local_round_index,history_epoch,multiplier,timestamp::text AS timestamp,NULL::text AS platform_timestamp,NULL::text AS platform_round_id,NULL::BIGINT AS platform_round_index,NULL::text AS observed_at,NULL::text AS stored_at,NULL::text AS round_identity_type,NULL::text AS round_index_source,NULL::text AS identity_confidence,FALSE AS continuity_verified,FALSE AS gap_before,NULL::text AS continuity_proof,NULL::text AS source,NULL::jsonb AS raw,NULL::text AS created_at,NULL::text AS updated_at,target::BIGINT AS target"
    return "id,round_id,round_index,round_index AS local_round_index,history_epoch,multiplier,timestamp,NULL AS platform_timestamp,NULL AS platform_round_id,NULL AS platform_round_index,NULL AS observed_at,NULL AS stored_at,NULL AS round_identity_type,NULL AS round_index_source,NULL AS identity_confidence,NULL AS continuity_verified,NULL AS gap_before,NULL AS continuity_proof,NULL AS source,NULL AS raw,NULL AS created_at,NULL AS updated_at,target"


def _ordered_round_rows(conn, repository, tables: list[str]):
    union = " UNION ALL ".join(
        f"SELECT '{table}' AS source_table,{_projection(repository, table)} FROM {table}"
        for table in tables
    )
    query = f"SELECT * FROM ({union}) history_export ORDER BY round_index ASC,id ASC,source_table ASC"
    if repository.database_url:
        cursor = conn.raw.cursor(name=f"history_export_{uuid.uuid4().hex}")
        cursor.execute(query)
        return cursor
    return conn.execute(query)


def _boundary(conn, repository, tables: list[str], epoch: int) -> tuple[int, dict | None, dict | None]:
    count = sum(_table_round_count(conn, table) for table in tables)
    if not count:
        return 0, None, None
    candidates = []
    for table in tables:
        if not _table_round_count(conn, table):
            continue
        projection = _projection(repository, table)
        first = conn.execute(f"SELECT {projection} FROM {table} ORDER BY round_index ASC,id ASC LIMIT 1").fetchone()
        last = conn.execute(f"SELECT {projection} FROM {table} ORDER BY round_index DESC,id DESC LIMIT 1").fetchone()
        candidates.append(_round_record(first, source_table=table, epoch=epoch))
        candidates.append(_round_record(last, source_table=table, epoch=epoch))
    candidates.sort(key=lambda row: (int(row.get("round_index") or 0), int(row.get("id") or 0), row["source_table"]))
    return count, candidates[0], candidates[-1]


def history_admin_status(repository, data_path: Path, *, collector_status: dict | None = None) -> dict:
    with repository.connect() as conn:
        tables = _history_tables(conn, repository)
        epoch = _epoch_from_connection(conn)
        count, first, last = _boundary(conn, repository, tables, epoch)
        try:
            state_row = conn.execute("SELECT total_history,contiguous_rounds,warmup_status FROM collector_state WHERE singleton=1").fetchone()
        except Exception:
            state_row = None
        try:
            export_row = conn.execute("SELECT export_id,format,record_count,created_at,verified_at,status FROM history_export_manifests ORDER BY created_at DESC LIMIT 1").fetchone()
        except Exception:
            export_row = None
        try:
            clear_row = conn.execute("SELECT payload,updated_at FROM application_state WHERE state_key='last_history_clear'").fetchone()
        except Exception:
            clear_row = None
        try:
            pending_row = conn.execute("SELECT COUNT(*) AS n FROM collector_round_observation_events WHERE event_status IN ('PENDING','RETRY')").fetchone()
            pending = int(pending_row["n"] or 0)
        except Exception:
            pending = 0
    local_index = int((last or {}).get("local_round_index") or 0)
    required = int((collector_status or {}).get("required_history_rounds") or 100)
    clear_payload = None
    if clear_row:
        try:
            clear_payload = json.loads(clear_row["payload"])
        except (TypeError, json.JSONDecodeError):
            clear_payload = None
    manifest = dict(export_row) if export_row else None
    return {
        "history_epoch": epoch, "total_records": count, "first_round": first, "last_round": last,
        "current_local_round_index": local_index, "warmup": {
            "required_rounds": required,
            "collected_rounds": min(local_index, required),
            "status": "READY" if local_index >= required else "WARMING_UP",
        },
        "collector_state": (collector_status or {}).get("status", "UNKNOWN"),
        "collector_pid": (collector_status or {}).get("pid"),
        "pending_queue_count": pending + int((collector_status or {}).get("pending_queue_count") or 0),
        "last_export": manifest,
        "export_verification_status": manifest.get("status") if manifest else "NOT_VERIFIED",
        "last_clear": clear_payload,
    }


def export_round_history(repository, data_path: Path, fmt: str) -> dict:
    fmt = str(fmt).lower()
    if fmt not in {"json", "csv"}:
        raise HistoryAdminError("EXPORT_FORMAT_INVALID", "format must be json or csv", status_code=422)
    export_id = str(uuid.uuid4())
    export_root = _export_root(data_path)
    export_root.mkdir(parents=True, exist_ok=True)
    extension = fmt
    final_path = export_root / f"round-history-{export_id}.{extension}"
    temp_path = final_path.with_suffix(final_path.suffix + ".tmp")
    count = 0
    first = last = None
    snapshot_id = None
    try:
        with repository.connect() as conn:
            if repository.database_url:
                conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                snapshot = conn.execute("SELECT txid_current_snapshot()::text AS snapshot_id").fetchone()
                snapshot_id = str(snapshot["snapshot_id"]) if snapshot else None
            else:
                # Pin a single SQLite read snapshot before counting or streaming;
                # a concurrent collector commit must not produce a mixed export.
                conn.execute("BEGIN")
            epoch = _epoch_from_connection(conn)
            tables = _history_tables(conn, repository)
            expected_count = sum(_table_round_count(conn, table) for table in tables)
            cursor = _ordered_round_rows(conn, repository, tables)
            if fmt == "json":
                with temp_path.open("w", encoding="utf-8", newline="") as output:
                    output.write('{"schema_version":' + json.dumps(SCHEMA_VERSION) + ',"rounds":[\n')
                    first_record = True
                    while True:
                        rows = cursor.fetchmany(1000)
                        if not rows:
                            break
                        for raw_row in rows:
                            row_values = dict(raw_row)
                            source_table = row_values.pop("source_table", None) or tables[0]
                            record = _round_record(row_values, source_table=source_table, epoch=epoch)
                            if first is None:
                                first = record
                            last = record
                            if not first_record:
                                output.write(",")
                            output.write(_row_json(record) + "\n")
                            first_record = False
                            count += 1
                    output.write("]}\n")
                    output.flush()
                    os.fsync(output.fileno())
            else:
                with temp_path.open("w", encoding="utf-8", newline="") as output:
                    writer = csv.DictWriter(output, fieldnames=EXPORT_FIELDS, extrasaction="ignore")
                    writer.writeheader()
                    while True:
                        rows = cursor.fetchmany(1000)
                        if not rows:
                            break
                        for raw_row in rows:
                            row_values = dict(raw_row)
                            source_table = row_values.pop("source_table", None) or tables[0]
                            record = _round_record(row_values, source_table=source_table, epoch=epoch)
                            if first is None:
                                first = record
                            last = record
                            writer.writerow({key: _csv_cell(value) for key, value in record.items()})
                            count += 1
                    output.flush()
                    os.fsync(output.fileno())
            close = getattr(cursor, "close", None)
            if callable(close):
                close()
            if count != expected_count:
                raise HistoryAdminError("EXPORT_COUNT_MISMATCH", "streamed export count differs from the snapshot row count",
                                        details={"expected": expected_count, "exported": count})
        os.replace(temp_path, final_path)
        digest = hashlib.sha256()
        with final_path.open("rb") as artifact:
            for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
                digest.update(chunk)
        checksum = digest.hexdigest()
        created_at = datetime.now(timezone.utc).isoformat()
        manifest = {
            "export_id": export_id, "export_timestamp": created_at, "record_count": count,
            "first_round": first, "last_round": last, "schema_version": SCHEMA_VERSION,
            "checksum_sha256": checksum, "database_snapshot_id": snapshot_id,
            "history_epoch": epoch, "format": fmt, "status": "COMPLETED",
            "download_url": f"/api/history/exports/{export_id}/download",
        }
        with repository.connect() as conn:
            conn.execute("""INSERT INTO history_export_manifests
                (export_id,format,history_epoch,record_count,first_round,last_round,schema_version,
                 checksum_sha256,snapshot_id,artifact_path,created_at,status)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (export_id, fmt, epoch, count, json.dumps(first, separators=(",", ":"), default=_json_value) if first else None,
                 json.dumps(last, separators=(",", ":"), default=_json_value) if last else None,
                 SCHEMA_VERSION, checksum, snapshot_id, str(final_path), created_at, "COMPLETED"))
        return manifest
    except HistoryAdminError:
        for path in (temp_path, final_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    except Exception as exc:
        for path in (temp_path, final_path):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        raise HistoryAdminError("EXPORT_FAILED", f"history export did not complete: {type(exc).__name__}",
                                status_code=503) from exc


def _load_manifest(repository, export_id: str):
    try:
        uuid.UUID(export_id)
    except (ValueError, TypeError):
        raise HistoryAdminError("EXPORT_ID_INVALID", "invalid export id", status_code=404) from None
    with repository.connect() as conn:
        row = conn.execute("SELECT * FROM history_export_manifests WHERE export_id=?", (export_id,)).fetchone()
    if not row:
        raise HistoryAdminError("EXPORT_NOT_FOUND", "export manifest not found", status_code=404)
    return dict(row)


def _parse_boundary(value):
    if not value:
        return None
    return json.loads(value) if isinstance(value, str) else value


def _csv_boundary_matches(actual: dict | None, expected: dict | None) -> bool:
    if actual is None or expected is None:
        return actual is None and expected is None
    for key in EXPORT_FIELDS:
        value = _csv_cell(expected.get(key))
        expected_cell = "" if value is None else str(value)
        if actual.get(key, "") != expected_cell:
            return False
    return True


def _validate_artifact(path: Path, fmt: str) -> tuple[int, dict | None, dict | None]:
    if not path.is_file() or not os.access(path, os.R_OK):
        raise ValueError("artifact is missing or unreadable")
    count = 0
    first = last = None
    if fmt == "json":
        with path.open("r", encoding="utf-8") as source:
            header = source.readline()
            expected = '{"schema_version":' + json.dumps(SCHEMA_VERSION) + ',"rounds":[\n'
            if header != expected:
                raise ValueError("JSON export header is invalid")
            closed = False
            for line in source:
                if line == "]}\n":
                    closed = True
                    if source.read(1):
                        raise ValueError("trailing content after JSON export")
                    break
                value = line.rstrip("\r\n")
                if value.startswith(","):
                    value = value[1:]
                if not value:
                    raise ValueError("empty JSON record line")
                record = json.loads(value)
                if not isinstance(record, dict):
                    raise ValueError("JSON rounds entry is not an object")
                if first is None:
                    first = record
                last = record
                count += 1
            if not closed:
                raise ValueError("JSON export has no closing object")
    elif fmt == "csv":
        with path.open("r", encoding="utf-8", newline="") as source:
            reader = csv.DictReader(source)
            if reader.fieldnames != EXPORT_FIELDS:
                raise ValueError("CSV headers do not match the export schema")
            for record in reader:
                if None in record:
                    raise ValueError("CSV record has an invalid number of columns")
                if first is None:
                    first = record
                last = record
                count += 1
    else:
        raise ValueError("unsupported export format")
    return count, first, last


def verify_round_export(repository, data_path: Path, export_id: str) -> dict:
    manifest = _load_manifest(repository, export_id)
    path = Path(manifest["artifact_path"]).resolve()
    expected_root = _export_root(data_path).resolve()
    if path.parent != expected_root:
        raise HistoryAdminError("EXPORT_PATH_INVALID", "export artifact path is outside the managed export directory")
    try:
        digest = hashlib.sha256()
        with path.open("rb") as artifact:
            for chunk in iter(lambda: artifact.read(1024 * 1024), b""):
                digest.update(chunk)
        actual_checksum = digest.hexdigest()
        count, first, last = _validate_artifact(path, str(manifest["format"]))
        manifest_first, manifest_last = _parse_boundary(manifest.get("first_round")), _parse_boundary(manifest.get("last_round"))
        checksum_ok = actual_checksum == manifest["checksum_sha256"]
        count_ok = count == int(manifest["record_count"])
        if str(manifest["format"]) == "csv":
            boundary_ok = (_csv_boundary_matches(first, manifest_first)
                           and _csv_boundary_matches(last, manifest_last))
        else:
            boundary_ok = (first == manifest_first and last == manifest_last)
        valid = checksum_ok and count_ok and boundary_ok
        checked_at = datetime.now(timezone.utc).isoformat()
        result = {"ok": valid, "export_id": export_id, "verified_at": checked_at,
                  "artifact_accessible": True, "structure_valid": True,
                  "record_count": count, "expected_record_count": int(manifest["record_count"]),
                  "checksum_sha256": actual_checksum, "checksum_matches": checksum_ok,
                  "boundaries_match": boundary_ok, "no_pagination": count_ok}
    except Exception as exc:
        checked_at = datetime.now(timezone.utc).isoformat()
        valid = False
        result = {"ok": False, "export_id": export_id, "verified_at": checked_at,
                  "artifact_accessible": path.is_file() and os.access(path, os.R_OK),
                  "structure_valid": False, "reason": str(exc)[:200]}
    with repository.connect() as conn:
        conn.execute("UPDATE history_export_manifests SET verified_at=?,verification_json=?,status=? WHERE export_id=?",
                     (checked_at, json.dumps(result, separators=(",", ":")), "VERIFIED" if valid else "INVALID", export_id))
    return result


def export_manifest(repository, export_id: str) -> dict:
    row = _load_manifest(repository, export_id)
    return {
        "export_id": row["export_id"], "format": row["format"],
        "history_epoch": int(row["history_epoch"]), "record_count": int(row["record_count"]),
        "first_round": _parse_boundary(row.get("first_round")),
        "last_round": _parse_boundary(row.get("last_round")),
        "schema_version": row["schema_version"], "checksum_sha256": row["checksum_sha256"],
        "database_snapshot_id": row.get("snapshot_id"), "export_timestamp": row["created_at"],
        "verified_at": row.get("verified_at"), "verification_status": row["status"],
        "verification": json.loads(row["verification_json"]) if row.get("verification_json") else None,
        "download_url": f"/api/history/exports/{row['export_id']}/download",
    }


def _foreign_key_dependencies(conn, repository, table: str) -> list[dict]:
    if repository.database_url:
        rows = conn.execute("""SELECT fk.conrelid::regclass::text AS child_table,
            a.attname AS child_column, fk.confdeltype AS delete_rule
            FROM pg_constraint fk
            JOIN LATERAL unnest(fk.conkey) WITH ORDINALITY k(attnum,ord) ON TRUE
            JOIN pg_attribute a ON a.attrelid=fk.conrelid AND a.attnum=k.attnum
            WHERE fk.contype='f' AND fk.confrelid=to_regclass(?) ORDER BY 1,2""",
            (f"public.{table}",)).fetchall()
        return [dict(row) for row in rows]
    names = [row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    dependencies = []
    for child in names:
        quoted = child.replace('"', '""')
        for fk in conn.execute(f'PRAGMA foreign_key_list("{quoted}")').fetchall():
            if str(fk[2]) == table:
                dependencies.append({"child_table": child, "child_column": fk[3], "delete_rule": fk[6]})
    return dependencies


def _table_exists(conn, repository, table: str) -> bool:
    if repository.database_url:
        return bool(conn.execute("SELECT 1 FROM information_schema.tables WHERE table_schema='public' AND table_name=?",
                                 (table,)).fetchone())
    return bool(conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone())


def _financial_reference_dependencies(conn, repository, history_table: str) -> list[dict]:
    refs = []
    candidates = [
        ("reconciliation_records", "round_id"),
        ("bet_executions", "target_round_id"),
        ("rule_target_outcomes", "target_round_id"),
    ]
    for table, column in candidates:
        if not _table_exists(conn, repository, table):
            continue
        try:
            row = conn.execute(f"SELECT COUNT(*) AS n FROM {table} protected JOIN {history_table} history ON history.round_id=protected.{column}").fetchone()
        except Exception:
            continue
        count = int(row["n"] or 0)
        if count:
            refs.append({"table": table, "column": column, "matching_records": count})
    return refs


def clear_round_history(repository, *, export_id: str, expected_count: int,
                        confirmation_count: int, final_confirmation: str,
                        actor: str | None = None) -> dict:
    if type(expected_count) is not int or expected_count < 0 or confirmation_count != expected_count:
        raise HistoryAdminError("CLEAR_COUNT_CONFIRMATION_MISMATCH", "confirm the exact current record count before clearing")
    if final_confirmation != "CLEAR HISTORY":
        raise HistoryAdminError("CLEAR_FINAL_CONFIRMATION_REQUIRED", "the second confirmation must contain CLEAR HISTORY")
    with repository.connect() as conn:
        tables = _history_tables(conn, repository)
        if repository.database_url:
            for table in tables:
                conn.execute(f"LOCK TABLE {table} IN ACCESS EXCLUSIVE MODE")
            manifest_row = conn.execute("SELECT * FROM history_export_manifests WHERE export_id=? FOR UPDATE", (export_id,)).fetchone()
        else:
            # Hold the write reservation from boundary check through deletion,
            # preventing a SQLite collector write between the count check and DELETE.
            conn.execute("BEGIN IMMEDIATE")
            manifest_row = conn.execute("SELECT * FROM history_export_manifests WHERE export_id=?", (export_id,)).fetchone()
        manifest = dict(manifest_row) if manifest_row else None
        if not manifest or manifest.get("status") != "VERIFIED" or not manifest.get("verified_at"):
            raise HistoryAdminError("VERIFIED_EXPORT_REQUIRED", "history clear requires a completed and verified export")
        epoch = _epoch_from_connection(conn)
        current_count, current_first, current_last = _boundary(conn, repository, tables, epoch)
        if current_count != expected_count or current_count != int(manifest["record_count"]):
            raise HistoryAdminError("EXPORT_BOUNDARY_STALE", "round history changed after the verified export; export and verify again",
                                    details={"current_count": current_count, "export_count": int(manifest["record_count"])})
        if epoch != int(manifest["history_epoch"]):
            raise HistoryAdminError("EXPORT_EPOCH_STALE", "verified export belongs to a different history epoch")
        if current_first != _parse_boundary(manifest.get("first_round")) or current_last != _parse_boundary(manifest.get("last_round")):
            raise HistoryAdminError("EXPORT_BOUNDARY_MISMATCH", "verified export first/last round no longer match current history")
        pending = 0
        if _table_exists(conn, repository, "collector_round_observation_events"):
            pending_row = conn.execute("SELECT COUNT(*) AS n FROM collector_round_observation_events WHERE event_status IN ('PENDING','RETRY')").fetchone()
            pending = int(pending_row["n"] or 0)
        if pending:
            raise HistoryAdminError("PENDING_OBSERVATIONS_REMAIN", "collector observation queue has unresolved records",
                                    details={"pending_count": pending})
        fk_dependencies = [dependency for table in tables
                           for dependency in _foreign_key_dependencies(conn, repository, table)]
        if fk_dependencies:
            raise HistoryAdminError("FOREIGN_KEY_DEPENDENCY", "round history is referenced by foreign keys; deletion is blocked",
                                    details={"dependencies": fk_dependencies})
        financial_refs = [dependency for table in tables
                          for dependency in _financial_reference_dependencies(conn, repository, table)]
        if financial_refs:
            raise HistoryAdminError("FINANCIAL_AUDIT_DEPENDENCY", "financial audit records reference rounds that would be deleted",
                                    details={"dependencies": financial_refs})
        open_financial = []
        if _table_exists(conn, repository, "bet_executions"):
            rows = conn.execute("SELECT execution_id,status FROM bet_executions WHERE status NOT IN ('RECONCILED','CLOSED','FAILED','EXPIRED','REJECTED') ORDER BY created_at LIMIT 20").fetchall()
            open_financial = [dict(row) for row in rows]
        if open_financial:
            raise HistoryAdminError("OPEN_FINANCIAL_EXECUTIONS", "unresolved financial executions block history reset",
                                    details={"executions": open_financial})

        next_epoch = epoch + 1
        if _table_exists(conn, repository, "round_distribution_events"):
            conn.execute("DELETE FROM round_distribution_events")
        if _table_exists(conn, repository, "round_distribution_meta"):
            conn.execute("UPDATE round_distribution_meta SET stream_id=?,continuity_gaps=0,continuity_unknown=0 WHERE singleton=1",
                         (uuid.uuid4().hex,))
        for table in tables:
            conn.execute(f"DELETE FROM {table}")
        if _table_exists(conn, repository, "collector_state"):
            conn.execute("""UPDATE collector_state SET collector_state='WAITING',last_processed_round_id=NULL,
                last_round_timestamp=NULL,contiguous_rounds=0,total_history=0,warmup_status='WARMING_UP',updated_at=CURRENT_TIMESTAMP
                WHERE singleton=1""")
        conn.execute("UPDATE history_epochs SET epoch=?,updated_at=CURRENT_TIMESTAMP WHERE singleton=1", (next_epoch,))
        clear_payload = {"history_epoch": next_epoch, "cleared_record_count": current_count,
                         "export_id": export_id, "cleared_at": datetime.now(timezone.utc).isoformat(),
                         "actor": actor, "new_local_round_index": 0, "next_local_round_index": 1}
        conn.execute("""INSERT INTO application_state(state_key,payload,updated_at) VALUES('last_history_clear',?,CURRENT_TIMESTAMP)
            ON CONFLICT(state_key) DO UPDATE SET payload=excluded.payload,updated_at=excluded.updated_at""",
            (json.dumps(clear_payload, separators=(",", ":")),))
        _expire_nonfinancial_intents(conn, repository, next_epoch)
        conn.execute("UPDATE history_export_manifests SET status='USED_FOR_CLEAR' WHERE export_id=?", (export_id,))
    return {"ok": True, **clear_payload}


def _expire_nonfinancial_intents(conn, repository, new_epoch: int) -> None:
    if not _table_exists(conn, repository, "rule_execution_intents"):
        return
    protected = {"ACCEPTED", "PENDING", "UNKNOWN", "RECONCILIATION_REQUIRED", "RECONCILIATION_PENDING", "RECONCILED", "SETTLED"}
    rows = conn.execute("SELECT decision_id,execution_status,payload FROM rule_execution_intents").fetchall()
    for row in rows:
        if str(row["execution_status"] or "").upper() in protected:
            continue
        raw_payload = row["payload"]
        if isinstance(raw_payload, dict):
            payload = raw_payload
        else:
            try:
                payload = json.loads(raw_payload or "{}")
            except (TypeError, json.JSONDecodeError):
                payload = {}
        payload.update({"execution_status": "EXPIRED", "history_epoch_reset": int(new_epoch),
                        "expiration_reason": "HISTORY_EPOCH_RESET"})
        payload_sql = "payload=?::jsonb" if repository.database_url else "payload=?"
        conn.execute(f"UPDATE rule_execution_intents SET execution_status='EXPIRED',{payload_sql} WHERE decision_id=?",
                     (json.dumps(payload, separators=(",", ":")), row["decision_id"]))
