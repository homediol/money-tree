"""
LSTM sequence classifier — PyTorch
==================================
"""
from __future__ import annotations

from typing import Any, Dict, List

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import DataLoader, TensorDataset

from src.models.base import BaseModel


class _LSTMNet(nn.Module):
    def __init__(self, in_dim: int, n_classes: int, hidden: int, layers: int, dropout: float, bidirectional: bool) -> None:
        super().__init__()
        self.lstm = nn.LSTM(
            input_size=in_dim,
            hidden_size=hidden,
            num_layers=layers,
            batch_first=True,
            dropout=dropout if layers > 1 else 0.0,
            bidirectional=bidirectional,
        )
        out_dim = hidden * (2 if bidirectional else 1)
        self.head = nn.Sequential(
            nn.LayerNorm(out_dim),
            nn.Dropout(dropout),
            nn.Linear(out_dim, 128),
            nn.GELU(),
            nn.Dropout(dropout),
            nn.Linear(128, n_classes),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out, _ = self.lstm(x)
        last = out[:, -1, :]
        return self.head(last)


class LSTMModel(BaseModel):
    name = "lstm"
    family = "sequence"
    needs_sequence = True

    def __init__(self, **hp: Any) -> None:
        defaults = dict(
            hidden=128,
            num_layers=2,
            dropout=0.3,
            bidirectional=True,
            lr=1e-3,
            weight_decay=1e-4,
            batch_size=64,
            max_epochs=80,
            patience=10,
            device="cpu",
        )
        defaults.update(hp)
        super().__init__(**defaults)
        self.device = torch.device(defaults["device"] if torch.cuda.is_available() and defaults["device"] == "cuda" else "cpu")
        self._input_dim: int = 0

    def _build(self):
        return _LSTMNet(
            in_dim=self._input_dim,
            n_classes=self.n_classes,
            hidden=self.hyperparams["hidden"],
            layers=self.hyperparams["num_layers"],
            dropout=self.hyperparams["dropout"],
            bidirectional=self.hyperparams["bidirectional"],
        ).to(self.device)

    def _fit(self, X, y, **kwargs) -> Dict[str, List[float]]:
        # X: (N, T, F)
        self._input_dim = X.shape[2]
        if self.model is None:
            self.model = self._build()
        X_t = torch.tensor(X, dtype=torch.float32, device=self.device)
        y_t = torch.tensor(y, dtype=torch.long, device=self.device)
        ds = TensorDataset(X_t, y_t)
        dl = DataLoader(ds, batch_size=self.hyperparams["batch_size"], shuffle=True)

        opt = torch.optim.AdamW(self.model.parameters(), lr=self.hyperparams["lr"], weight_decay=self.hyperparams["weight_decay"])
        sched = torch.optim.lr_scheduler.CosineAnnealingLR(opt, T_max=self.hyperparams["max_epochs"])
        loss_fn = nn.CrossEntropyLoss()

        history: Dict[str, List[float]] = {"train_loss": [], "train_accuracy": []}
        best_loss, best_state, patience = float("inf"), None, 0

        for epoch in range(self.hyperparams["max_epochs"]):
            self.model.train()
            total_loss, correct, n = 0.0, 0, 0
            for xb, yb in dl:
                opt.zero_grad()
                logits = self.model(xb)
                loss = loss_fn(logits, yb)
                loss.backward()
                torch.nn.utils.clip_grad_norm_(self.model.parameters(), 1.0)
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

    def _coerce_X(self, X):
        import numpy as _np
        if hasattr(X, "values"):
            arr = _np.asarray(X.values, dtype=_np.float32)
        else:
            arr = _np.asarray(X, dtype=_np.float32)
        if arr.ndim == 2:
            arr = arr.reshape(1, arr.shape[0], arr.shape[1])
        return arr

