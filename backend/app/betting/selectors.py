"""Read-only DOM knowledge for the live Spribe Aviator game.

DOM topology discovered by live probing (2026-09-06) against winner.rw:

  page  https://winner.rw/en/virtual/crash-games/aviator
    └─ OOPIF iframe target:  https://aviaport.spribegaming.com/aviator?…
         (listed by /json as type=iframe; header & balance live here)
         └─ child iframe:    https://aviator-next.spribegaming.com/?…
              (same-site child → reachable via contentDocument; the actual
               game plane, betting panel and payout history live here)

Observation expressions below are read-only (querySelector / style reads
only). The browser client evaluates outer-shell expressions in aviaport and
game expressions directly in an isolated aviator-next frame context.
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

# Evaluated directly in the aviator-next CDP execution context.
GAME_DOCUMENT_PROBE = """() => {
  if (!document.body) return { ok: false, err: 'aviator-next body is not ready' };
  const visible = node => !!node && !!(node.offsetWidth || node.offsetHeight);
  const loginPromptVisible = Array.from(document.querySelectorAll(
    'input[type="password"], input[autocomplete*="password" i], button, a'
  )).some(node => visible(node) && /^(?:log\\s*in|login|sign\\s*in)$/i.test((node.textContent || '').trim()))
    || /\\b(?:please\\s+)?(?:log\\s*in|sign\\s*in)\\s+(?:to\\s+)?(?:play|continue)\\b|\\b(?:authentication|session)\\s+(?:is\\s+)?required\\b/i.test(document.body.innerText || '');
  const sessionEnded = /session\\s+ended[\\s\\S]{0,100}opened in another window/i.test(document.body.innerText || '');
  return { ok: !sessionEnded, readyState: document.readyState, loginPromptVisible, sessionEnded,
    err: sessionEnded ? 'AVIATOR_SESSION_ENDED_IN_ANOTHER_WINDOW' : null };
}"""

# JS snippet: read balance from the aviaport (outer) document.
READ_BALANCE = """() => {
  const el = document.querySelector('.header__balance, .header__wrap-balance');
  if (!el) return { ok: false, reason: 'balance element not found' };
  const txt = (el.textContent || '').trim();
  const codes = txt.toUpperCase().match(/(?:^|\\s)([A-Z]{3})(?=$|\\s)/g) || [];
  const currency = codes.length === 1 ? codes[0].trim() : null;
  const visible = node => !!node && !!(node.offsetWidth || node.offsetHeight);
  const authenticated = Array.from(document.querySelectorAll(
    '.au-m-nav-u.login-area.usr-lgd-in, a.usr-balance, .user-profile, .account-dropdown, .user-avatar, a[href*="logout"], [data-testid="user-balance"]'
  )).some(visible);
  const loginVisible = Array.from(document.querySelectorAll(
    '#user-menu-login, a.login-btn, #phoneInput, [data-testid="login-button"]'
  )).some(visible);
  return { ok: true, text: txt, currency,
    authenticationState: loginVisible ? 'EXPIRED' : authenticated ? 'AUTHENTICATED' : 'UNKNOWN' };
}"""

# JS snippet: full read-only capability snapshot. It is evaluated directly in
# the aviator-next frame's isolated world. No mutation: no clicks/input/nav.
SNAPSHOT = r"""() => {
  const d = document;
  const view = window;
  if (!d.body) return { ok: false, err: 'aviator-next body is not ready' };
  const out = {
    ok: true,
    payouts: [], bets: 0, balanceText: '', betPanel: false,
    stakeInputs: [], placeBetButtons: [], presets: [],
    phaseHints: [], betTabActive: false, cashoutControls: [], statusMessages: [],
    betPanelStates: [],
    bettingWindow: { state: 'UNKNOWN', observedAt: new Date().toISOString(), evidence: {
      source: 'AVIATOR_BETTING_PANEL_READ_ONLY', selector: '.btn-success.bet',
      panel_index: 0, button_visible: false, button_enabled: false,
      stake_input_visible: false, bet_tab_active: false,
    } },
  };
  const payEls = d.querySelectorAll('.payouts-block .payout');
  payEls.forEach((e, i) => { if (i < 10) out.payouts.push((e.textContent || '').trim()); });
  out.bets = d.querySelectorAll('.bets-list .bet-list-item').length;

  const appPanels = Array.from(d.querySelectorAll('app-bet-control'));
  out.betPanel = appPanels.length > 0;
  appPanels.forEach((panel, i) => {
    if (i >= 2) return;
    const b = panel.querySelector('.bet-block');
    const inp = b?.querySelector('input[type="text"]:not([placeholder=""])');
    const visible = node => !!node && !!(node.offsetWidth || node.offsetHeight);
    const textOf = node => (node?.textContent || '').trim().replace(/\s+/g, ' ');
    const button = panel.querySelector('.btn-success.bet');
    const buttonText = textOf(button);
    const buttonStyle = button ? view.getComputedStyle(button) : null;
    const buttonVisible = !!button && !!buttonStyle && buttonStyle.display !== 'none'
      && buttonStyle.visibility !== 'hidden' && visible(button);
    const buttonDisabled = !!button && (button.disabled
      || button.getAttribute('aria-disabled') === 'true'
      || button.classList.contains('disabled'));
    const pendingControls = Array.from(panel.querySelectorAll('button, .btn')).filter(node => {
      if (!visible(node)) return false;
      return /^(?:cash\s*out|take\s*win|collect|cancel(?:\s*bet)?)\b/i.test(textOf(node));
    });
    const pendingBetState = pendingControls.length ? 'PENDING'
      : /^Bet\b/i.test(buttonText) ? 'CLEAR' : 'UNKNOWN';
    out.stakeInputs.push({
      slot: i,
      selector: '.bet-block input[type="text"]:not([placeholder=""])',
      placeholder: inp ? (inp.placeholder || '') : null,
      value: inp ? (inp.value || '') : null,
      visible: visible(inp),
      minAttribute: inp ? (inp.getAttribute('min') || null) : null,
      maxAttribute: inp ? (inp.getAttribute('max') || null) : null,
    });
    const tabs = Array.from(panel?.querySelectorAll('.navigation-switcher .tab') || []);
    const betTab = tabs.find(t => (t.textContent || '').trim() === 'Bet');
    const betTabActive = !!betTab && betTab.classList.contains('active');
    const inputVisible = visible(inp);
    const panelEvidence = {
      source: 'AVIATOR_BETTING_PANEL_READ_ONLY', selector: '.btn-success.bet',
      panel_index: i,
      button_visible: buttonVisible, button_enabled: buttonVisible && !buttonDisabled,
      stake_input_visible: inputVisible, bet_tab_active: betTabActive,
      button_text: buttonText.slice(0, 40),
      button_class: button ? String(button.className || '').slice(0, 160) : null,
      pending_bet_state: pendingBetState,
    };
    out.betPanelStates.push({slot: i, ...panelEvidence});
    if (i === 0) {
      out.betTabActive = betTabActive;
      const controlsReady = buttonVisible && inputVisible && betTabActive;
      out.bettingWindow = {
        state: controlsReady ? (buttonDisabled ? 'BETTING_CLOSED' : 'BETTING_OPEN') : 'UNKNOWN',
        observedAt: new Date().toISOString(),
        evidence: panelEvidence,
      };
    }
    const autoTabs = Array.from(panel.querySelectorAll('.navigation-switcher .tab'));
    const autoTab = autoTabs.find(t => (t.textContent || '').trim() === 'Auto');
    const cashoutSwitch = panel.querySelector('.cash-out-switcher .input-switch');
    const autoBetSwitch = panel.querySelector('.auto-bet .input-switch');
    const cashoutInput = panel.querySelector('.cashout-spinner input[type="text"]');
    out.cashoutControls.push({
      slot: i,
      autoTabVisible: visible(autoTab),
      autoTabSelected: !!autoTab && autoTab.classList.contains('active'),
      autoBetEnabled: autoBetSwitch ? !autoBetSwitch.classList.contains('off') : null,
      autoCashoutEnabled: cashoutSwitch ? !cashoutSwitch.classList.contains('off') : null,
      displayedValue: cashoutInput ? (cashoutInput.value || '').trim() : null,
      inputVisible: visible(cashoutInput),
      platformAcknowledged: null,
    });
  });

  d.querySelectorAll('.btn-success.bet').forEach((btn, i) => {
    if (i >= 2) return;
    const cs = view.getComputedStyle(btn);
    out.placeBetButtons.push({
      slot: i,
      selector: '.btn-success.bet',
      associatedPanel: appPanels.indexOf(btn.closest('app-bet-control')),
      text: (btn.textContent || '').trim(),
      disabled: btn.disabled || btn.getAttribute('aria-disabled') === 'true' ||
                btn.classList.contains('disabled'),
      visible: cs.display !== 'none' && cs.visibility !== 'hidden' &&
               !!(btn.offsetWidth || btn.offsetHeight),
    });
  });

  d.querySelectorAll('[role="alert"], [aria-live="assertive"]').forEach((el, i) => {
    if (i >= 8) return;
    const style = view.getComputedStyle(el);
    const visible = style.display !== 'none' && style.visibility !== 'hidden'
      && !!(el.offsetWidth || el.offsetHeight);
    const text = (el.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 200);
    if (visible && text) out.statusMessages.push({ text, source: 'ACCESSIBLE_ALERT_REGION' });
  });

  d.querySelectorAll('.bet-block .bet-opt').forEach((p, i) => {
    if (i < 8) out.presets.push((p.textContent || '').trim());
  });

  d.querySelectorAll('[class*="stage" i], [class*="status" i]').forEach(e => {
    const c = (e.className || '').toString();
    if (c.length < 90 && out.phaseHints.length < 6) {
      out.phaseHints.push({ cls: c, txt: (e.textContent || '').trim().slice(0, 40) });
    }
  });

  return out;
}"""

# These two interaction expressions are used only by explicitly invoked UI
# practice methods. They never locate or click the bet button.
SET_AUTO_CASHOUT = r"""async () => {
  const slot = __SLOT__;
  const target = __TARGET__;
  const block = document.querySelectorAll('.bet-block')[slot];
  const panel = block?.closest('app-bet-control') || block?.parentElement;
  if (!block || !panel) return { ok: false, reason: 'BETTING_PANEL_NOT_FOUND' };
  const autoTab = Array.from(panel.querySelectorAll('.navigation-switcher .tab'))
    .find(el => (el.textContent || '').trim() === 'Auto');
  if (!autoTab) return { ok: false, reason: 'AUTO_TAB_NOT_FOUND' };
  const autoTabClicked = !autoTab.classList.contains('active');
  if (autoTabClicked) autoTab.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  const toggle = panel.querySelector('.cash-out-switcher .input-switch');
  if (!toggle) return { ok: false, reason: 'AUTO_CASHOUT_TOGGLE_NOT_FOUND', autoTabClicked };
  const toggleClicked = toggle.classList.contains('off');
  if (toggleClicked) toggle.click();
  await new Promise(resolve => setTimeout(resolve, 0));
  const input = panel.querySelector('.cashout-spinner input[type="text"]');
  if (!input) return { ok: false, reason: 'CASHOUT_VALUE_INPUT_NOT_FOUND', autoTabClicked, toggleClicked };
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
  if (!setter) return { ok: false, reason: 'INPUT_VALUE_SETTER_UNAVAILABLE', autoTabClicked, toggleClicked };
  setter.call(input, target);
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
  input.blur();
  await new Promise(resolve => setTimeout(resolve, 0));
  return {
    ok: true,
    autoTabClicked,
    toggleClicked,
    autoTabSelected: autoTab.classList.contains('active'),
    enabled: !toggle.classList.contains('off'),
    displayedValue: (input.value || '').trim(),
    inputVisible: !!(input.offsetWidth || input.offsetHeight),
    target,
    platformAcknowledged: null,
  };
}"""

SET_STAKE_INPUT = r"""() => {
  const slot = __SLOT__;
  const value = __VALUE__;
  const block = document.querySelectorAll('.bet-block')[slot];
  const input = block?.querySelector('input[type="text"]:not([placeholder=""])');
  if (!input || !(input.offsetWidth || input.offsetHeight))
    return { ok: false, reason: 'VISIBLE_STAKE_INPUT_NOT_FOUND' };
  const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
  if (!setter) return { ok: false, reason: 'INPUT_VALUE_SETTER_UNAVAILABLE' };
  setter.call(input, value);
  input.dispatchEvent(new Event('input', { bubbles: true }));
  input.dispatchEvent(new Event('change', { bubbles: true }));
  input.blur();
  return { ok: true, displayedValue: (input.value || '').trim(),
    selector: '.bet-block input[type="text"]:not([placeholder=""])',
    associatedPanel: slot };
}"""


# ── Parsing helpers (pure, unit-testable) ───────────────────────────────────
_NUM_RE = re.compile(r"[0-9][0-9.,]*")
_SUFFIX = {"K": 1e3, "M": 1e6, "B": 1e9}
_CURRENCY_RE = re.compile(r"(?<![A-Z])([A-Z]{3})(?![A-Z])")


def parse_balance_display(text: str | None, currency_hint: str | None = None) -> dict:
    """Parse an observed balance without inventing a zero or a currency.

    The amount is ``None`` when the UI text cannot be parsed. Currency is
    returned only when the display itself contains one unambiguous ISO-like
    three-letter code or the browser supplied that code from the same element.
    """
    if not isinstance(text, str) or not text.strip():
        return {"amount": None, "currency": None}
    match = _NUM_RE.search(text.replace("\u00a0", " "))
    if not match:
        return {"amount": None, "currency": None}
    raw = match.group(0).replace(",", "")
    try:
        amount = float(raw)
    except (ValueError, OverflowError):
        return {"amount": None, "currency": None}
    if amount < 0 or amount == float("inf"):
        return {"amount": None, "currency": None}
    tail = text[match.end():].strip().upper()
    scale = re.match(r"([KMB])(?=\s|$|[A-Z]{3}\b)", tail)
    if scale:
        amount *= _SUFFIX[scale.group(1)]
    codes = _CURRENCY_RE.findall(text.upper())
    hinted = str(currency_hint).strip().upper() if currency_hint else None
    currency = hinted if hinted and re.fullmatch(r"[A-Z]{3}", hinted) else None
    if currency is None and len(set(codes)) == 1:
        currency = codes[0]
    elif currency is None and len(set(codes)) > 1:
        currency = None
    return {"amount": amount, "currency": currency}


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
    # Do not confuse the B in the BIF currency code with a billion suffix.
    # Accept compact forms (12.3KBIF), spaced forms (12.3 K BIF), and a bare
    # scale suffix while requiring a real token boundary after K/M/B.
    scale = re.match(r"([KMB])(?=\s|$|BIF\b|RWF\b|USD\b)", tail)
    if scale:
        value *= _SUFFIX[scale.group(1)]
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
    balance: float | None = None
    balance_currency: str | None = None
    balance_observed_at: str | None = None
    provider_identity: str | None = None
    session_identity: str | None = None
    authentication_state: str = "UNKNOWN"
    page_role: str = "UNKNOWN"
    browser_pages: dict = field(default_factory=dict)
    browser_topology_verified: bool = False
    bet_panel: bool = False
    stake_inputs: list[dict] = field(default_factory=list)
    place_bet_buttons: list[dict] = field(default_factory=list)
    presets: list[str] = field(default_factory=list)
    phase_hints: list[dict] = field(default_factory=list)
    bet_tab_active: bool = False
    cashout_controls: list[dict] = field(default_factory=list)
    status_messages: list[dict] = field(default_factory=list)
    bet_panel_states: list[dict] = field(default_factory=list)
    betting_window: dict = field(default_factory=lambda: {"state": "UNKNOWN"})
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
        raw_window = value.get("bettingWindow")
        betting_window = dict(raw_window) if isinstance(raw_window, dict) else {}
        try:
            from app.betting.platform_state import classify_betting_window
            betting_window["state"] = classify_betting_window(
                betting_window.get("evidence"), panel=0,
            )
        except Exception:
            betting_window["state"] = "UNKNOWN"
        balance_text = value.get("balanceText") or ""
        parsed_balance = parse_balance_display(balance_text, value.get("balanceCurrency"))
        bal = parsed_balance["amount"]
        return cls(
            ok=True,
            payouts=payouts,
            bets_visible=int(value.get("bets") or 0),
            balance_text=balance_text,
            balance=bal,
            balance_currency=parsed_balance["currency"],
            bet_panel=bool(value.get("betPanel")),
            stake_inputs=list(value.get("stakeInputs") or []),
            place_bet_buttons=list(value.get("placeBetButtons") or []),
            presets=list(value.get("presets") or []),
            phase_hints=list(value.get("phaseHints") or []),
            bet_tab_active=bool(value.get("betTabActive")),
            cashout_controls=list(value.get("cashoutControls") or []),
            status_messages=list(value.get("statusMessages") or []),
            bet_panel_states=list(value.get("betPanelStates") or []),
            betting_window=betting_window or {"state": "UNKNOWN"},
        )

    def as_dict(self) -> dict:
        return {
            "ok": self.ok,
            "error": self.error,
            "payouts_head": self.payouts[:8],
            "bets_visible": self.bets_visible,
            "balance_text": self.balance_text,
            "balance": self.balance,
            "balance_currency": self.balance_currency,
            "balance_observed_at": self.balance_observed_at,
            "provider_identity": self.provider_identity,
            "session_identity": self.session_identity,
            "authentication_state": self.authentication_state,
            "page_role": self.page_role,
            "browser_pages": self.browser_pages,
            "browser_topology_verified": self.browser_topology_verified,
            "bet_panel": self.bet_panel,
            "ui_ready": self.ui_ready,
            "place_bet_enabled": self.place_bet_enabled,
            "stake_inputs": self.stake_inputs,
            "place_bet_buttons": self.place_bet_buttons,
            "presets": self.presets,
            "bet_tab_active": self.bet_tab_active,
            "cashout_controls": self.cashout_controls,
            "status_messages": self.status_messages,
            "bet_panel_states": self.bet_panel_states,
            "betting_window": self.betting_window,
        }
