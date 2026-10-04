"""Exploratory V3 discovery funnel for next-round >=2.10x outcomes.

V3 is a research stream only. It freezes assessments before the next outcome,
uses the pre-V1-test history for historical exploration, and never emits a
Decision/Risk/Execution input.
"""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timezone
from threading import Lock
from typing import Any

import numpy as np
import pandas as pd
from scipy.stats import binom
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import StandardScaler

from app.ml.opportunity import TARGET, _bucket, validated_rounds, wilson

VERSION = "opportunity-discovery-v3-2026-10"
SEQUENCE_LENGTHS = (3, 4, 5, 6, 7, 8, 10)
STATE_NAMES = ("A", "B", "C", "D", "E", "F")


def _state(value: float) -> str:
    if value < 1.2: return "A"
    if value < 1.5: return "B"
    if value < 2.0: return "C"
    if value < 3.0: return "D"
    if value < 5.0: return "E"
    return "F"


def _contiguous(frame: pd.DataFrame) -> list[tuple[int, int]]:
    if frame.empty: return []
    cuts, start = [], 0
    indexes = frame["round_index"].to_numpy(int)
    for i in range(1, len(frame)):
        if indexes[i] != indexes[i - 1] + 1 or str(frame.iloc[i].round_id) == str(frame.iloc[i - 1].round_id):
            cuts.append((start, i)); start = i
    cuts.append((start, len(frame)))
    return cuts


def _ci_lift(successes: int, count: int, baseline: float) -> dict[str, Any]:
    ci = wilson(successes, count)
    return {"lower": None if ci["lower"] is None else ci["lower"] - baseline,
            "upper": None if ci["upper"] is None else ci["upper"] - baseline,
            "confidence": .95}


def _metric(y: np.ndarray, chosen: np.ndarray, baseline: float) -> dict[str, Any]:
    n = int(chosen.sum()); hits = int(y[chosen].sum()) if n else 0
    ci = wilson(hits, n)
    rest_n = int(len(y) - n); rest_hits = int(y[~chosen].sum()) if rest_n else 0
    rest_ci = wilson(rest_hits, rest_n)
    if n and rest_n:
        p1, p2 = hits / n, rest_hits / rest_n
        delta = p1 - p2
        diff_lower = delta - math.sqrt((p1 - ci["lower"]) ** 2 + (rest_ci["upper"] - p2) ** 2)
        diff_upper = delta + math.sqrt((ci["upper"] - p1) ** 2 + (p2 - rest_ci["lower"]) ** 2)
        factor = rest_n / len(y)
        lift_ci = {"lower": factor * diff_lower, "upper": factor * diff_upper, "confidence": .95,
                   "method": "Newcombe-Wilson selected-vs-rest scaled to global-baseline lift"}
    else:
        lift_ci = {"lower": None, "upper": None, "confidence": .95,
                   "method": "insufficient selected or unselected sample"}
    return {"rounds": int(len(y)), "signals": n, "coverage": n / len(y) if len(y) else None,
            "successes": hits, "precision": hits / n if n else None,
            "precision_ci95": ci, "baseline": baseline,
            "lift": hits / n - baseline if n else None,
            "lift_ci95": lift_ci}


class OpportunityDiscoveryV3:
    """Research-only funnel and immutable prospective assessment writer."""
    def __init__(self, repository):
        self.repository = repository
        self._lock = Lock()
        self.report = repository.load_application_state("selective_opportunity_v3") or {
            "version": VERSION, "status": "NOT_EVALUATED", "target": TARGET,
            "research_shadow_ready": False, "reason": "V3 discovery research has not been run."}

    def current(self, rounds: pd.DataFrame | None = None) -> dict[str, Any]:
        observations = [x for x in self.repository.list_opportunity_observations(limit=100000)
                        if x.get("model_version") == VERSION]
        resolved = [x for x in observations if x.get("status") == "SCORED"]
        pending = [x for x in observations if x.get("status") == "PENDING"]
        clean, quality = validated_rounds(rounds if rounds is not None else pd.DataFrame())
        contiguous = int(quality.get("verified_contiguous_rounds", 0) or 0)
        return {**self.report, "prospective": {
            "rounds_assessed": len(observations), "scored": len(resolved), "pending": len(pending),
            "unknown": sum(x.get("status") == "UNKNOWN" for x in observations),
            "contiguous_clean_suffix": contiguous,
            "observed_consecutive_index_suffix": int(quality.get("observed_consecutive_index_suffix", 0) or 0),
            "platform_identity_verified": bool(len(clean) and clean.get("round_identity_type", pd.Series(dtype=str)).eq("PLATFORM").all()),
            "baseline": (sum(int(x.get("outcome_2_1x", 0)) for x in resolved) / len(resolved)) if resolved else None,
            "top_evidence_rate": None,
            "status": "COLLECTING_PROSPECTIVE_DATA" if observations else "WAITING_FOR_FIRST_VERIFIED_ASSESSMENT",
            "reason": None if observations else "No V3 assessment is frozen yet; the live observer requires an exact collector/dataset latest-round match."},
            "current_assessment": observations[0] if observations else None,
            "data_quality": quality}

    def run(self, rounds: pd.DataFrame) -> dict[str, Any]:
        frozen = self.repository.load_application_state("selective_opportunity_v3")
        if frozen and frozen.get("version") == VERSION and frozen.get("generated_at"):
            self.report = frozen
            return self.current(rounds) | {"research_run": "FROZEN_REPORT_REUSED",
                                           "note": "V3 historical results are immutable; new observations belong to V4."}
        if not self._lock.acquire(blocking=False): return self.current(rounds) | {"status": "RESEARCH_RUNNING"}
        try:
            from app.ml.opportunity_v2 import V1_DIAGNOSTIC_START_INDEX
            clean, quality = validated_rounds(rounds.copy())
            development = clean.loc[clean.round_index < V1_DIAGNOSTIC_START_INDEX].reset_index(drop=True)
            frame_rows, labels, sources, seqs, stability = self._build_rows(development)
            result = self._research(development, frame_rows, labels, sources, seqs, stability, quality)
            from app.ml.opportunity_v2 import V1_SOURCE_END_INDEX
            prospective = clean.loc[clean.round_index > V1_SOURCE_END_INDEX].reset_index(drop=True)
            result["clean_rounds"] = int(quality.get("valid", len(clean)))
            result["historical_development_rounds"] = int(len(development))
            result["prospective_boundary"] = {**result.get("prospective_boundary", {}),
                "old_v1_test_start_round_index": V1_DIAGNOSTIC_START_INDEX,
                "prospective_start_after_v1_source_round_index": V1_SOURCE_END_INDEX,
                "historical_valid_rounds": int(len(development)),
                "prospective_valid_rounds_after_boundary": int(len(prospective)),
                "contiguous_prospective_rounds": self._suffix(prospective),
                "continuity_breaks": self._continuity_audit(prospective),
                "note": "Valid history may contain gaps; V1 test rows are excluded. Verified round identity/index define continuity; clock anomalies are reported separately."}
            result["version"] = VERSION; result["target"] = TARGET
            result["generated_at"] = datetime.now(timezone.utc).isoformat()
            result["old_v1_test"] = "DIAGNOSTIC ONLY; excluded from V3 fitting and evaluation"
            self.repository.save_application_state("selective_opportunity_v3", result, result["generated_at"])
            self.report = result
            return self.current(rounds)
        finally: self._lock.release()

    def _build_rows(self, frame):
        values = frame.multiplier.to_numpy(float)
        rows, labels, sources, seqs, stability = [], [], [], [], []
        for a, b in _contiguous(frame):
            # Each feature uses values through its source round; update exact
            # sequence evidence only after revealing each prior target.
            state_values = np.select([values[a:b] < 1.2, values[a:b] < 1.5, values[a:b] < 2.0,
                                      values[a:b] < 3.0, values[a:b] < 5.0], [0, 1, 2, 3, 4], default=5).astype(np.int8)
            pattern_counts: dict[tuple[str, str], list[int]] = {}
            prior_hits = prior_count = 0
            for source_pos in range(a + 100, b - 1):
                relative = source_pos - a
                hist = values[a:source_pos + 1]
                seq = "".join(STATE_NAMES[int(v)] for v in state_values[max(0, relative - 9):relative + 1])
                seq5 = seq[-5:]
                buckets = _bucket(hist[-1000:])
                key = "|".join(map(str, buckets[-5:]))
                count, hits = pattern_counts.get(("sequence_5", key), [0, 0])
                past_rate = (prior_hits + 10) / (prior_count + 20)
                row = {"pattern_sequence_5_count": count, "pattern_sequence_5_successes": hits,
                       "pattern_sequence_5_rate": (hits + 10 * past_rate) / (count + 10),
                       **{f"rate_ge_2_1_{window}": float(np.mean(hist[-window:] >= TARGET))
                          for window in (100, 250, 500, 1000)}}
                rows.append(row)
                label = int(values[source_pos + 1] >= TARGET)
                labels.append(label)
                seqs.append(seq); sources.append(source_pos)
                prior_base = past_rate
                stability.append({"successes": hits, "count": count,
                    "rate": float(row["pattern_sequence_5_rate"]), "baseline": prior_base,
                    "sequence": seq5, "target_states": seq5})
                for length in (2, 3, 5):
                    pattern_key = "|".join(map(str, buckets[-length:]))
                    pair = pattern_counts.setdefault((f"sequence_{length}", pattern_key), [0, 0])
                    pair[0] += 1; pair[1] += label
                prior_hits += label; prior_count += 1
        return rows, np.asarray(labels, dtype=int), sources, seqs, stability

    def _research(self, clean, rows, y, sources, seqs, stability, quality):
        if len(y) < 500:
            return {"status": "INSUFFICIENT_DATA", "clean_rounds": int(len(clean)),
                    "rounds_analyzed": len(y), "reason": "fewer_than_500_pre_test_eligible_targets",
                    "discovery_funnel": self._empty_funnel(len(y))}
        from app.ml.opportunity_v2 import V1_DIAGNOSTIC_START_INDEX, V1_SOURCE_END_INDEX
        dev_n = len(y)
        # The fixed pre-V1-test development prefix is divided chronologically;
        # its last 25% is exploratory OOS, never a final untouched holdout.
        train_end = int(dev_n * .65); eval_start = train_end
        train_idx, eval_idx = np.arange(train_end), np.arange(eval_start, dev_n)
        feature_names = ["pattern_sequence_5_successes", "pattern_sequence_5_rate"]
        X = np.asarray([[r[n] for n in feature_names] for r in rows], dtype=float)
        med = np.nanmedian(X[:train_end], axis=0); med[~np.isfinite(med)] = 0
        X = np.where(np.isfinite(X), X, med)
        from app.ml.opportunity import _factories
        model_probabilities, model_errors = {}, {}
        for model_name in ("logistic_regression", "extra_trees", "random_forest", "gradient_boosting", "hist_gradient_boosting"):
            factory = _factories().get(model_name)
            if factory is None:
                model_errors[model_name] = "unavailable"; continue
            try:
                estimator = factory(); estimator.fit(X[train_idx], y[train_idx])
                model_probabilities[model_name] = estimator.predict_proba(X[eval_idx])[:, 1]
            except Exception as exc:
                model_errors[model_name] = f"{type(exc).__name__}: {exc}"
        if not model_probabilities:
            return {"status": "INSUFFICIENT_MODELS", "clean_rounds": int(len(clean)), "rounds_analyzed": dev_n,
                    "reason": "no_supported_model_family_fitted", "model_errors": model_errors,
                    "discovery_funnel": self._empty_funnel(dev_n)}
        prob = model_probabilities.get("logistic_regression", next(iter(model_probabilities.values())))
        eval_y = y[eval_idx]
        # Independent sequence and analog evidence use train-only outcomes.
        train_baseline = float(y[train_idx].mean())
        train_seq: dict[tuple[int, str], list[int]] = {}
        for j in train_idx:
            seq = seqs[j]
            for length in SEQUENCE_LENGTHS:
                key = (length, seq[-length:]); acc = train_seq.setdefault(key, [0, 0]); acc[0] += 1; acc[1] += int(y[j])
        supported_tests = sum(n >= 30 for n, _ in train_seq.values())
        stable_patterns = set()
        train_boundaries = np.linspace(0, len(train_idx), 5, dtype=int)
        for (length, pattern), (n, hits) in train_seq.items():
            if n < 30 or wilson(hits, n)["lower"] <= train_baseline:
                continue
            p_adjusted = float(binom.sf(hits - 1, n, train_baseline)) * max(1, supported_tests)
            if p_adjusted > .05: continue
            positive = supported = 0
            for left, right in zip(train_boundaries[:-1], train_boundaries[1:]):
                mask = np.asarray([seqs[train_idx[z]][-length:] == pattern for z in range(left, right)])
                if int(mask.sum()) < 5: continue
                supported += 1
                if float(y[train_idx[left:right]][mask].mean()) > float(y[train_idx[left:right]].mean()): positive += 1
            if supported >= 3 and positive >= 3: stable_patterns.add((length, pattern))
        pscore, ascore, analog_counts, pattern_counts, stable_flags = [], [], [], [], []
        scales = StandardScaler().fit(X[train_idx])
        train_x = scales.transform(X[train_idx]); eval_x = scales.transform(X[eval_idx])
        nn = NearestNeighbors(n_neighbors=min(100, len(train_idx)), algorithm="auto").fit(train_x)
        distances, neighbors = nn.kneighbors(eval_x)
        for local_i, j in enumerate(eval_idx):
            seq = seqs[j]
            # best supported sequence among requested suffix lengths, with a
            # Bonferroni-corrected one-sided binomial test across observed patterns.
            candidates = []
            for length in SEQUENCE_LENGTHS:
                n, hits = train_seq.get((length, seq[-length:]), [0, 0])
                if n >= 30:
                    pval = float(binom.sf(hits - 1, n, train_baseline))
                    candidates.append((hits / n - train_baseline, n, hits, length, pval,
                                       (length, seq[-length:]) in stable_patterns,
                                       wilson(hits, n)["lower"] - train_baseline))
            if candidates:
                best = max(candidates, key=lambda v: v[0]); pscore.append(best[0]); pattern_counts.append(best[1]); stable_flags.append(best[5])
            else: pscore.append(0.0); pattern_counts.append(0); stable_flags.append(False)
            ids = neighbors[local_i]; ds = distances[local_i]
            weights = 1 / np.maximum(ds, 1e-6); outcomes = y[train_idx[ids]]
            rate = float(np.average(outcomes, weights=weights)); ascore.append(rate - train_baseline); analog_counts.append(int(len(ids)))
        pscore, ascore = np.asarray(pscore), np.asarray(ascore)
        evidence = np.column_stack([prob, pscore, ascore])
        # Discovery ranking is an exploratory learned probability ordering. No
        # threshold is selected for promotion; coverage slices are diagnostics.
        score = prob
        base_eval = float(eval_y.mean())
        coverage = {}
        for cov in (.01, .02, .05, .10):
            k = max(1, int(math.ceil(len(score) * cov)))
            chosen = np.zeros(len(score), bool); chosen[np.argsort(score)[-k:]] = True
            coverage[f"{cov:.0%}"] = _metric(eval_y, chosen, base_eval)
        curve = []
        for cov in np.arange(.005, .2001, .005):
            k = max(1, int(math.ceil(len(score) * float(cov))))
            chosen = np.zeros(len(score), bool); chosen[np.argsort(score)[-k:]] = True
            curve.append({"requested_coverage": float(cov), **_metric(eval_y, chosen, base_eval)})
        deciles = []
        for decile in range(10):
            lo, hi = decile / 10, (decile + 1) / 10
            left = int(len(score) * lo); right = int(len(score) * hi)
            order = np.argsort(score); selected = np.zeros(len(score), bool); selected[order[left:right]] = True
            deciles.append({"decile": decile + 1, **_metric(eval_y, selected, base_eval)})
        # Component ablations on the same frozen exploratory OOS slice.
        ablation = {}
        for name, component in (("Pattern only", pscore), ("Analog only", ascore), ("ML only", prob),
                                ("Pattern + ML", pscore + prob), ("Pattern + Analog", pscore + ascore),
                                ("ML + Analog", prob + ascore), ("All", prob + pscore + ascore)):
            k = max(1, int(math.ceil(len(component) * .05))); selected = np.zeros(len(component), bool)
            selected[np.argsort(component)[-k:]] = True
            ablation[name] = _metric(eval_y, selected, base_eval)
        funnel, failed = self._funnel(eval_y, score, pscore, ascore, np.asarray(analog_counts),
                                      np.asarray(pattern_counts), prob, train_baseline, np.asarray(stable_flags))
        train_sequences = np.asarray(seqs, dtype=object)[train_idx]
        seq_report = self._sequence_report(train_seq, train_sequences, y[train_idx], train_baseline)
        v2_snapshot = self.repository.load_application_state("selective_opportunity_v2") or {}
        feature_report = self._feature_report(X[train_idx], y[train_idx], feature_names, v2_snapshot)
        # Sequence-five values are strongly dependent: success count is the
        # numerator used by its smoothed rate, so report correlation/redundancy.
        success = X[eval_idx, 0]; rate = X[eval_idx, 1]
        redundancy = float(np.corrcoef(success, rate)[0, 1]) if len(success) > 2 and np.std(success) and np.std(rate) else None
        prospective = clean.loc[clean.round_index > V1_SOURCE_END_INDEX]
        dynamic_baselines = {f"last_{window}": float(np.mean([rows[i][f"rate_ge_2_1_{window}"] for i in eval_idx]))
                             for window in (100, 250, 500, 1000)}
        model_reports, model_votes = {}, []
        for model_name, model_p in model_probabilities.items():
            k = max(1, int(math.ceil(len(model_p) * .05))); selected = np.zeros(len(model_p), bool)
            selected[np.argsort(model_p)[-k:]] = True
            model_reports[model_name] = _metric(eval_y, selected, base_eval)
            model_votes.append(model_p >= train_baseline)
        agreement_count = np.sum(np.column_stack(model_votes), axis=1) if model_votes else np.zeros(len(eval_y))
        agreed = agreement_count >= max(2, math.ceil(len(model_votes) / 2)) if model_votes else np.zeros(len(eval_y), bool)
        block_curve = []
        for block_no, block in enumerate(np.array_split(np.arange(len(eval_y)), 4), start=1):
            if not len(block): continue
            block_y, block_score = eval_y[block], score[block]
            row = {"block": block_no, "baseline": float(block_y.mean()), "rounds": int(len(block)), "coverage": {}}
            for cov in (.01, .02, .05, .10):
                k = max(1, int(math.ceil(len(block) * cov))); chosen = np.zeros(len(block), bool)
                chosen[np.argsort(block_score)[-k:]] = True
                row["coverage"][f"{cov:.0%}"] = _metric(block_y, chosen, float(block_y.mean()))
            block_curve.append(row)
        return {"status": "EXPLORATORY_ONLY", "clean_rounds": int(len(clean)), "rounds_analyzed": dev_n,
                "eligible_oos_rounds": int(len(eval_y)), "baseline": train_baseline,
                "oos_baseline": base_eval, "discovery_coverage": coverage, "evidence_deciles": deciles,
                "precision_coverage_curve": curve, "score_source": "logistic-regression diagnostic ranking on the two V2 research leads; not a calibrated production opportunity score",
                "discovery_funnel": funnel, "candidate_failure_reasons": failed,
                "dynamic_baselines": dynamic_baselines,
                "performance_by_chronological_block": block_curve,
                "stable_sequence_count_in_training": len(stable_patterns),
                "sequence_engine": seq_report, "analog_engine": {"method": "train-only standardized 2-feature nearest neighbors, inverse-distance weighted",
                    "candidates": len(eval_y), "median_analog_count": int(np.median(analog_counts)) if analog_counts else 0,
                    "stable_analog_groups": 0, "best_oos_lift": ablation["Analog only"]["lift"],
                    "note": "Groups are exploratory; stable group gate is not met."},
                "ablation": ablation, "models_tested": list(model_probabilities), "model_errors": model_errors,
                "best_model": None, "best_ensemble": None, "best_opportunity_policy": None,
                "selection_note": "No model, ensemble, or policy was selected from this shared exploratory evaluation slice.",
                "model_agreement": {"included": False, "individual_top5": model_reports,
                    "top5_agreement": _metric(eval_y, agreed, base_eval),
                    "reason": "Agreement is measured but excluded from the score until independent chronological blocks show added value."},
                "features": feature_report,
                "feature_behavior_by_regime": self._feature_by_regime(rows, sources, eval_idx, eval_y),
                "pattern_sequence_5_analysis": self._feature_association(X[eval_idx, 0], eval_y, base_eval,
                    np.asarray([rows[i]["rate_ge_2_1_1000"] for i in eval_idx])),
                "pattern_sequence_5_rate_analysis": self._feature_association(X[eval_idx, 1], eval_y, base_eval,
                    np.asarray([rows[i]["rate_ge_2_1_1000"] for i in eval_idx])),
                "sequence_5_redundancy_correlation": redundancy,
                "regime_analysis": self._regimes(clean, train_end),
                "prospective_boundary": {"old_v1_test_start_round_index": V1_DIAGNOSTIC_START_INDEX,
                    "prospective_start_after_v1_source_round_index": V1_SOURCE_END_INDEX,
                    "historical_valid_rounds": int(len(clean)), "prospective_valid_rounds_after_boundary": int(len(prospective)),
                    "contiguous_prospective_rounds": self._suffix(prospective),
                "note": "Valid history may contain gaps; continuity is counted from unique identities and consecutive platform order, with clock anomalies reported separately."},
                "research_shadow_ready": False,
                "reason": "No V3 gate has passed prospective confirmation; historical discovery is exploratory and cannot authorize signals."}

    @staticmethod
    def _feature_by_regime(rows, sources, eval_idx, labels):
        groups: dict[int, list[int]] = {}
        for local, idx in enumerate(eval_idx):
            groups.setdefault(int(sources[idx]) // 1000, []).append(local)
        report = []
        for block, positions in sorted(groups.items()):
            if len(positions) < 20: continue
            y = labels[positions]
            row = {"chronological_position_block_1000": block, "support": len(positions),
                   "target_rate": float(y.mean()), "features": {}}
            for feature in ("pattern_sequence_5_successes", "pattern_sequence_5_rate"):
                x = np.asarray([rows[eval_idx[p]][feature] for p in positions], dtype=float)
                corr = float(np.corrcoef(x, y)[0, 1]) if np.std(x) > 0 and np.std(y) > 0 else None
                row["features"][feature] = {"mean": float(np.mean(x)), "correlation_with_target": corr,
                    "distinct_values": int(len(np.unique(x)))}
            report.append(row)
        return {"blocks": report, "note": "Descriptive 1,000-position OOS blocks; not used for feature or threshold selection."}

    @staticmethod
    def _empty_funnel(n): return {"all_rounds": n, "discovery_candidates": 0, "statistical_support": 0,
                                  "stable": 0, "model_supported": 0, "confirmed": 0, "final_opportunities": 0}

    def _funnel(self, y, score, pattern, analog, analog_n, pattern_n, prob, baseline, stable_pattern):
        k = max(1, int(math.ceil(len(y) * .10))) if len(y) else 0
        discovered = np.zeros(len(y), bool)
        if k: discovered[np.argsort(score)[-k:]] = True
        stats = discovered & (pattern > 0) & (analog > 0) & (analog_n >= 50) & (pattern_n >= 30)
        stable = stats & stable_pattern
        model = stable & (prob > baseline)
        confirmed = np.zeros(len(y), bool)  # Requires prospective repeated block evidence.
        final = np.zeros(len(y), bool)      # V3 is explicitly discovery-only.
        funnel = {"all_rounds": int(len(y)), "discovery_candidates": int(discovered.sum()),
                  "statistical_support": int(stats.sum()), "stable": int(stable.sum()),
                  "model_supported": int(model.sum()), "confirmed": 0, "final_opportunities": 0,
                  "failed_gate_counts": {"discovery": int((~discovered).sum()), "statistical_support": int((discovered & ~stats).sum()),
                    "stability": int((stats & ~stable).sum()), "model_support": int((stable & ~model).sum()),
                    "uncertainty_or_prospective_confirmation": int(model.sum()), "final_opportunity_gate": int(len(y))}}
        reasons = {"not_top_10pct_evidence": int((~discovered).sum()),
                   "insufficient_pattern_or_analog_lift_or_support": int((discovered & ~stats).sum()),
                   "insufficient_sequence_support": int((stats & ~stable).sum()),
                   "probability_not_above_training_baseline": int((stable & ~model).sum()),
                   "no_prospective_confirmation": int(model.sum()), "discovery_only_never_final": int(len(y))}
        return funnel, reasons

    def _sequence_report(self, patterns, sequences, y_train, baseline):
        total_tests = sum(1 for (length, _), (n, _) in patterns.items() if n >= 30)
        candidates = []
        for (length, seq), (n, hits) in patterns.items():
            if n < 30: continue
            p = float(binom.sf(hits - 1, n, baseline))
            matched = np.asarray([s[-length:] == seq for s in sequences], dtype=bool)
            boundaries = np.linspace(0, len(y_train), 5, dtype=int)
            block_rates = []; positive_blocks = 0; supported_blocks = 0
            for left, right in zip(boundaries[:-1], boundaries[1:]):
                mask = matched[left:right]
                block_y = y_train[left:right]
                if int(mask.sum()) < 5: continue
                supported_blocks += 1
                support = int(mask.sum()); hits_in_block = int(block_y[mask].sum())
                rate = hits_in_block / support; block_base = float(block_y.mean())
                block_rates.append({"support": support, "successes": hits_in_block, "rate": rate,
                                    "ci95": wilson(hits_in_block, support), "baseline": block_base,
                                    "lift": rate - block_base,
                                    "direction": "POSITIVE" if rate > block_base else "NEGATIVE_OR_FLAT"})
                positive_blocks += int(rate > block_base)
            adjusted = min(1.0, p * max(1, total_tests))
            stable = supported_blocks >= 3 and positive_blocks >= 3 and adjusted <= .05
            candidates.append({"length": length, "pattern": seq, "support": n, "successes": hits,
                "target_rate": hits / n, "baseline": baseline, "lift": hits / n - baseline,
                "ci95": wilson(hits, n), "raw_p_value": p, "bonferroni_p": adjusted,
                "block_stability": {"blocks_with_support": supported_blocks,
                    "positive_lift_blocks": positive_blocks, "rates": block_rates,
                    "classification": "STABLE_IN_TRAINING_ONLY" if stable else "UNSTABLE_OR_INSUFFICIENT"}})
        # Benjamini-Hochberg q-values describe discovery only. Bonferroni
        # remains the predeclared family-wise promotion veto.
        ordered_p = sorted(enumerate(candidates), key=lambda item: item[1]["raw_p_value"])
        running_q = 1.0
        for reverse_rank in range(len(ordered_p) - 1, -1, -1):
            original_index, candidate = ordered_p[reverse_rank]
            rank = reverse_rank + 1
            running_q = min(running_q, candidate["raw_p_value"] * len(ordered_p) / rank, 1.0)
            candidates[original_index]["fdr_q_value"] = float(running_q)
        candidates.sort(key=lambda x: (x["bonferroni_p"] <= .05, x["lift"], x["support"]), reverse=True)
        return {"lengths": list(SEQUENCE_LENGTHS), "states": {"A": "<1.20", "B": "1.20-1.49", "C": "1.50-1.99",
                "D": "2.00-2.99", "E": "3.00-4.99", "F": ">=5.00"},
                "patterns_researched": len(patterns), "patterns_with_support_30": total_tests,
            "multiple_testing": "FDR q-values are descriptive discovery control; Bonferroni family-wise correction remains the promotion veto, evaluated on discovery split only",
            "stable_patterns": sum(x["block_stability"]["classification"] == "STABLE_IN_TRAINING_ONLY" for x in candidates),
            "best_stable_pattern": next((x for x in candidates if x["block_stability"]["classification"] == "STABLE_IN_TRAINING_ONLY"), None),
            "top_research_patterns": candidates[:25],
            "note": "Stability labels use training blocks only and do not establish prospective or independent OOS stability."}

    def _feature_report(self, X, y, names, v2_snapshot):
        v2_folds = v2_snapshot.get("outer_blocks", [])
        v2_by_name: dict[str, list[dict[str, Any]]] = {}
        for block in v2_folds:
            for row in (block.get("feature_stability") or {}).get("rows", []):
                v2_by_name.setdefault(row.get("feature"), []).append(row)
        out = []
        for name in sorted(v2_by_name):
            rows = v2_by_name[name]
            v2_retained = sum(bool(row.get("retained")) for row in rows)
            v2_effects = [float(effect) for row in rows for effect in row.get("block_effects", [])
                          if effect is not None and np.isfinite(effect)]
            direction_consistency = (max(sum(x > 0 for x in v2_effects), sum(x < 0 for x in v2_effects)) / len(v2_effects)
                                     if v2_effects else None)
            classification = ("STABLE" if v2_retained == len(v2_folds) and len(v2_folds) >= 3 and direction_consistency == 1.0
                              else "CONDITIONALLY_STABLE" if v2_retained > 0 or (direction_consistency is not None and direction_consistency >= .75)
                              else "INSUFFICIENT_EVIDENCE" if not v2_effects else "UNSTABLE")
            out.append({"feature": name, "classification": classification,
                "v2_retained_folds": v2_retained, "v2_folds": len(v2_folds),
                "sample_count": max((int(block.get("train_rounds", 0)) for block in v2_folds), default=0),
                "target_association_by_chronological_block": v2_effects,
                "direction_consistency": direction_consistency,
                "reason": "V2 frozen feature stability diagnostics, reclassified against fold retention and direction consistency"})
        if not out:
            for col, name in enumerate(names):
                effects = []
                for block in np.array_split(np.arange(len(y)), 4):
                    if len(block) and len(np.unique(y[block])) > 1:
                        effects.append(float(np.corrcoef(X[block, col], y[block])[0, 1]))
                consistent = sum(x > 0 for x in effects) / len(effects) if effects else None
                klass = "INSUFFICIENT_EVIDENCE" if len(effects) < 3 else ("STABLE" if consistent == 1.0 and abs(float(np.mean(effects))) >= .03 else ("CONDITIONALLY_STABLE" if consistent is not None and consistent >= .75 else "UNSTABLE"))
                out.append({"feature": name, "classification": klass, "sample_count": len(y),
                            "target_association_by_chronological_block": effects, "direction_consistency": consistent})
        eligible_names = {name for name, rows in v2_by_name.items()
                          if len(v2_folds) >= 3 and sum(bool(row.get("retained")) for row in rows) == len(v2_folds)}
        return {"retained": [row for row in out if row["feature"] in eligible_names],
                "classified": out,
                "rejected": [row for row in out if row["classification"] in {"UNSTABLE", "INSUFFICIENT_EVIDENCE"}],
                "not_model_eligible": [row["feature"] for row in out if row["feature"] not in eligible_names],
                "counts": {key: sum(row["classification"] == key for row in out)
                           for key in ("STABLE", "CONDITIONALLY_STABLE", "UNSTABLE", "INSUFFICIENT_EVIDENCE")}}

    def _feature_association(self, values, y, baseline, contemporaneous_baseline=None):
        groups = []
        finite = np.asarray(values)[np.isfinite(values)]
        exact = (len(np.unique(finite)) <= 15 and len(finite) and np.all(np.equal(finite, np.floor(finite)))
                 and np.min(finite) >= 0)
        if exact:
            bins = [(float(v), float(v), values == v) for v in sorted(np.unique(finite))]
        else:
            quantiles = np.unique(np.quantile(finite, [0, .25, .5, .75, 1])) if len(finite) else []
            bins = [(float(a), float(b), (values >= a) & (values <= b if b == quantiles[-1] else values < b))
                    for a, b in zip(quantiles[:-1], quantiles[1:])]
        for a, b, mask in bins:
            n = int(mask.sum()); hits = int(y[mask].sum()) if n else 0
            local_baseline = float(np.mean(contemporaneous_baseline[mask])) if n and contemporaneous_baseline is not None else baseline
            groups.append({"value" if exact else "range": float(a) if exact else [a, b], "support": n, "successes": hits,
                           "rate": hits / n if n else None, "contemporaneous_baseline": local_baseline,
                           "lift": hits / n - local_baseline if n else None,
                           "ci95": wilson(hits, n), "lift_ci95": _ci_lift(hits, n, local_baseline),
                           "evidence_state": "INSUFFICIENT_SUPPORT" if n < 30 else "DESCRIPTIVE_ONLY",
                           "promotion_eligible": False})
        chronological_blocks = []
        for block_no, indexes in enumerate(np.array_split(np.arange(len(y)), 4), 1):
            if not len(indexes):
                continue
            block_values = np.asarray(values)[indexes]
            block_y = np.asarray(y)[indexes]
            block_base = float(np.mean(block_y))
            q75 = float(np.quantile(block_values, .75))
            high = block_values >= q75
            n = int(high.sum()); hits = int(block_y[high].sum()) if n else 0
            rate = hits / n if n else None
            chronological_blocks.append({"block": block_no, "support": int(len(indexes)),
                "baseline": block_base, "high_value_cutoff_q75": q75,
                "feature_support": n, "successes": hits, "target_rate": rate,
                "lift": rate - block_base if rate is not None else None,
                "ci95": wilson(hits, n),
                "evidence_state": "INSUFFICIENT_SUPPORT" if n < 30 else "DESCRIPTIVE_ONLY",
                "direction": "POSITIVE" if rate is not None and rate > block_base else
                    "NEGATIVE_OR_FLAT" if rate is not None else "INSUFFICIENT"})
        return {"sample_count": int(len(y)), "baseline": baseline, "quantile_ranges": groups,
                "chronological_blocks": chronological_blocks,
                "leakage_check": "features use only rounds through the source round; following target is excluded"}

    @staticmethod
    def _regimes(clean, train_end):
        rates = []
        values = clean.multiplier.to_numpy(float)
        for start in range(0, min(len(values), 1000 * 20), 1000):
            x = values[start:start + 1000]
            if len(x) >= 100: rates.append({"start_position": start, "rounds": len(x),
                "rate_ge_2_1": float(np.mean(x >= TARGET)), "median": float(np.median(x)),
                "q90": float(np.quantile(x, .9)), "entropy": float(-(lambda p: (p * np.log2(p)).sum())(np.unique([_state(v) for v in x], return_counts=True)[1] / len(x)))})
        return {"method": "chronological 1,000-round descriptive blocks", "blocks": rates,
                "predictive_claim": False, "change_point_status": "DESCRIPTIVE_ONLY"}

    @staticmethod
    def _suffix(frame):
        if frame.empty: return 0
        n = 1
        for i in range(len(frame) - 1, 0, -1):
            if (int(frame.iloc[i]["round_index"]) != int(frame.iloc[i - 1]["round_index"]) + 1
                    or str(frame.iloc[i].round_id) == str(frame.iloc[i - 1].round_id)): break
            n += 1
        return n

    @staticmethod
    def _continuity_audit(frame):
        counts = {"genuine_round_identity_gaps": 0, "timestamp_gap_over_120s": 0,
                  "timestamp_order_reversal": 0, "timestamp_missing_or_invalid": 0}
        latest = None
        for i in range(1, len(frame)):
            left, right = frame.iloc[i - 1], frame.iloc[i]
            delta_index = int(right.round_index) - int(left.round_index)
            delta_seconds = (right.timestamp_dt - left.timestamp_dt).total_seconds()
            reason = None
            if delta_index != 1 or str(right.round_id) == str(left.round_id):
                reason = "genuine_round_identity_gaps"; counts[reason] += 1
            if pd.isna(left.timestamp_dt) or pd.isna(right.timestamp_dt):
                counts["timestamp_missing_or_invalid"] += 1
            elif delta_seconds > 120:
                counts["timestamp_gap_over_120s"] += 1
            elif delta_seconds < 0:
                counts["timestamp_order_reversal"] += 1
            if reason:
                latest = {"reason": reason, "previous_round_index": int(left.round_index),
                          "next_round_index": int(right.round_index), "elapsed_seconds": round(delta_seconds, 3)}
        return {"counts": counts, "latest_break": latest}

    def observe(self, rounds: pd.DataFrame, collector_latest_round_id: str | None = None):
        clean, _ = validated_rounds(rounds.copy())
        segments = _contiguous(clean)
        if not segments: return None
        last_start, last_end = segments[-1]
        clean = clean.iloc[last_start:last_end].reset_index(drop=True)
        if collector_latest_round_id and str(clean.iloc[-1].round_id) != str(collector_latest_round_id): return None
        if len(clean) < 102:
            source = clean.iloc[-1]
            now = datetime.now(timezone.utc).isoformat()
            assessment = {"observation_id": hashlib.sha256(f"{VERSION}|{source.round_id}".encode()).hexdigest(),
                "model_version": VERSION, "source_round_id": str(source.round_id),
                "source_round_index": int(source.round_index), "created_at": now, "observed_at": now,
                "target_threshold": TARGET, "status": "PENDING", "mode": "RESEARCH_DISCOVERY_ONLY",
                "candidate_discovered": False, "candidate_state": "NO_DISCOVERY_CANDIDATE",
                "pattern_score": None, "analog_score": None, "model_score": None, "baseline_lift": None,
                "support_count": 0, "confidence_interval": None, "feature_stability": "NOT_ESTABLISHED",
                "regime_status": "INSUFFICIENT_CONTIGUOUS_HISTORY", "model_agreement": None,
                "uncertainty": "HIGH", "failed_gate": "STATISTICAL_SUPPORT",
                "failed_reason": "fewer_than_100_contiguous_prior_rounds_for_stable_feature assessment",
                "evidence": {"history_segment_rounds": int(len(clean))}, "signal": False, "abstain": True,
                "target_round_id": None, "outcome_2_1x": None, "actual_multiplier": None, "usable": False}
            return self.repository.save_opportunity_observation(assessment)
        source = clean.iloc[-1]
        values = clean.multiplier.to_numpy(float)
        baseline = float(np.mean(values[:-1] >= TARGET))
        seq = "".join(_state(v) for v in values[-5:])
        pattern_counts: dict[tuple[str, str], list[int]] = {}
        prior_hits = prior_count = 0
        for prior_source in range(100, len(values) - 1):
            history = values[:prior_source + 1]
            label = int(values[prior_source + 1] >= TARGET)
            buckets = _bucket(history[-1000:])
            for length in (2, 3, 5):
                key = "|".join(map(str, buckets[-length:]))
                pair = pattern_counts.setdefault((f"sequence_{length}", key), [0, 0])
                pair[0] += 1; pair[1] += label
            prior_hits += label; prior_count += 1
        buckets = _bucket(values[-1000:])
        key = "|".join(map(str, buckets[-5:]))
        count, hits = pattern_counts.get(("sequence_5", key), [0, 0])
        past_rate = (prior_hits + 10) / (prior_count + 20)
        v2_features = {"pattern_sequence_5_count": count, "pattern_sequence_5_successes": hits,
            "pattern_sequence_5_rate": (hits + 10 * past_rate) / (count + 10),
            **{f"rate_ge_2_1_{window}": float(np.mean(values[-window:] >= TARGET))
               for window in (100, 250, 500, 1000)}}
        sequence_stats = {}
        # Past-only exact sequence support from every previously observable source.
        for length in SEQUENCE_LENGTHS:
            n = hits = 0
            for i in range(100, len(values) - 1):
                if "".join(_state(v) for v in values[i - length + 1:i + 1]) == seq[-length:]:
                    n += 1; hits += int(values[i + 1] >= TARGET)
            sequence_stats[str(length)] = {"count": n, "successes": hits,
                "rate": hits / n if n else None, "lift": hits / n - baseline if n else None,
                "ci95": wilson(hits, n)}
        features = {**v2_features,
                    "sequence_5_count": sequence_stats["5"]["count"], "sequence_5": seq}
        # Discovery is explicitly permissive and descriptive. No confirmed or
        # final opportunity can be emitted by prospective V3 at this stage.
        candidate = any(v["count"] >= 30 and (v["lift"] or 0) > 0 for v in sequence_stats.values())
        failed_gate = "STATISTICAL_SUPPORT" if not candidate else "STABILITY_CHECK"
        assessment = {"observation_id": hashlib.sha256(f"{VERSION}|{source.round_id}".encode()).hexdigest(),
            "model_version": VERSION, "source_round_id": str(source.round_id), "source_round_index": int(source.round_index),
            "created_at": datetime.now(timezone.utc).isoformat(), "observed_at": datetime.now(timezone.utc).isoformat(),
            "target_threshold": TARGET, "status": "PENDING", "mode": "RESEARCH_DISCOVERY_ONLY",
            "candidate_discovered": bool(candidate), "candidate_state": "DISCOVERY_CANDIDATE" if candidate else "NO_DISCOVERY_CANDIDATE",
            "pattern_score": max([v["lift"] or 0 for v in sequence_stats.values()] or [0]),
            "analog_score": None, "model_score": None, "baseline_lift": max([v["lift"] or 0 for v in sequence_stats.values()] or [0]),
            "support_count": max([v["count"] for v in sequence_stats.values()] or [0]), "confidence_interval": None,
            "feature_stability": "NOT_ESTABLISHED", "regime_status": "DESCRIPTIVE_ONLY", "model_agreement": None,
            "uncertainty": "HIGH", "failed_gate": failed_gate,
            "failed_reason": "prospective blocks have not established stability, model support, or uncertainty bounds",
            "evidence": {"features": features, "sequence_evidence": sequence_stats, "historical_baseline": baseline},
            "signal": False, "abstain": True, "target_round_id": None, "outcome_2_1x": None,
            "actual_multiplier": None, "usable": False}
        return self.repository.save_opportunity_observation(assessment)
