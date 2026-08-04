import logging
from typing import Dict

from utils import load_round_history, read_json, utc_now, write_json, METADATA_PATH, DECISIONS_PATH


class TrainingService:
    def __init__(self) -> None:
        self.logger = logging.getLogger(self.__class__.__name__)
        # Use V2 predictor — the same one used by /predict endpoint
        from prediction.predictor import get_predictor
        self.predictor = get_predictor()

    def _get_multipliers(self):
        """Load multipliers from roundhistory.json."""
        history = load_round_history()
        return [r["multiplier"] for r in history], len(history)

    def train(self, epochs: int = 100, force: bool = True) -> Dict[str, object]:
        """
        Train using the V2 pipeline (feature engineering + LSTM + scaler).
        Falls back to the statistical fallback if TF/sklearn are unavailable.
        """
        multipliers, count = self._get_multipliers()
        if not multipliers:
            raise ValueError("No round data available for training.")

        try:
            from training.train_model import train as _train_v2
            metrics = _train_v2(epochs=epochs)
        except Exception as exc:
            self.logger.warning(
                "V2 training failed (%s) — falling back to statistical model.", exc
            )
            # Rebuild the statistical sequence model using the current round data
            from prediction.statistical_predictor import get_statistical_predictor
            import prediction.statistical_predictor as _sp
            _sp._model = None  # force full refit
            get_statistical_predictor(multipliers)
            metrics = {
                "engine": "statistical_ensemble",
                "samples": len(multipliers),
                "validation_accuracy": 0.0,
                "train_accuracy": 0.0,
            }

        # Reset predictor so it reloads the freshly trained model on next predict
        self.predictor._loaded = False
        self.predictor._model = None
        self.predictor._scaler = None

        metadata = {
            "last_trained_at": utc_now(),
            "rounds_seen": count,
            "epochs": epochs,
            **metrics,
        }
        write_json(METADATA_PATH, metadata)
        self.logger.info(
            "Model trained — engine=%s  samples=%s",
            metrics.get("engine"),
            metrics.get("samples"),
        )
        return metadata

    def should_retrain(self, min_new_rounds: int = 25) -> bool:
        metadata = read_json(METADATA_PATH, {})
        _, count = self._get_multipliers()
        if not metadata:
            return True
        return count - int(metadata.get("rounds_seen", 0)) >= min_new_rounds

    def auto_retrain_if_needed(self) -> Dict[str, object]:
        if self.should_retrain():
            # Run training in a background thread so it never blocks HTTP requests
            import threading as _t
            _t.Thread(
                target=self.train,
                kwargs={"epochs": 20},
                daemon=True,
                name="auto-retrain",
            ).start()
            return {"retrained": "started_background", "metadata": read_json(METADATA_PATH, {})}
        return {"retrained": False, "metadata": read_json(METADATA_PATH, {})}

    def backfill_actual_results(self) -> int:
        """
        Match stored decisions against completed rounds and fill in
        actual_multiplier + correct fields so accuracy can be measured.
        Returns number of decisions updated.
        """
        from utils import multiplier_to_category

        decisions = read_json(DECISIONS_PATH, [])
        if not isinstance(decisions, list) or not decisions:
            return 0

        rounds = load_round_history()
        rounds_by_id = {r["round_id"]: r for r in rounds}
        updated = 0

        for d in decisions:
            if d.get("actual_multiplier") is not None:
                continue
            # Support both new format (for_round) and legacy format (last_round_id + 1)
            for_round = d.get("for_round")
            if for_round is None:
                rid = d.get("last_round_id")
                if rid is None:
                    continue
                for_round = rid + 1
            if for_round in rounds_by_id:
                actual = rounds_by_id[for_round]["multiplier"]
                actual_cat = multiplier_to_category(actual)
                d["actual_multiplier"]  = actual
                d["actual_category"]    = actual_cat
                d["actual_round_id"]    = for_round
                d["actual_round_ts"]    = rounds_by_id[for_round].get("timestamp")
                d["correct"]            = actual_cat == d.get("prediction")
                updated += 1

                # Feed outcome back to calibration + guard pipeline
                try:
                    from prediction.risk_management import get_skip_guard, get_vh_guard
                    from prediction.calibration_engine import get_calibration_engine
                    from prediction.confidence_calibrator import get_confidence_calibrator
                    from prediction.momentum_streak import get_momentum_engine
                    get_skip_guard().record_outcome(
                        action_taken=d.get("action", "BET"),
                        was_correct=bool(d["correct"]),
                    )
                    get_vh_guard().record_outcome(
                        prediction=d.get("prediction", ""),
                        actual_multiplier=float(actual),
                        was_correct=bool(d["correct"]),
                    )
                    get_calibration_engine().record_outcome(d)
                    get_confidence_calibrator().record_outcome(
                        raw_confidence=float(d.get("raw_confidence", d.get("confidence", 0))),
                        was_correct=bool(d["correct"]),
                    )
                    streak_info = d.get("streak", {})
                    _trend  = str(streak_info.get("trend", streak_info.get("effective_trend", "NEUTRAL"))).upper()
                    _regime = str(d.get("regime", "medium"))
                    get_momentum_engine().record_outcome(_trend, _regime, bool(d["correct"]))
                    # Risk-tier validator feedback
                    from prediction.risk_tier_validator import get_risk_tier_validator
                    get_risk_tier_validator().record_outcome(
                        prediction=d.get("prediction", ""),
                        actual_multiplier=float(actual),
                        cashout=float(d.get("recommended_cashout", 2.0)),
                        was_correct=bool(d["correct"]),
                    )
                except Exception:
                    pass

        if updated:
            write_json(DECISIONS_PATH, decisions[-250:])
        return updated
