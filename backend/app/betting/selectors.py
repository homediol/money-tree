"""Read-only DOM knowledge for the live Spribe Aviator game.

DOM topology discovered by live probing (2026-09-06) against winner.rw:

  page  https://winner.rw/en/virtual/crash-games/aviator
    └─ OOPIF iframe target:  https://aviaport.spribegaming.com/aviator?…
         (listed by /json as type=iframe; header & balance live here)
         └─ child iframe:    https://aviator-next.spribegaming.com/?…
              (same-site child → reachable via contentDocument; the actual
               game plane, betting panel and payout history live here)

All expressions below are read-only (querySelector / style reads only).
They are evaluated inside the aviaport document so that the aviator-next
child can be reached through ``contentDocument``.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ── Target URL fingerprints (from /json target list) ────────────────────────
PAGE_HINT = "winner.rw"  # must also contain 'aviator' (checked separately)
GAME_FRAME_HINT = "aviaport.spribegaming.com"
NEXT_GAME_HINT = "aviator-next"

# ── DOM class selectors (verified present in live probing) ─────────────────
BALANCE_SEL = ".header__balance, .header__wrap-balance"          # aviaport doc
PAYOUT_SEL = ".payouts-block .payout"                            # aviator-next
BET_BLOCK_SEL = ".bet-block"                                     # aviator-next
PLACE_BET_SEL = ".btn-success.bet"                               # aviator-next
PRESET_SEL = ".bet-opt"                                          # aviator-next
BETS_LIST_SEL = ".bets-list .bet-list-item"                      # aviator-next

# JS snippet: reach the aviator-next document (throws descriptive errors).
AVIATOR_NEXT_DOC = """() => {
  const f = document.querySelector('iframe[src*="aviator-next"]');
  if (!f) return { ok: false, err: 'aviator-next iframe not found' };
  if (!f.contentDocument || !f.contentDocument.body)
    return { ok: false, err: 'aviator-next contentDocument inaccessible (OOPIF boundary?)' };
  return { ok: true, doc: f.contentDocument };
}"""

# JS snippet: read balance from the aviaport (outer) document.
READ_BALANCE = """() => {
  const el = document.querySelector('.header__balance, .header__wrap-balance');
  if (!el) return { ok: false, reason: 'balance element not found' };
  const txt = (el.textContent || '').trim();
  return { ok: true, text: txt, html: (el.innerHTML || '').slice(0, 200) };
}"""

# JS snippet: full read-only capability snapshot of the game (evaluated in
# the aviator-next document). No mutation: no clicks, no input, no nav.
SNAPSHOT = r"""() => {
  const out = {
    ok: true,
    payouts: [], bets: 0, balanceText: '', betPanel: false,
    stakeInputs: [], placeBetButtons: [], presets: [],
    phaseHints: [], betTabActive: false,
  };
  const payEls = document.querySelectorAll('.payouts-block .payout');
  payEls.forEach((e, i) => { if (i < 10) out.payouts.push((e.textContent || '').trim()); });
  out.bets = document.querySelectorAll('.bets-list .bet-list-item').length;

  const blocks = document.querySelectorAll('.bet-block');
  out.betPanel = blocks.length > 0;
  blocks.forEach((b, i) => {
    if (i >= 2) return;
    const inp = b.querySelector('input[type="text"]:not([placeholder=""])');
    out.stakeInputs.push({
      slot: i,
      placeholder: inp ? (inp.placeholder || '') : null,
      value: inp ? (inp.value || '') : null,
      visible: inp ? !!(inp.offsetWidth || inp.offsetHeight) : false,
    });
    const tabs = Array.from(b.querySelectorAll('.tab')).map(t => (t.textContent || '').trim());
    if (i === 0) out.betTabActive = tabs.includes('Bet');
  });

  document.querySelectorAll('.btn-success.bet').forEach((btn, i) => {
    if (i >= 2) return;
    const cs = getComputedStyle(btn);
    out.placeBetButtons.push({
      slot: i,
      text: (btn.textContent || '').trim(),
      disabled: btn.disabled || btn.getAttribute('aria-disabled') === 'true' ||
                btn.classList.contains('disabled'),
      visible: cs.display !== 'none' && cs.visibility !== 'hidden' &&
               !!(btn.offsetWidth || btn.offsetHeight),
    });
  });

  document.querySelectorAll('.bet-block .bet-opt').forEach((p, i) => {
    if (i < 8) out.presets.push((p.textContent || '').trim());
  });

  document.querySelectorAll('[class*="stage" i], [class*="status" i]').forEach(e => {
    const c = (e.className || '').toString();
    if (c.length < 90 && out.phaseHints.length < 6) {
      out.phaseHints.push({ cls: c, txt: (e.textContent || '').trim().slice(0, 40) });
    }
  });

  const balEl = document.querySelector('.header__balance, .header__wrap-balance');
  if (balEl) out.balanceText = (balEl.textContent || '').trim();
  return out;
}"""


# ── Parsing helpers (pure, unit-testable) ───────────────────────────────────
_NUM_RE = re.compile(r"[0-9][0-9.,]*")
_SUFFIX = {"K": 1e3, "M": 1e6, "B": 1e9}


def parse_amount(text: str | None) -> float:
    """Parse a balance/stake string such as '0BIF', '1,234.5' or '12.3K'.

    Returns 0.0 for empty/unknown strings — never raises.
    """
    if not text:
        return 0.0
    m = _NUM_RE.search(text.replace("\u00a0", " "))
    if not m:
        return 0.0
    raw = m.group(0).replace(",", "")
    try:
        value = float(raw)
    except ValueError:
        return 0.0
    tail = text[m.end():].strip().upper()
    if tail and tail[0] in _SUFFIX:
        value *= _SUFFIX[tail[0]]
    return value


def parse_payout(text: str | None) -> float | None:
    """Parse a payout cell such as '2.61x' → 2.61 (None when unparsable)."""
    if not text:
        return None
    m = re.search(r"[0-9]+(?:\.[0-9]+)?", text)
    if not m:
        return None
    try:
        return float(m.group(0))
    except ValueError:
        return None


@dataclass
class GameSnapshot:
    """Read-only picture of the game at one instant (result of SNAPSHOT)."""

    ok: bool = True
    payouts: list[float] = field(default_factory=list)
    bets_visible: int = 0
    balance_text: str = ""
    balance: float = 0.0
    bet_panel: bool = False
    stake_inputs: list[dict] = field(default_factory=list)
    place_bet_buttons: list[dict] = field(default_factory=list)
    presets: list[str] = field(default_factory=list)
    phase_hints: list[dict] = field(default_factory=list)
    bet_tab_active: bool = False
    error: str | None = None

    @property
    def ui_ready(self) -> bool:
        """True when the betting panel is rendered and the place-bet button
        exists (readiness to *receive* a bet; balance/phase still gated)."""
        return bool(
            self.bet_panel
            and any(b.get("visible") for b in self.place_bet_buttons)
            and any(i.get("visible") for i in self.stake_inputs)
        )

    @property
    def place_bet_enabled(self) -> bool:
        return bool(self.place_bet_buttons) and any(
            b.get("visible") and not b.get("disabled")
            for b in self.place_bet_buttons
        )

    @property
    def latest_payout(self) -> float | None:
        return self.payouts[0] if self.payouts else None

    @classmethod
    def from_value(cls, value) -> "GameSnapshot":
        if not isinstance(value, dict):
            return cls(ok=False, error="evaluation returned no value")
        err = value.get("err")
        if value.get("ok") is False or err:
            return cls(ok=False, error=err or "snapshot failed")
        payouts = []
        for p in value.get("payouts", []):
            parsed = parse_payout(p)
            if parsed is not None:
                payouts.append(parsed)
        bal = parse_amount(value.get("balanceText") or "")
        return cls(
            ok=True,
            payouts=payouts,
            bets_visible=int(value.get("bets") or 0),
            balance_text=value.get("balanceText") or "",
            balance=bal,
            bet_panel=bool(value.get("betPanel")),
            stake_inputs=list(value.get("stakeInputs") or []),
            place_bet_buttons=list(value.get("placeBetButtons") or []),
            presets=list(value.get("presets") or []),
            phase_hints=list(value.get("phaseHints") or []),
            bet_tab_active=bool(value.get("betTabActive")),
        )

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "error": self.error,
            "payouts_head": self.payouts[:8],
            "bets_visible": self.bets_visible,
            "balance_text": self.balance_text,
            "balance": self.balance,
            "bet_panel": self.bet_panel,
            "ui_ready": self.ui_ready,
            "place_bet_enabled": self.place_bet_enabled,
            "stake_inputs": self.stake_inputs,
            "place_bet_buttons": self.place_bet_buttons,
            "presets": self.presets,
            "bet_tab_active": self.bet_tab_active,
        }

