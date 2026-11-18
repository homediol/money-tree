from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

from sqlalchemy import String, insert, literal, select
from sqlalchemy.orm import Session

from .analysis import (
    baseline_assessment, classify_multiplier, classify_state, disjoint_reference_blocks,
    motif_evidence, rolling_features, sequence_features, time_groups,
    validate_expanding_baseline,
)
from .models import (
    AnalysisSnapshot, AuditLog, BehaviourStateHistory, CanonicalRound, CumulativeStats,
    DiscoveredMotif, DistributionSnapshot, ForecastHistory, ForecastReferenceBlock,
    Next100Assessment, ResearchFinding, ResearchImport, RoundSourceMapping,
    MotifResearchEvidence, RuleDefinition, SequenceObservation, SequenceOutlookResult, SourceRecord,
    TimeGroup, WorkerCheckpoint,
)
from .rules import RULES, RULE_VERSION

SNAPSHOT_ID = "phase1-2026-11-13T083013Z"
DEFAULT_RESEARCH_ROOT = Path(__file__).resolve().parent.parent / "research_phase1_2026-11-13"
_PHASE1_COMMAND_LOCK = "advanced-crash-behaviour-phase1-import-command"


@contextmanager
def phase1_import_command_lock(engine):
    """Serialize the import and its separately committed motif enrichment."""
    if engine.dialect.name != "postgresql":
        yield
        return
    statement = f"SELECT pg_advisory_lock(hashtext('{_PHASE1_COMMAND_LOCK}'))"
    unlock = f"SELECT pg_advisory_unlock(hashtext('{_PHASE1_COMMAND_LOCK}'))"
    with engine.connect() as connection:
        connection.exec_driver_sql(statement)
        connection.commit()
        try:
            yield
        finally:
            connection.exec_driver_sql(unlock)
            connection.commit()


def import_phase1_and_enrich(engine, research_root: Path, *, batch_size: int = 1000) -> dict:
    """Keep concurrent CLI invocations out of the import/enrichment gap."""
    with phase1_import_command_lock(engine):
        result = import_phase1(engine, research_root, batch_size=batch_size)
        result["motif_research_enrichment"] = enrich_motif_research(engine, research_root)
        return result


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return [dict(row) for row in csv.DictReader(f)]


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _stamp(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _hash_payload(payload: dict) -> str:
    packed = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(packed.encode("utf-8")).hexdigest()


def _validate_rule_storage_lengths() -> None:
    for rule in RULES:
        for column in RuleDefinition.__table__.columns:
            limit = column.type.length if isinstance(column.type, String) else None
            value = rule.get(column.key)
            if limit is not None and isinstance(value, str) and len(value) > limit:
                raise ValueError(f"rule {rule['rule_id']} field {column.key} is {len(value)} characters; schema allows {limit}")


def _source_payload_index(root: Path) -> dict[tuple[str, str], dict]:
    source_dir = root / "data" / "all_sources"
    index: dict[tuple[str, str], dict] = {}

    def add(source: str, key: object, row: dict) -> None:
        identity = (source, str(key))
        if identity in index:
            raise ValueError(f"duplicate source payload identity: {source}/{key}")
        index[identity] = row

    for row in _read_csv(source_dir / "aviator_rounds.csv"):
        add("aviator_rounds", row.get("id", ""), row)
    for row in _read_csv(source_dir / "analysis_rounds.csv"):
        add(f"analysis_rounds:{row.get('dataset_source', '')}", row.get("id", ""), row)
    for source, filename in (("collector_round_observation_events", "collector_round_observation_events.csv"),
                             ("data/roundhistory.json", "data_roundhistory.json"),
                             ("backend/data/roundhistory.json", "backend_data_roundhistory.json")):
        if filename.endswith(".csv"):
            for pos, row in enumerate(_read_csv(source_dir / filename)):
                add(source, pos, row)
        else:
            for pos, row in enumerate(_read_json(source_dir / filename)):
                add(source, pos, row)
    for row in _read_csv(source_dir / "round_distribution_events.csv"):
        add("round_distribution_events", row.get("stream_sequence", ""), row)
    for row in _read_csv(source_dir / "rule_target_outcomes.csv"):
        add("rule_target_outcomes", row.get("outcome_id", ""), row)
    for row in _read_csv(source_dir / "aviator_rounds_dump.csv"):
        add("data/aviator_rounds.dump", row.get("source_row", ""), row)
    return index


def _canonical_rows(root: Path) -> tuple[list[dict], set[str]]:
    rows = _read_csv(root / "data" / "unified" / "unified_rounds.csv")
    rows.sort(key=lambda r: (
        _stamp(r.get("timestamp")) or datetime.max.replace(tzinfo=timezone.utc),
        int(r["round_index"]) if (r.get("round_index") or "").isdigit() else 2**63,
        r.get("research_key", ""),
    ))
    inversions_file = root / "unified_results" / "tables" / "chronology_inversions.csv"
    inversion_ids: set[str] = set()
    if inversions_file.exists():
        for row in _read_csv(inversions_file):
            for name in ("before_round_id", "after_round_id"):
                if row.get(name):
                    inversion_ids.add(row[name])
    return rows, inversion_ids


def _manifest_summary(root: Path, evidence: list[dict]) -> tuple[dict, str]:
    manifest = _read_json(root / "data" / "all_sources" / "manifest.json")
    recon = _read_json(root / "reports" / "unified_reconciliation.json")
    source_dir = root / "data" / "all_sources"
    for table_name, metadata in manifest.get("tables", {}).items():
        filename = metadata.get("file")
        if not filename:
            # The manifest also inventories operational tables that have no
            # exported row payload. They are not part of the frozen round-source
            # reconciliation set; their counts remain present in the manifest.
            continue
        source_path = (source_dir / str(filename)).resolve()
        if not filename or not source_path.is_relative_to(source_dir.resolve()) or not source_path.is_file():
            raise ValueError(f"manifest source file is missing or invalid: {table_name}")
        actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if actual_hash != metadata.get("sha256"):
            raise ValueError(f"raw source bytes do not match the Phase 1 manifest: {table_name}")
    for item in manifest.get("json_sources", []):
        filename = item.get("file")
        source_path = (source_dir / str(filename)).resolve()
        if not filename or not source_path.is_relative_to(source_dir.resolve()) or not source_path.is_file():
            raise ValueError(f"manifest JSON source file is missing or invalid: {item.get('path')}")
        actual_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        if actual_hash != item.get("sha256"):
            raise ValueError(f"JSON source bytes do not match the Phase 1 manifest: {item.get('path')}")
    dated_dump = _read_json(source_dir / "dated_dump_manifest.json")
    extracted_path = source_dir / "aviator_rounds_dump.csv"
    if hashlib.sha256(extracted_path.read_bytes()).hexdigest() != dated_dump.get("extracted_columns_sha256"):
        raise ValueError("dated dump extraction does not match its Phase 1 manifest digest")
    expected_sources = sum(v["rows"] for k, v in manifest["tables"].items()
                           if k in ("aviator_rounds", "analysis_rounds", "collector_round_observation_events",
                                    "round_distribution_events", "rule_target_outcomes"))
    expected_sources += sum(v["rows"] for v in manifest["json_sources"])
    expected_sources += int(_read_json(root / "data" / "all_sources" / "dated_dump_manifest.json")["rows"])
    if expected_sources != len(evidence) or expected_sources != recon["source_rows_accounted"]:
        raise ValueError(f"source reconciliation mismatch: manifest={expected_sources}, evidence={len(evidence)}, report={recon['source_rows_accounted']}")
    if recon.get("research_rounds") != 50123 or recon.get("valid_multiplier_rounds") != 50123:
        raise ValueError("Phase 1 canonical population differs from the required 50,123 valid rounds")
    canonical_digest = hashlib.sha256((root / "data" / "unified" / "unified_rounds.csv").read_bytes()).hexdigest()
    evidence_digest = hashlib.sha256((root / "data" / "unified" / "source_reconciliation.csv").read_bytes()).hexdigest()
    if canonical_digest != recon["unified_csv_sha256"] or evidence_digest != recon["reconciliation_csv_sha256"]:
        raise ValueError("Phase 1 CSV bytes do not match the hashes in unified_reconciliation.json")
    source_hash = hashlib.sha256((canonical_digest + evidence_digest).encode()).hexdigest()
    return {"manifest": manifest, "reconciliation": recon}, source_hash


def _snapshot_input_digest(root: Path) -> str:
    """Hash every file consumed by the import, not just manifest-declared digests."""
    paths = [
        "data/all_sources/manifest.json", "data/all_sources/dated_dump_manifest.json",
        "data/all_sources/aviator_rounds.csv", "data/all_sources/analysis_rounds.csv",
        "data/all_sources/collector_round_observation_events.csv",
        "data/all_sources/data_roundhistory.json", "data/all_sources/backend_data_roundhistory.json",
        "data/all_sources/round_distribution_events.csv", "data/all_sources/rule_target_outcomes.csv",
        "data/all_sources/aviator_rounds_dump.csv", "data/unified/unified_rounds.csv",
        "data/unified/source_reconciliation.csv", "reports/unified_reconciliation.json",
        "unified_results/tables/chronology_inversions.csv",
        "unified_results/reports/machine_readable_findings.json",
        "unified_results/tables/sequence_ngrams_2_to_8.csv",
        "unified_results/tables/motif_block_shuffle_tests.csv",
    ]
    digest = hashlib.sha256()
    for relative in paths:
        path = root / relative
        if not path.is_file():
            raise ValueError(f"required Phase 1 import input is missing: {relative}")
        raw = path.read_bytes()
        digest.update(relative.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(raw).digest())
    return digest.hexdigest()


def import_phase1(engine, research_root: Path = DEFAULT_RESEARCH_ROOT, batch_size: int = 1000) -> dict:
    _validate_rule_storage_lengths()
    root = research_root.resolve()
    evidence = _read_csv(root / "data" / "unified" / "source_reconciliation.csv")
    canon, inversion_ids = _canonical_rows(root)
    manifest_data, snapshot_hash = _manifest_summary(root, evidence)
    input_files_hash = _snapshot_input_digest(root)
    findings = _read_json(root / "unified_results" / "reports" / "machine_readable_findings.json")
    source_index = _source_payload_index(root)
    source_expected = int(manifest_data["reconciliation"]["source_rows_accounted"])
    if len(source_index) != source_expected:
        raise ValueError(f"raw sources loaded={len(source_index)}; expected={source_expected}")

    recon_by_identity: dict[tuple[str, str], dict] = {}
    status_counts = Counter()
    mapped_source_count = 0
    for item in evidence:
        identity = (item["source"], item["source_row"])
        if identity in recon_by_identity:
            raise ValueError(f"duplicate reconciliation key: {identity}")
        if identity not in source_index:
            raise ValueError(f"reconciliation row has no raw source payload: {identity}")
        recon_by_identity[identity] = item
        status_counts[item["status"]] += 1
        mapped_source_count += bool(item.get("research_key"))
    extra = set(source_index) - set(recon_by_identity)
    if extra:
        raise ValueError(f"raw source payloads without reconciliation evidence: {len(extra)}")
    if mapped_source_count != source_expected:
        raise ValueError(f"{source_expected - mapped_source_count} raw source rows have no canonical research key")

    primary_payload_by_research_key = {
        item.get("research_key"): source_index[(item["source"], item["source_row"])]
        for item in evidence if item["source"] == "aviator_rounds" and item.get("research_key")
    }

    index_by_round_id = {r.get("round_id"): r.get("research_key") for r in canon if r.get("round_id")}
    timestamp_counts = Counter(r.get("timestamp") for r in canon)
    round_db_rows = []
    symbols = []
    keys = set()
    for position, row in enumerate(canon, 1):
        key = row.get("research_key", "")
        if not key or key in keys:
            raise ValueError("canonical research key is missing or duplicated")
        keys.add(key)
        raw = row.get("multiplier", "")
        try:
            value = Decimal(raw)
            symbol = classify_multiplier(value)
        except (InvalidOperation, ValueError):
            raise ValueError(f"invalid Phase 1 multiplier on research key {key}") from None
        event_timestamp = _stamp(row.get("timestamp"))
        if event_timestamp is None:
            raise ValueError(f"missing Phase 1 event timestamp on research key {key}")
        quality = "shared_timestamp" if timestamp_counts[row.get("timestamp")] > 1 else "recorded_event_time"
        round_id = row.get("round_id") or None
        order_quality = "phase1_timestamp_index_key_order"
        if round_id in inversion_ids:
            order_quality = "phase1_order_timestamp_inversion_flagged"
        primary_payload = primary_payload_by_research_key.get(key, {})
        stored_at = _stamp(primary_payload.get("stored_at"))
        round_db_rows.append({
            "identity_key": key, "round_id": round_id,
            "source_round_index": row.get("round_index") or None,
            "source_sequence": None, "analysis_position": position,
            "multiplier_raw": raw, "multiplier_value": value, "outcome": symbol,
            "event_timestamp": event_timestamp, "ingestion_timestamp": stored_at,
            "source_name": row.get("primary_source") or "phase1_unknown",
            "timestamp_quality": quality, "order_quality": order_quality,
            "import_snapshot_id": SNAPSHOT_ID, "inserted_at": datetime.now(timezone.utc),
        })
        symbols.append(symbol)
    class_counts = Counter(symbols)
    expected_counts = {"H": 24339, "L": 25651, "E": 133}
    if len(round_db_rows) != 50123 or dict(class_counts) != expected_counts:
        raise ValueError(f"Phase 1 round reconciliation failed: rounds={len(round_db_rows)}, classes={dict(class_counts)}")
    canonical_keys = {r["identity_key"] for r in round_db_rows}
    if any(item.get("research_key") not in canonical_keys for item in recon_by_identity.values()):
        raise ValueError("a source reconciliation key does not map to an imported canonical round")
    forecast_validation = validate_expanding_baseline(symbols)
    if not forecast_validation.get("matches_phase1_tolerance"):
        raise ValueError(f"expanding forecast baseline does not reproduce Phase 1: {forecast_validation}")

    manifest_file = _read_json(root / "data" / "all_sources" / "dated_dump_manifest.json")
    snapshot_utc = _stamp(manifest_data["manifest"]["created_utc"])
    if snapshot_utc is None:
        raise ValueError("source snapshot time is invalid")

    with Session(engine, expire_on_commit=False) as session:
        # All inserts and reconciliation evidence commit or roll back together.
        with session.begin():
            if engine.dialect.name == "postgresql":
                session.connection().exec_driver_sql(
                    "SELECT pg_advisory_xact_lock(hashtext('advanced-crash-behaviour-round-writer'))")
                session.connection().exec_driver_sql(
                    "SELECT pg_advisory_xact_lock(hashtext('advanced-crash-behaviour-phase1-import'))")
            existing = session.get(ResearchImport, SNAPSHOT_ID)
            if existing:
                if (existing.source_sha256 != snapshot_hash
                        or existing.source_records != source_expected
                        or existing.canonical_rounds != len(round_db_rows)):
                    raise ValueError("existing Phase 1 import does not match the frozen source manifest")
                if existing.input_files_sha256 not in (None, input_files_hash):
                    raise ValueError("Phase 1 import input files differ from the recorded content digest")
                if existing.input_files_sha256 is None:
                    existing.input_files_sha256 = input_files_hash
                    session.add(AuditLog(occurred_at=datetime.now(timezone.utc), actor="phase1_import",
                                         action="source_manifest_digest_attested", object_type="research_snapshot",
                                         object_id=SNAPSHOT_ID,
                                         details={"input_files_sha256": input_files_hash,
                                                  "note": "attested existing import against consumed file bytes"}))
                stats = session.get(CumulativeStats, "global")
                if stats and not stats.log_multiplier_square_sum:
                    values = session.scalars(select(CanonicalRound.multiplier_raw).where(
                        CanonicalRound.import_snapshot_id == SNAPSHOT_ID
                    ).order_by(CanonicalRound.analysis_position)).all()
                    log_values = [math.log(float(Decimal(value))) for value in values]
                    stats.log_multiplier_square_sum = sum(value * value for value in log_values)
                    stats.tail_counts = {
                        f"ge_{threshold}": sum(Decimal(value) >= Decimal(threshold) for value in values)
                        for threshold in ("5", "10", "100", "1000")
                    }
                    session.add(AuditLog(occurred_at=datetime.now(timezone.utc), actor="phase1_import",
                                         action="cumulative_distribution_summary_backfilled",
                                         object_type="research_snapshot", object_id=SNAPSHOT_ID,
                                         details={"rounds": len(values),
                                                  "fields": ["log_multiplier_square_sum", "tail_counts"]}))
                return {"status": "already_imported", "snapshot_id": SNAPSHOT_ID,
                        "source_records": existing.source_records, "canonical_rounds": existing.canonical_rounds,
                        "high": existing.high_count, "low": existing.low_count, "exact": existing.exact_count,
                        "input_files_sha256": existing.input_files_sha256 or input_files_hash}
            for start in range(0, len(round_db_rows), batch_size):
                session.bulk_insert_mappings(CanonicalRound, round_db_rows[start:start + batch_size])
            observations = ({"identity_key": r["identity_key"],
                             "analysis_position": r["analysis_position"],
                             "symbol": r["outcome"], "observed_at": datetime.now(timezone.utc),
                             "order_quality": r["order_quality"]} for r in round_db_rows)
            observation_batch = []
            for observation in observations:
                observation_batch.append(observation)
                if len(observation_batch) >= batch_size:
                    session.bulk_insert_mappings(SequenceObservation, observation_batch)
                    observation_batch.clear()
            if observation_batch:
                session.bulk_insert_mappings(SequenceObservation, observation_batch)
            # Preserve one immutable raw payload and reconciliation status per direct source record.
            raw_rows = []
            for (source, source_row), item in recon_by_identity.items():
                payload = source_index[(source, source_row)]
                raw_rows.append({"snapshot_id": SNAPSHOT_ID, "source": source,
                                 "source_row": source_row, "research_key": item.get("research_key") or None,
                                 "reconciliation_status": item["status"],
                                 "reconciliation_detail": item.get("detail") or None,
                                 "raw_payload": payload, "payload_sha256": _hash_payload(payload)})
                if len(raw_rows) >= batch_size:
                    session.bulk_insert_mappings(SourceRecord, raw_rows)
                    raw_rows.clear()
            if raw_rows:
                session.bulk_insert_mappings(SourceRecord, raw_rows)
            mapping_select = select(
                literal(SNAPSHOT_ID), SourceRecord.research_key, SourceRecord.id,
                SourceRecord.reconciliation_status
            ).where(SourceRecord.snapshot_id == SNAPSHOT_ID)
            session.execute(insert(RoundSourceMapping).from_select(
                ["snapshot_id", "identity_key", "source_record_id", "relation"], mapping_select))
            # Phase 1 reproduces the complete 10/20/50/100/200/500 rolling features
            # from the current order without materializing a redundant 300k-row table.
            global_features = sequence_features(symbols)
            context = [{"identity_key": r["identity_key"], "analysis_position": r["analysis_position"],
                        "outcome": r["outcome"], "multiplier_raw": r["multiplier_raw"],
                        "event_timestamp": r["event_timestamp"]} for r in round_db_rows[-500:]]
            rolling = rolling_features(context)
            latest_seq = sequence_features([r["outcome"] for r in context])
            candidate_state, state_evidence = classify_state(rolling["50"])
            reference = disjoint_reference_blocks(symbols)
            if len(reference) != 499:
                raise ValueError(f"Phase 1 forecast reference blocks={len(reference)}, expected 499")
            next100 = baseline_assessment(reference, latest_position=len(round_db_rows),
                                          latest_identity_key=round_db_rows[-1]["identity_key"],
                                          state=candidate_state)
            ngram_path = root / "unified_results" / "tables" / "sequence_ngrams_2_to_8.csv"
            ngrams = {r["motif"]: r for r in _read_csv(ngram_path)}
            motif_tests = _read_csv(root / "unified_results" / "tables" / "motif_block_shuffle_tests.csv")
            tested_by_pattern = {m["motif"]: m for m in motif_tests}
            selected_patterns = set(tested_by_pattern)
            by_length: dict[int, list[dict]] = {}
            for ngram in ngrams.values():
                size = int(ngram["k"])
                by_length.setdefault(size, []).append(ngram)
            for size, items in by_length.items():
                if size >= 2:
                    selected_patterns.update(x["motif"] for x in sorted(items,
                                                key=lambda x: int(x["count"]), reverse=True)[:30])
            motifs_latest = motif_evidence(symbols, sorted(selected_patterns))
            session.add(AnalysisSnapshot(generated_at=datetime.now(timezone.utc),
                                         latest_identity_key=round_db_rows[-1]["identity_key"],
                                         latest_position=len(round_db_rows), round_count=len(round_db_rows),
                                         features={"global_sequence": global_features,
                                                   "sequence": latest_seq,
                                                   "rolling_windows": rolling,
                                                   "motifs": motifs_latest,
                                                   "state": {"candidate": candidate_state,
                                                             "confirmed_state": candidate_state,
                                                             "pending_state": None, "pending_count": 0,
                                                             **state_evidence},
                                                   "next100": next100,
                                                   "source_counts": dict(status_counts),
                                                   "timestamp_tie_rounds": sum(v for v in timestamp_counts.values() if v > 1)},
                                         source_quality={"snapshot_id": SNAPSHOT_ID,
                                                         "snapshot_utc": snapshot_utc.isoformat(),
                                                         "timestamp_tie_rounds": 8722,
                                                         "chronology_inversion_pairs": 60,
                                                         "event_time_source": "Phase 1 UTC timestamps"}))
            session.add(DistributionSnapshot(generated_at=datetime.now(timezone.utc), horizon="global",
                                             counts=dict(class_counts),
                                             statistics=findings["distribution"]["multiplier"]))
            session.add_all(ForecastReferenceBlock(block_number=b["block_number"],
                                                   first_position=b["first_position"],
                                                   last_position=b["last_position"],
                                                   high_count=b["high_count"], low_count=b["low_count"],
                                                   exact_count=b["exact_count"],
                                                   high_rate_nonexact=b["high_rate_nonexact"],
                                                   sequence_stats=b["sequence"],
                                                   source="phase1_frozen_disjoint_100") for b in reference)
            # Persist the exact discovered research motif family, including its
            # corrected local-block shuffle evidence. These remain descriptive.
            session.add_all(DiscoveredMotif(
                motif=pattern, length=len(pattern),
                historical_count=int(tested_by_pattern[pattern]["observed_count"])
                    if pattern in tested_by_pattern else int(ngrams[pattern]["count"]),
                relative_frequency=float(ngrams[pattern]["frequency_per_possible_window"])
                    if pattern in ngrams else None,
                shuffle_p_value=(float(tested_by_pattern[pattern]["empirical_p_two_sided"])
                                 if pattern in tested_by_pattern else None),
                bh_q_value=(float(tested_by_pattern[pattern]["q_bh"])
                            if pattern in tested_by_pattern else None),
                evidence_status=("local_block_shuffle_BH_q_lt_0.05_descriptive"
                                 if pattern in tested_by_pattern and float(tested_by_pattern[pattern]["q_bh"]) < .05
                                 else "tested_no_BH_evidence_descriptive" if pattern in tested_by_pattern
                                 else "top_phase1_ngram_frequency_descriptive_unadjusted"),
                research_snapshot_id=SNAPSHOT_ID) for pattern in sorted(selected_patterns))
            groups = time_groups([
                {"analysis_position": r["analysis_position"], "outcome": r["outcome"],
                 "multiplier_raw": r["multiplier_raw"], "event_timestamp": r["event_timestamp"]}
                for r in round_db_rows], gap_seconds=180.0452)
            if len(groups) != 427:
                raise ValueError(f"Phase 1 time group reconciliation failed: found {len(groups)}, expected 427")
            session.add_all(TimeGroup(
                first_position=g["first_position"], last_position=g["last_position"],
                start_at=_stamp(g["start"]), end_at=_stamp(g["end"]),
                duration_seconds=g["duration_seconds"],
                threshold_seconds=g["gap_threshold_seconds"], round_count=g["rounds"],
                stats={"sequence": g["sequence"], "multiplier": g["multiplier"],
                       "state": g["state"], "state_evidence": g["state_evidence"]},
                timestamp_quality=g["timestamp_quality"]) for g in groups)
            session.add(Next100Assessment(
                assessed_at=datetime.now(timezone.utc), latest_identity_key=round_db_rows[-1]["identity_key"],
                latest_position=len(round_db_rows), training_blocks=len(reference),
                expected_high_rate_nonexact=next100["expected_high_rate_nonexact"],
                expected_exact_rate=next100["expected_exact_rate"],
                expected_high_count=next100["expected_high_count"], expected_low_count=next100["expected_low_count"],
                expected_exact_count=next100["expected_exact_count"],
                historical_count_distribution=next100["historical_count_distribution"],
                threshold_probabilities=next100["threshold_probabilities"],
                data_sufficiency=next100["data_sufficiency"], state=candidate_state,
                active_rules=[r["rule_id"] for r in RULES if r["enabled"]], explanation=next100["explanation"]))
            session.add(ForecastHistory(assessed_at=datetime.now(timezone.utc),
                                        latest_identity_key=round_db_rows[-1]["identity_key"],
                                        latest_position=len(round_db_rows), forecast_type="next100_expanding_baseline",
                                        forecast=next100, change_from_previous={"initial_snapshot": True}))
            session.add(SequenceOutlookResult(assessed_at=datetime.now(timezone.utc),
                                              latest_identity_key=round_db_rows[-1]["identity_key"],
                                              historical_windows=len(reference),
                                              outlook=next100["historical_count_distribution"],
                                              evidence_status="historical_empirical_distribution_descriptive"))
            session.add(BehaviourStateHistory(generated_at=datetime.now(timezone.utc),
                                              latest_identity_key=round_db_rows[-1]["identity_key"],
                                              observed_state=candidate_state, confirmed_state=candidate_state,
                                              evidence={**state_evidence, "initial_historical_snapshot": True},
                                              rule_version=RULE_VERSION))
            log_values = [math.log(float(Decimal(r["multiplier_raw"]))) for r in round_db_rows]
            tail_counts = {
                f"ge_{threshold}": sum(Decimal(r["multiplier_raw"]) >= Decimal(threshold)
                                         for r in round_db_rows)
                for threshold in ("5", "10", "100", "1000")
            }
            session.add(CumulativeStats(scope="global", round_count=len(round_db_rows),
                                        high_count=class_counts["H"], low_count=class_counts["L"],
                                        exact_count=class_counts["E"],
                                        event_timestamp_count=50123, event_timestamp_missing_count=0,
                                        timestamp_tie_count=8722, order_uncertain_count=len(inversion_ids),
                                        minimum_multiplier=min(Decimal(r["multiplier_raw"]) for r in round_db_rows),
                                        maximum_multiplier=max(Decimal(r["multiplier_raw"]) for r in round_db_rows),
                                        multiplier_sum=sum((Decimal(r["multiplier_raw"]) for r in round_db_rows), Decimal("0")),
                                        log_multiplier_sum=sum(log_values),
                                        log_multiplier_square_sum=sum(value * value for value in log_values),
                                        tail_counts=tail_counts,
                                        updated_at=datetime.now(timezone.utc)))
            session.add(WorkerCheckpoint(name="analysis", last_position=len(round_db_rows),
                                         updated_at=datetime.now(timezone.utc)))
            for finding_key, payload in findings.items():
                session.add(ResearchFinding(snapshot_id=SNAPSHOT_ID,
                                            finding_key=finding_key, payload=payload,
                                            source_sha256=snapshot_hash))
            session.add(ResearchImport(snapshot_id=SNAPSHOT_ID, snapshot_utc=snapshot_utc,
                                       source_sha256=snapshot_hash,
                                       input_files_sha256=input_files_hash,
                                       source_records=source_expected,
                                       canonical_rounds=len(round_db_rows),
                                       high_count=class_counts["H"], low_count=class_counts["L"],
                                       exact_count=class_counts["E"], imported_at=datetime.now(timezone.utc)))
            for rule in RULES:
                if session.get(RuleDefinition, (rule["rule_id"], RULE_VERSION)) is None:
                    session.add(RuleDefinition(**rule))
            session.add(AuditLog(occurred_at=datetime.now(timezone.utc), actor="phase1_import",
                                 action="historical_snapshot_imported", object_type="research_snapshot",
                                 object_id=SNAPSHOT_ID,
                                 details={"source_records": source_expected, "canonical_rounds": len(round_db_rows),
                                          "class_counts": dict(class_counts), "source_status_counts": dict(status_counts),
                                          "dated_dump_source_sha256": manifest_file.get("original_sha256"),
                                          "forecast_validation": forecast_validation}))
        session.expire_all()
    return {"status": "imported", "snapshot_id": SNAPSHOT_ID,
            "snapshot_utc": snapshot_utc.isoformat(), "source_records": source_expected,
            "canonical_rounds": len(round_db_rows), "source_status_counts": dict(status_counts),
            "source_rows_with_canonical_mapping": mapped_source_count,
            "high": class_counts["H"], "low": class_counts["L"], "exact": class_counts["E"],
            "forecast_reference_blocks": 499, "time_groups": 427,
            "input_files_sha256": input_files_hash,
            "motif_test_count": len(motif_tests), "source_sha256": snapshot_hash}


def enrich_motif_research(engine, research_root: Path = DEFAULT_RESEARCH_ROOT) -> dict:
    """Store Phase 1 motif continuations and per-time-group counts, descriptively."""
    from sqlalchemy.orm import Session
    from sqlalchemy import select
    from .models import CanonicalRound, DiscoveredMotif, MotifResearchEvidence, TimeGroup

    root = research_root.resolve()
    ngrams = {r["motif"]: r for r in _read_csv(
        root / "unified_results" / "tables" / "sequence_ngrams_2_to_8.csv")}
    shuffle = {r["motif"]: r for r in _read_csv(
        root / "unified_results" / "tables" / "motif_block_shuffle_tests.csv")}
    transitions: dict[str, list[dict]] = {}
    for row in _read_csv(root / "unified_results" / "tables" / "sequence_context_transitions.csv"):
        transitions.setdefault(row["context"], []).append({
            "next": row["next"], "count": int(row["count"]), "support": int(row["support"]),
            "conditional_probability": float(row["conditional_probability"]),
        })

    with Session(engine, expire_on_commit=False) as session:
        motifs = session.scalars(select(DiscoveredMotif).where(
            DiscoveredMotif.research_snapshot_id == SNAPSHOT_ID).order_by(
                DiscoveredMotif.length, DiscoveredMotif.motif)).all()
        existing = session.scalar(select(MotifResearchEvidence.motif).where(
            MotifResearchEvidence.research_snapshot_id == SNAPSHOT_ID).limit(1))
        if existing:
            return {"status": "already_enriched", "motifs": len(motifs)}
        groups = session.scalars(select(TimeGroup).order_by(TimeGroup.first_position)).all()
        outcomes = session.scalars(select(CanonicalRound.outcome).where(
            CanonicalRound.import_snapshot_id == SNAPSHOT_ID).order_by(
                CanonicalRound.analysis_position)).all()
    patterns = {m.motif for m in motifs}
    time_counts: dict[str, list[dict]] = {pattern: [] for pattern in patterns}
    for group_number, group in enumerate(groups, 1):
        symbols = outcomes[group.first_position - 1:group.last_position]
        counts = Counter()
        for size in range(2, 9):
            counts.update("".join(symbols[i:i + size])
                          for i in range(max(0, len(symbols) - size + 1))
                          if "E" not in symbols[i:i + size])
        for pattern in patterns:
            count = counts.get(pattern, 0)
            if count:
                time_counts[pattern].append({
                    "group_number": group_number, "time_group_id": group.id,
                    "start_at": group.start_at.isoformat(), "end_at": group.end_at.isoformat(),
                    "rounds": group.round_count, "occurrences": count,
                    "timestamp_quality": group.timestamp_quality,
                })
    with Session(engine, expire_on_commit=False) as write_session:
        with write_session.begin():
            for motif in motifs:
                ngram = ngrams.get(motif.motif, {})
                test = shuffle.get(motif.motif, {})
                rows = time_counts[motif.motif]
                write_session.add(MotifResearchEvidence(
                    motif=motif.motif, research_snapshot_id=SNAPSHOT_ID,
                    details={
                        "evidence_status": motif.evidence_status,
                        "predictive": False,
                        "historical_ngram": {
                            "occurrences": int(ngram.get("count", motif.historical_count)),
                            "frequency_per_possible_window": float(ngram["frequency_per_possible_window"])
                                if ngram.get("frequency_per_possible_window") else motif.relative_frequency,
                            "first_analysis_position_zero_based": int(ngram["positions_first"])
                                if ngram.get("positions_first") else None,
                        },
                        "local_block_shuffle": ({
                            "null_mean": float(test["null_mean"]),
                            "null_p025": float(test["null_p025"]),
                            "null_p975": float(test["null_p975"]),
                            "excess_count": float(test["excess_count"]),
                            "empirical_p_two_sided": float(test["empirical_p_two_sided"]),
                            "bh_q_value": float(test["q_bh"]),
                        } if test else None),
                        "time_distribution": {
                            "nonzero_group_count": len(rows), "total_groups": len(groups),
                            "groups": rows,
                        },
                        "subsequent_symbol_distribution": {
                            "status": "retrospective_conditional_frequency_not_prediction",
                            "rows": transitions.get(motif.motif, []),
                        },
                    }))
    return {"status": "enriched", "motifs": len(motifs),
            "time_groups": len(groups),
            "nonzero_motif_group_observations": sum(len(rows) for rows in time_counts.values())}
