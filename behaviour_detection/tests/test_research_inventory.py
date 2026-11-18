from collections import Counter
import hashlib
import json
import pytest

from behaviour_detection.analysis import classify_multiplier
from behaviour_detection.analysis import disjoint_reference_blocks
from behaviour_detection.importer import (
    DEFAULT_RESEARCH_ROOT, _canonical_rows, _manifest_summary, _read_csv,
    _snapshot_input_digest, _source_payload_index,
)


def test_complete_phase1_sources_and_rounds_reconcile_before_import():
    root = DEFAULT_RESEARCH_ROOT
    evidence = _read_csv(root / "data" / "unified" / "source_reconciliation.csv")
    summary, _snapshot_hash = _manifest_summary(root, evidence)
    source_payloads = _source_payload_index(root)
    canonical, inversion_ids = _canonical_rows(root)
    assert len(evidence) == summary["reconciliation"]["source_rows_accounted"] == 171378
    assert len(source_payloads) == 171378
    assert len(canonical) == summary["reconciliation"]["research_rounds"] == 50123
    assert len(inversion_ids) == 120
    counts = Counter(classify_multiplier(row["multiplier"]) for row in canonical)
    assert counts == {"H": 24339, "L": 25651, "E": 133}
    mapped = Counter(row["status"] for row in evidence)
    assert sum(mapped.values()) == 171378
    assert all((row["source"], row["source_row"]) in source_payloads for row in evidence)
    assert len(_snapshot_input_digest(root)) == 64


def test_phase1_expanding_reference_positions_are_already_one_based():
    symbols = ["H", "L"] * 250
    blocks = disjoint_reference_blocks(symbols)
    assert blocks[0]["first_position"] == 201
    assert blocks[0]["last_position"] == 300
    assert blocks[1]["first_position"] == 301
    assert blocks[1]["last_position"] == 400


def test_first_import_rejects_raw_payload_changed_after_manifest_export(tmp_path):
    temp_root = tmp_path / "research"
    source_dir = temp_root / "data" / "all_sources"
    unified_dir = temp_root / "data" / "unified"
    reports_dir = temp_root / "reports"
    source_dir.mkdir(parents=True)
    unified_dir.mkdir(parents=True)
    reports_dir.mkdir()
    raw_file = source_dir / "aviator_rounds.csv"
    raw_file.write_text("id,multiplier\n1,2\n", encoding="utf-8")
    expected_raw_hash = hashlib.sha256(raw_file.read_bytes()).hexdigest()
    raw_file.write_text("id,multiplier\n1,9\n", encoding="utf-8")
    extraction = source_dir / "aviator_rounds_dump.csv"
    extraction.write_text("source_row,id,multiplier\n", encoding="utf-8")
    unified = unified_dir / "unified_rounds.csv"
    reconciliation = unified_dir / "source_reconciliation.csv"
    unified.write_text("round_id,multiplier\nr1,2\n", encoding="utf-8")
    reconciliation.write_text("source,source_row,research_key,status\na,1,r1,mapped\n", encoding="utf-8")
    recon = {
        "source_rows_accounted": 1, "research_rounds": 50123, "valid_multiplier_rounds": 50123,
        "unified_csv_sha256": hashlib.sha256(unified.read_bytes()).hexdigest(),
        "reconciliation_csv_sha256": hashlib.sha256(reconciliation.read_bytes()).hexdigest(),
    }
    (reports_dir / "unified_reconciliation.json").write_text(json.dumps(recon), encoding="utf-8")
    (source_dir / "manifest.json").write_text(json.dumps({
        "tables": {"aviator_rounds": {"rows": 1, "file": raw_file.name,
                                        "sha256": expected_raw_hash}},
        "json_sources": [],
    }), encoding="utf-8")
    (source_dir / "dated_dump_manifest.json").write_text(json.dumps({
        "rows": 0, "extracted_columns_sha256": hashlib.sha256(extraction.read_bytes()).hexdigest(),
    }), encoding="utf-8")
    evidence = [{"source": "a", "source_row": "1", "research_key": "r1", "status": "mapped"}]
    with pytest.raises(ValueError, match="raw source bytes do not match"):
        _manifest_summary(temp_root, evidence)
