"""
training/retrain.py
====================
Incremental retraining trigger.

Uses IncrementalUpdater from dataset_loader for state management.
Triggers every RETRAIN_INTERVAL new rounds (default 5,000).

Run as a one-shot check:
    cd backend && python -m training.retrain
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

RETRAIN_INTERVAL    = 5_000
MIN_ROUNDS_TO_TRAIN = 500

log = logging.getLogger(__name__)


def should_retrain() -> bool:
    from training.dataset_loader import IncrementalUpdater
    from utils import load_round_history
    updater = IncrementalUpdater(
        retrain_interval=RETRAIN_INTERVAL,
        min_rounds=MIN_ROUNDS_TO_TRAIN,
    )
    return updater.should_retrain(len(load_round_history()))


def retrain_if_needed(epochs: int = 100, batch_size: int = 256) -> Optional[dict]:
    """
    Returns training metrics if a retrain was triggered, else None.

    Uses actual imports (not find_spec) so it works correctly in both
    venv and system Python contexts — if the library isn't importable
    the ImportError is caught and logged cleanly.
    """
    try:
        from utils import load_round_history
        from training.dataset_loader import IncrementalUpdater

        # Actual imports — will raise ImportError if not installed
        import numpy   # noqa: F401
        import sklearn  # noqa: F401

        current = len(load_round_history())
        updater = IncrementalUpdater(
            retrain_interval=RETRAIN_INTERVAL,
            min_rounds=MIN_ROUNDS_TO_TRAIN,
        )
        return updater.retrain_if_needed(current, epochs=epochs, batch_size=batch_size)

    except ImportError as exc:
        log.warning("Auto-retrain skipped (missing dependency): %s", exc)
        return None
    except Exception as exc:
        log.error("Auto-retrain failed: %s", exc)
        return None


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    result = retrain_if_needed()
    if result:
        print(json.dumps(result, indent=2))
    else:
        print("No retrain needed or threshold not reached.")
