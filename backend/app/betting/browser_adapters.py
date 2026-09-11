"""Executors that translate decisions into game actions.

Two backends implement the same ``PlacementBackend`` protocol:

- ``RealBrowserBackend`` — talks to the live browser through the read-only
  client. In Part 1 real DOM bet placement is *not implemented*: an attempt
  raises ``PlacementUnavailable`` with reason ``placement_driver_not_implemented``.
  The session layer converts that into an honest DEFERRED ledger entry, never
  a fake "placed".
- ``SimulationBackend`` — a deterministic scripted environment used for
  demo/testing. Every entry it produces carries ``simulated: true`` and its
  ledger/events never claim a real placement happened.

The module intentionally contains no prediction logic.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.betting.selectors import GameSnapshot

log = logging.getLogger("betting.adapters")


class PlacementUnavailable(Exception):
    """The executor could not place a bet.

    ``reason`` uses DecisionRejectReason-style slugs so the session layer can
    map it onto a ledger status without string matching.
    """

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason
        self.message = message


@dataclass
class PlacementOutcome:
    ok: bool
    simulated: bool
    detail: str
    round_label: str | None = None
    crash_point: float | None = None
    placed_at: float = field(default_factory=time.time)

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "simulated": self.simulated,
            "detail": self.detail,
            "round_label": self.round_label,
            "crash_point": self.crash_point,
            "placed_at": self.placed_at,
        }


class PlacementBackend(Protocol):
    simulated: bool

    def name(self) -> str: ...
    def describe(self) -> dict: ...

    async def readiness(self) -> dict:
        """Observed status; never mutates the page."""
        ...

    async def snapshot(self) -> dict:
        """Full read-only snapshot of the game plane."""
        ...

    async def ensure_ready(self) -> None:
        """Raise PlacementUnavailable when the environment cannot bet."""
        ...

    async def place(self, *, amount_bif: int, target_multiplier: float,
                    bet_slot: int, decision_id: str) -> PlacementOutcome:
        """Place/queue one bet. Real backend raises PlacementUnavailable."""
        ...

    async def collect_resolved(self) -> list[dict]:
        """Return bets resolved since the last call.

        Each dict:
          decision_id, amount_bif, target_multiplier, bet_slot,
          crash_point, won, pnl_bif, round_label, resolved_at
        """
        ...


# ── Simulation scenario ─────────────────────────────────────────────────────
# Deterministic crash points per round index. Tests rely on these exact
# values: index 0 (1.21) and index 3 (1.60) crash below 2.0; index 6 (1.05)
# is a near-instant crash.
SIM_CRASH_SEQUENCE = [1.21, 3.10, 5.75, 1.60, 2.44, 8.20, 1.05, 12.00, 2.02, 1.97]


class SimulationBackend:
    """Deterministic in-process demo of the game loop.

    - ``readiness()``/``snapshot()`` mirror the real client's shapes so the
      session and UI code paths are identical.
    - Rounds open and close on a wall-clock cadence (``round_every_s``).
      ``place()`` during an open round joins that round; a placement between
      rounds joins the next round to open. Resolution is delivered through
      ``collect_resolved()`` with the round's scripted crash point.
    - ``initial_balance`` shrinks/grows with resolved bets so the demo stays
      honest (a loss actually reduces the demo balance).
    """

    simulated = True
    name_label = "simulation"

    def __init__(self, *, round_every_s: float = 6.0, initial_balance: float = 2000.0,
                 crash_sequence: list[float] | None = None):
        self.round_every_s = max(0.05, round_every_s)
        self.initial_balance = initial_balance
        self.crash_sequence = list(crash_sequence or SIM_CRASH_SEQUENCE)
        self._balance = float(initial_balance)
        self._index = 0                 # next round index to open
        self._current: dict | None = None  # {index, crash, open_until}
        self._queue: list[dict] = []    # placed bets awaiting their round close
        self._resolved: list[dict] = []
        self._payouts: list[float] = []
        self._last_tick = time.monotonic()
        self._next_open = time.monotonic()

    # ── identity ─────────────────────────────────────────────────────────────
    def name(self) -> str:
        return self.name_label

    def describe(self) -> dict:
        return {
            "backend": self.name(),
            "simulated": True,
            "round_every_s": self.round_every_s,
            "crash_sequence": self.crash_sequence[:6],
            "initial_balance_bif": self.initial_balance,
        }

    # ── bookkeeping helpers (exposed for tests) ──────────────────────────────
    def _open_next_round(self) -> None:
        crash = self.crash_sequence[self._index % len(self.crash_sequence)]
        self._current = {
            "index": self._index,
            "crash": crash,
            "open_until": time.monotonic() + self.round_every_s,
        }
        self._index += 1
        self._next_open = time.monotonic() + self.round_every_s

    def _close_current(self) -> None:
        cur = self._current
        crash = cur["crash"]
        label = f"SIM-R{cur['index']}"
        # Resolve every bet that joined this round.
        for bet in self._queue:
            won = bet["target_multiplier"] < crash
            if won:
                pnl = bet["amount_bif"] * (bet["target_multiplier"] - 1)
                self._balance += pnl
            else:
                pnl = -bet["amount_bif"]
                self._balance += pnl
            self._resolved.append({
                "decision_id": bet["decision_id"],
                "amount_bif": bet["amount_bif"],
                "target_multiplier": bet["target_multiplier"],
                "bet_slot": bet["bet_slot"],
                "crash_point": crash,
                "won": won,
                "pnl_bif": round(pnl, 2),
                "round_label": label,
                "resolved_at": time.time(),
            })
        self._queue.clear()
        # Crash history newest-first, matching .payouts-block .payout order.
        self._payouts.insert(0, crash)
        self._current = None

    def _advance_if_due(self) -> None:
        now = time.monotonic()
        if self._current is None and now >= self._next_open:
            self._open_next_round()
        if self._current is not None and now >= self._current["open_until"]:
            self._close_current()
            self._next_open = now + self.round_every_s
        self._last_tick = now

    def crash_payouts(self) -> list[float]:
        self._advance_if_due()
        return list(self._payouts)

    def current_balance(self) -> float:
        return self._balance

    def pending_count(self) -> int:
        return len(self._queue)

    # ── PlacementBackend interface ───────────────────────────────────────────
    async def readiness(self) -> dict:
        self._advance_if_due()
        return {
            "connected": True,
            "stage": "ready",
            "page_found": True,
            "game_found": True,
            "balance_text": f"{self._balance:.0f}BIF",
            "aviator_next_reachable": True,
            "simulated": True,
            "error": None,
        }

    async def snapshot(self) -> dict:
        self._advance_if_due()
        snap = GameSnapshot(
            ok=True,
            payouts=self.crash_payouts(),
            bets_visible=self.pending_count(),
            balance_text=f"{self._balance:.0f}BIF",
            balance=self._balance,
            bet_panel=True,
            stake_inputs=[{"slot": 0, "visible": True, "value": ""},
                          {"slot": 1, "visible": True, "value": ""}],
            place_bet_buttons=[{"slot": 0, "visible": True, "disabled": False},
                               {"slot": 1, "visible": True, "disabled": False}],
            presets=[],
            bet_tab_active=True,
        )
        return snap.as_dict()

    async def ensure_ready(self) -> None:
        self._advance_if_due()
        if self._balance < 100:
            raise PlacementUnavailable("insufficient_balance",
                                       "simulated balance below minimum stake")

    async def place(self, *, amount_bif: int, target_multiplier: float,
                    bet_slot: int, decision_id: str) -> PlacementOutcome:
        self._advance_if_due()
        if amount_bif > self._balance:
            raise PlacementUnavailable(
                "insufficient_balance",
                f"simulated balance {self._balance:.0f} BIF < stake {amount_bif} BIF",
            )
        self._queue.append({
            "decision_id": decision_id,
            "amount_bif": amount_bif,
            "target_multiplier": target_multiplier,
            "bet_slot": bet_slot,
        })
        target = self._current["index"] if self._current else self._index
        return PlacementOutcome(
            ok=True,
            simulated=True,
            detail=f"simulated bet queued for round {target} (slot {bet_slot})",
            round_label=f"SIM-R{target}",
        )

    async def collect_resolved(self) -> list[dict]:
        self._advance_if_due()
        out = self._resolved
        self._resolved = []
        return out


# ── Real browser backend ─────────────────────────────────────────────────────
class RealBrowserBackend:
    """Read-only real-browser executor (Part 1).

    ``place()`` raises ``PlacementUnavailable`` — the real DOM bet driver
    intentionally ships in Part 2. When placement is later enabled, this class
    remains the seam: the session only ever touches the browser through it.
    """

    simulated = False
    name_label = "real-browser"

    def __init__(self, browser_client: Any, *, allow_real_placement: bool = False):
        self.browser = browser_client
        self.allow_real_placement = bool(allow_real_placement)

    def name(self) -> str:
        return self.name_label

    def describe(self) -> dict:
        return {
            "backend": self.name(),
            "simulated": False,
            "allow_real_placement": self.allow_real_placement,
            "cdp_http": self.browser.settings.cdp_http,
        }

    async def readiness(self) -> dict:
        """Read-only readiness against the live browser."""
        return await self.browser.readiness()

    async def snapshot(self) -> dict:
        snap = await self.browser.snapshot()
        return snap.as_dict()

    async def ensure_ready(self) -> None:
        if not self.allow_real_placement:
            raise PlacementUnavailable(
                "placement_driver_not_implemented",
                "BETTING_ALLOW_REAL_PLACEMENT is off; the real DOM bet driver "
                "ships in Part 2 (Part 1 performs read-only verification only).",
            )

    async def place(self, *, amount_bif: int, target_multiplier: float,
                    bet_slot: int, decision_id: str) -> PlacementOutcome:
        raise PlacementUnavailable(
            "placement_driver_not_implemented",
            "RealBrowserBackend.place() is intentionally unimplemented in "
            "Part 1; see Part 2 for the DOM bet placement driver.",
        )

    async def collect_resolved(self) -> list[dict]:
        return []  # nothing is ever placed in Part 1

