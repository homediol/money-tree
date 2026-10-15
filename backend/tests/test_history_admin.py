import csv
import json

import pytest

from app.database.repository import Repository
from app.history_admin import (HistoryAdminError, clear_round_history,
                               export_round_history, verify_round_export)


def _repo(tmp_path, rounds=501):
    repository = Repository(tmp_path / "history-admin.sqlite3")
    repository.init()
    with repository.connect() as conn:
        conn.executemany(
            "INSERT INTO rounds(round_id,round_index,multiplier,timestamp,target) VALUES(?,?,?,?,?)",
            [(f"local-{index}", index, f"{1 + index / 100:.2f}",
              f"2026-10-15T00:{index // 60:02d}:{index % 60:02d}Z", 2)
             for index in range(1, rounds + 1)],
        )
    return repository


@pytest.mark.parametrize("fmt", ["json", "csv"])
def test_streamed_export_contains_all_rows_and_verifies_count_checksum_and_boundaries(tmp_path, fmt):
    repository = _repo(tmp_path, rounds=501)
    manifest = export_round_history(repository, tmp_path / "data" / "state.sqlite3", fmt)
    assert manifest["record_count"] == 501
    assert manifest["database_snapshot_id"] is None
    assert len(manifest["checksum_sha256"]) == 64

    path = tmp_path / "data" / "history_exports" / f"round-history-{manifest['export_id']}.{fmt}"
    assert path.is_file() and path.stat().st_size > 0
    if fmt == "json":
        exported = json.loads(path.read_text(encoding="utf-8"))
        assert len(exported["rounds"]) == 501
        assert exported["rounds"][0]["id"] == 1
        assert exported["rounds"][-1]["id"] == 501
        assert exported["rounds"][0]["local_round_index"] == 1
    else:
        with path.open(encoding="utf-8", newline="") as source:
            rows = list(csv.DictReader(source))
        assert len(rows) == 501
        assert rows[0]["id"] == "1"
        assert rows[-1]["local_round_index"] == "501"

    verified = verify_round_export(repository, tmp_path / "data" / "state.sqlite3", manifest["export_id"])
    assert verified["ok"] is True
    assert verified["record_count"] == verified["expected_record_count"] == 501
    assert verified["checksum_matches"] is True
    assert verified["boundaries_match"] is True
    assert verified["no_pagination"] is True


def test_corrupted_export_and_clear_without_fresh_verification_are_rejected(tmp_path):
    repository = _repo(tmp_path, rounds=3)
    data_path = tmp_path / "data" / "state.sqlite3"
    manifest = export_round_history(repository, data_path, "json")
    artifact = tmp_path / "data" / "history_exports" / f"round-history-{manifest['export_id']}.json"
    artifact.write_text(artifact.read_text(encoding="utf-8").replace('"round_id":"local-1"', '"round_id":"tampered"'), encoding="utf-8")

    result = verify_round_export(repository, data_path, manifest["export_id"])
    assert result["ok"] is False
    with pytest.raises(HistoryAdminError, match="verified") as error:
        clear_round_history(repository, export_id=manifest["export_id"], expected_count=3,
                            confirmation_count=3, final_confirmation="CLEAR HISTORY")
    assert error.value.code == "VERIFIED_EXPORT_REQUIRED"
    assert repository.round_history_status()["count"] == 3


def test_clear_requires_exact_dual_confirmation_and_preserves_financial_audit(tmp_path):
    repository = _repo(tmp_path, rounds=3)
    data_path = tmp_path / "data" / "state.sqlite3"
    manifest = export_round_history(repository, data_path, "csv")
    verify_round_export(repository, data_path, manifest["export_id"])
    with repository.connect() as conn:
        conn.execute("INSERT INTO system_audit_log(event_type,entity_id,recorded_at,payload) VALUES(?,?,?,?)",
                     ("FINANCIAL_AUDIT_TEST", "receipt-1", "2026-10-15T00:00:00Z", "{}"))
        conn.execute("INSERT INTO balance_ledger(ledger_id,event_type,balance_status,recorded_at,payload) VALUES(?,?,?,?,?)",
                     ("ledger-1", "SETTLEMENT", "VERIFIED", "2026-10-15T00:00:00Z", "{}"))

    with pytest.raises(HistoryAdminError) as error:
        clear_round_history(repository, export_id=manifest["export_id"], expected_count=3,
                            confirmation_count=2, final_confirmation="CLEAR HISTORY")
    assert error.value.code == "CLEAR_COUNT_CONFIRMATION_MISMATCH"
    with pytest.raises(HistoryAdminError) as error:
        clear_round_history(repository, export_id=manifest["export_id"], expected_count=3,
                            confirmation_count=3, final_confirmation="clear history")
    assert error.value.code == "CLEAR_FINAL_CONFIRMATION_REQUIRED"

    result = clear_round_history(repository, export_id=manifest["export_id"], expected_count=3,
                                 confirmation_count=3, final_confirmation="CLEAR HISTORY", actor="test-admin")
    assert result["history_epoch"] == 2
    assert result["cleared_record_count"] == 3
    assert result["new_local_round_index"] == 0
    assert result["next_local_round_index"] == 1
    assert repository.round_history_status()["count"] == 0
    with repository.connect() as conn:
        assert conn.execute("SELECT COUNT(*) AS n FROM system_audit_log").fetchone()["n"] == 1
        assert conn.execute("SELECT COUNT(*) AS n FROM balance_ledger").fetchone()["n"] == 1
        assert conn.execute("SELECT status FROM history_export_manifests WHERE export_id=?",
                            (manifest["export_id"],)).fetchone()["status"] == "USED_FOR_CLEAR"
        conn.execute("INSERT INTO rounds(round_id,round_index,multiplier,timestamp,target) VALUES(?,?,?,?,?)",
                     ("epoch-2-round-1", 1, 2.0, "2026-10-15T01:00:00Z", 2))
        assert conn.execute("SELECT id FROM rounds WHERE round_id='epoch-2-round-1'").fetchone()["id"] == 4
    assert repository.current_history_epoch() == 2


def test_clear_blocks_pending_observation_and_financial_round_references(tmp_path):
    repository = _repo(tmp_path, rounds=2)
    data_path = tmp_path / "data" / "state.sqlite3"
    manifest = export_round_history(repository, data_path, "json")
    verify_round_export(repository, data_path, manifest["export_id"])
    with repository.connect() as conn:
        conn.execute("INSERT INTO collector_round_observation_events(round_id,multiplier,observed_at,payload,event_status) VALUES(?,?,?,?,?)",
                     ("pending-1", 2.0, "2026-10-15T01:00:00Z", "{}", "PENDING"))
    with pytest.raises(HistoryAdminError) as error:
        clear_round_history(repository, export_id=manifest["export_id"], expected_count=2,
                            confirmation_count=2, final_confirmation="CLEAR HISTORY")
    assert error.value.code == "PENDING_OBSERVATIONS_REMAIN"

    with repository.connect() as conn:
        conn.execute("UPDATE collector_round_observation_events SET event_status='PROCESSED'")
        conn.execute("INSERT INTO reconciliation_records(reconciliation_id,execution_id,round_id,status,outcome,created_at,payload) VALUES(?,?,?,?,?,?,?)",
                     ("recon-1", "exec-1", "local-1", "RECONCILED", "WIN", "2026-10-15T01:00:00Z", "{}"))
    with pytest.raises(HistoryAdminError) as error:
        clear_round_history(repository, export_id=manifest["export_id"], expected_count=2,
                            confirmation_count=2, final_confirmation="CLEAR HISTORY")
    assert error.value.code == "FINANCIAL_AUDIT_DEPENDENCY"


def test_clear_rejects_export_when_round_history_changed_after_export(tmp_path):
    repository = _repo(tmp_path, rounds=2)
    data_path = tmp_path / "data" / "state.sqlite3"
    manifest = export_round_history(repository, data_path, "json")
    verify_round_export(repository, data_path, manifest["export_id"])
    with repository.connect() as conn:
        conn.execute("INSERT INTO rounds(round_id,round_index,multiplier,timestamp,target) VALUES(?,?,?,?,?)",
                     ("new-round", 3, 2.0, "2026-10-15T01:00:00Z", 2))
    with pytest.raises(HistoryAdminError) as error:
        clear_round_history(repository, export_id=manifest["export_id"], expected_count=2,
                            confirmation_count=2, final_confirmation="CLEAR HISTORY")
    assert error.value.code == "EXPORT_BOUNDARY_STALE"
