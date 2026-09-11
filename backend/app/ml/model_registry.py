from __future__ import annotations

from app.ml.trainer import ModelTrainer, TrainingResult


class ModelRegistry:
    def __init__(self, target: float = 2.0):
        self.trainer = ModelTrainer(target)
        self.latest: TrainingResult | None = None

    def train(self, rounds):
        self.latest = self.trainer.train_validate(rounds)
        return self.latest

    def performance(self) -> dict:
        if not self.latest:
            return {"status": "MODEL NOT TRAINED", "validated": False, "models": {}, "baselines": {}}
        return self.latest.model_dump()

