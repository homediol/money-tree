"""
Feed-forward Neural Network (MLP) — PyTorch
===========================================
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, TensorDataset

from src.models.base import BaseModel


class _MLPNet(nn.Module):
    def __init__(self, in_dim: int, n_classes: int, hidden: List[int], dropout: float) -> None:
        super().__init__()
        layers: List[nn.Module] = []
        prev = in_dim
        for h in hidden:
            layers += [nn.Linear(prev, h), nn.BatchNorm1d(h), nn.GELU(), nn.Dropout(dropout)]
            prev = h
        layers.append(nn.Linear(prev, n_classes))
        self.net = nn.Sequential(*layers)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


class NeuralNetworkModel(BaseModel):
    name = "neural_network"
    family = "deep_learning"

    def __init__(self, **hp: Any) -> None:
        defaults = dict(
            hidden=[256, 128, 64],
            dropout=0.3,
            lr=1e-3,
            weight_decay=1e-4,
            batch_size=64,
            max_epochs=100,
            patience=12,
            device="cpu",
        )
        defaults.update(hp)
        super().__init__(**defaults)
        self.device = torch.device(defaults["device"] if torch.cuda.is_available() and defaults["device"] == "cuda" else "cpu")
        self._input_dim: int = 0

    def _build(self):
        return _MLPNet(
            in_dim=self._input_dim,
            n_classes=self.n_classes,
            hidden=self.hyperparams["hidden"],
            dropout=self.hyperparams["dropout"],
        ).to(self.device)

    def _fit(self, X, y, **kwargs) -> Dict[str, List[float]]:
        self._input_dim = X.shape[1]
        if self.model is None:
            self.model = self._build()
        X_t = torch.tensor(X, dtype=torch.float32, device=self.device)
        y_t = torch.tensor(y, dtype=torch.long, device=self.device)
        ds = TensorDataset(X_t, y_t)
        dl = DataLoader(ds, batch_size=self.hyperparams["batch_size"], shuffle=True, drop_last=False)

        opt = torch.optim.AdamW(self.model.parameters(), lr=self.hyperparams["lr"], weight_decay=self.hyperparams["weight_decay"])
        sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=self.hyperparams["max_epochs"])
        loss_fn = nn.CrossEntropyLoss()

        history: Dict[str, List[float]] = {"train_loss": [], "train_accuracy": []}
        best_loss = float("inf")
        best_state = None
        patience = 0

        for epoch in range(self.hyperparams["max_epochs"]):
            self.model.train()
            total_loss, correct, n = 0.0, 0, 0
            for xb, yb in dl:
                opt.zero_grad()
                logits = self.model(xb)
                loss = loss_fn(logits, yb)
                loss.backward()
                opt.step()
                total_loss += float(loss.item()) * len(xb)
                correct += int((logits.argmax(1) == yb).sum().item())
                n += len(xb)
            sched.step()
            avg_loss = total_loss / max(n, 1)
            acc = correct / max(n, 1)
            history["train_loss"].append(avg_loss)
            history["train_accuracy"].append(acc)
            if avg_loss < best_loss - 1e-5:
                best_loss = avg_loss
                best_state = {k: v.detach().cpu().clone() for k, v in self.model.state_dict().items()}
                patience = 0
            else:
                patience += 1
                if patience >= self.hyperparams["patience"]:
                    break
        if best_state is not None:
            self.model.load_state_dict(best_state)
        return history

    def _predict_proba(self, X) -> np.ndarray:
        self.model.eval()
        with torch.no_grad():
            xt = torch.tensor(X, dtype=torch.float32, device=self.device)
            logits = self.model(xt)
            proba = F.softmax(logits, dim=-1).cpu().numpy()
        return proba

