"""Executors that translate risk-approved decisions into game actions.

Two backends implement the same ``PlacementBackend`` protocol:

- ``RealBrowserBackend`` — attaches to the collector-owned CDP browser and
  exposes guarded UI configuration/inspection. Financial placement remains
  fail-closed until acceptance and settlement receipts are implemented.
- ``SimulationBackend`` — a deterministic scripted environment used for
  demo/testing. Every entry it produces carries ``simulated: true`` and its
  ledger/events never claim a real placement happened.

The module intentionally contains no prediction logic.
"""
from __future__ import annotations

import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

from app.betting.cashout import inspect_cashout_configuration
from app.betting.selectors import GameSnapshot

UNVERIFIED_LIVE_CAPABILITIES = {
    "bet_submission": False,
    "target_identity": False,
    "betting_deadline": False,
    "cashout_acknowledgement": False,
    "acceptance_receipt": False,
    "settlement_receipt": False,
    "currency_validation": False,
}

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

    async def cashout_configuration(self, panel: int = 0) -> dict:
        """Read cashout state without changing the page."""
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
                "balance_after": self._balance,
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

    async def cashout_configuration(self, panel: int = 0) -> dict:
        return {**inspect_cashout_configuration(None), "simulated": True,
                "failures": ["SIMULATION_HAS_NO_PLATFORM_CASHOUT_SETTING"]}

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
    """Live executor using only the collector-owned browser/context."""

    simulated = False
    name_label = "real-browser"

    def __init__(self, browser_client: Any, *, allow_real_placement: bool = False,
                 execution_gate=None):
        self.browser = browser_client
        self.allow_real_placement = bool(allow_real_placement)
        self.execution_gate = execution_gate
        self._pending: list[dict] = []

    def name(self) -> str:
        return self.name_label

    def describe(self) -> dict:
        return {
            "backend": self.name(),
            "simulated": False,
            "allow_real_placement": self.allow_real_placement,
            "cdp_http": self.browser.settings.cdp_http,
        }

    def verified_capabilities(self) -> dict[str, bool]:
        """The current CDP selector client has no receipt or target proof.

        A visible button and a click are insufficient to claim an accepted
        wager. The real rule's activation review consumes this explicit
        capability contract rather than inferring readiness from a page.
        """
        return dict(UNVERIFIED_LIVE_CAPABILITIES)

    async def readiness(self) -> dict:
        """Read-only readiness against the live browser."""
        readiness = await self.browser.readiness()
        readiness["cashout_configuration"] = await self.cashout_configuration(0)
        return readiness

    async def cashout_configuration(self, panel: int = 0) -> dict:
        if hasattr(self.browser, "cashout_configuration"):
            return await self.browser.cashout_configuration(panel)
        return inspect_cashout_configuration(None)

    async def read_auto_cashout(self, panel: int = 0) -> dict:
        method = getattr(self.browser, "read_auto_cashout", None)
        if method is not None:
            return await method(panel=panel)
        return await self.cashout_configuration(panel)

    async def set_auto_cashout(self, target: float = 2.00, panel: int = 0) -> dict:
        """Cashout configuration is operator-owned; this adapter is read-only."""
        del target, panel
        return {**inspect_cashout_configuration(None), "status": "CASHOUT_UNAVAILABLE",
                "error": "MANUAL_OPERATOR_CONFIGURATION_REQUIRED", "mutation_performed": False}

    async def set_stake_input(self, amount: int | float | str = 100, panel: int = 0) -> dict:
        """Stake entry is operator-owned; this adapter never writes to the UI."""
        del amount, panel
        return {"ok": False, "error": "MANUAL_OPERATOR_CONFIGURATION_REQUIRED",
                "mutation_performed": False}

    async def read_stake_input(self, panel: int = 0) -> dict:
        method = getattr(self.browser, "read_stake_input", None)
        if method is None:
            return {"found": False, "reason": "STAKE_INPUT_READ_UNAVAILABLE"}
        return await method(panel=panel)

    async def detect_bet_button(self, panel: int = 0) -> dict:
        method = getattr(self.browser, "detect_bet_button", None)
        if method is None:
            return {"detected": False, "reason": "BET_BUTTON_DETECTION_UNAVAILABLE"}
        return await method(panel=panel)

    async def detect_betting_window(self, panel: int = 0) -> dict:
        method = getattr(self.browser, "detect_betting_window", None)
        if method is None:
            return {"state": "UNKNOWN", "reason": "BETTING_WINDOW_DETECTION_UNAVAILABLE"}
        return await method(panel=panel)

    async def detect_insufficient_balance(self) -> dict:
        method = getattr(self.browser, "detect_insufficient_balance", None)
        if method is None:
            return {"detected": False, "observed": False,
                    "matched_messages": [], "source": None}
        return await method()

    async def read_verified_balance(self) -> dict:
        method = getattr(self.browser, "read_verified_balance", None)
        if method is None:
            return {"verified": False, "amount": None, "currency": None,
                    "reason": "BALANCE_READ_UNAVAILABLE"}
        return await method()

    async def snapshot(self) -> dict:
        snap = await self.browser.snapshot()
        return snap.as_dict()

    async def ensure_ready(self) -> None:
        if not self.allow_real_placement:
            raise PlacementUnavailable(
                "placement_driver_not_implemented",
                "BETTING_ALLOW_REAL_PLACEMENT is off; live placement is disabled.",
            )
        ready = await self.readiness()
        if not ready.get("connected") or ready.get("stage") != "ready":
            raise PlacementUnavailable(
                "placement_driver_not_implemented",
                "Part 2 compatibility: collector-owned browser is not ready for the Part 9 driver",
            )

    async def place(self, *, amount_bif: int, target_multiplier: float,
                    bet_slot: int, decision_id: str, intent=None,
                    execution_id: str | None = None) -> PlacementOutcome:
        # The current selector probe cannot verify a platform acceptance
        # receipt, target-round identity, or a confirmed cashout. A click is
        # therefore unsafe even when the environment switch was enabled by
        # mistake.
        await self.ensure_ready()
        if self.execution_gate is None or intent is None:
            raise PlacementUnavailable(
                "final_safety_gate_unavailable",
                "Backend final safety authorization is unavailable; NO BET",
            )
        try:
            gate_parameters = inspect.signature(self.execution_gate).parameters
        except (TypeError, ValueError):
            gate_parameters = {}
        if "execution_id" in gate_parameters:
            gate = self.execution_gate(intent, execution_id=execution_id)
        else:
            gate = self.execution_gate(intent)
        if hasattr(gate, "__await__"):
            gate = await gate
        if not gate.get("allowed"):
            raise PlacementUnavailable(
                "final_safety_gate_blocked",
                "NO BET: " + ", ".join(gate.get("reasons", [])),
            )
        raise PlacementUnavailable(
            "platform_verification_incomplete",
            "Live platform acceptance, round and cashout evidence are "
            "unverified; NO BET",
        )

    async def collect_resolved(self) -> list[dict]:
        # A changing crash-history cell is not a bet result. Only a verified
        # platform receipt can resolve a real execution.
        return []
