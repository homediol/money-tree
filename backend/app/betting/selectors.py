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
    observedAt: new Date().toISOString(),
    payouts: [], bets: 0, balanceText: '', betPanel: false,
    stakeInputs: [], placeBetButtons: [], presets: [],
    phaseHints: [], betTabActive: false, cashoutControls: [], statusMessages: [],
    betPanelStates: [],
    bettingWindow: { state: 'UNKNOWN', observedAt: new Date().toISOString(), evidence: {
      source: 'AVIATOR_EXPLICIT_BETTING_PHASE_DOM', panel_index: 0,
      phase_indicator_visible: false, explicit_window_state: 'UNKNOWN',
    } },
  };
  const payEls = d.querySelectorAll('.payouts-block .payout');
  payEls.forEach((e, i) => { if (i < 10) out.payouts.push((e.textContent || '').trim()); });
  out.bets = d.querySelectorAll('.bets-list .bet-list-item').length;

  const appPanels = Array.from(d.querySelectorAll('app-bet-control'));
  out.betPanel = appPanels.length > 0;
  const visible = node => !!node && !!(node.offsetWidth || node.offsetHeight);
  const textOf = node => (node?.textContent || '').trim().replace(/\s+/g, ' ');
  const panelRoots = [0, 1].map(slot => appPanels[slot] || null);
  const observedAt = new Date().toISOString();
  const frameIdentity = `${location.origin}${location.pathname}#${Math.trunc(performance.timeOrigin)}`.slice(0, 180);
  const actionPattern = /^(bet|cancel|cash\s*out)\b/i;
  const visiblePhaseHints = [];
  d.querySelectorAll('[class*="betting" i], [class*="bet-status" i], [data-phase], [data-testid*="betting" i], [role="alert"], [aria-live="assertive"]').forEach((node, index) => {
    if (index >= 20 || !visible(node)) return;
    const text = textOf(node).slice(0, 120);
    if (text) visiblePhaseHints.push({text, source: 'VISIBLE_GAME_PHASE_DOM', visible: true});
  });
  const directPhaseText = visiblePhaseHints.map(item => item.text).join(' ').toLowerCase();
  const countdown = directPhaseText.match(/\b(?:betting|bets?)(?:\s+window)?\s+(?:closes|ends)\s+in\s+(\d+(?:\.\d+)?)\s*(?:s|sec|seconds?)\b/i);
  const countdownSeconds = countdown ? Number(countdown[1]) : null;
  const explicitWindowState = (Number.isFinite(countdownSeconds) && countdownSeconds > 0 && countdownSeconds <= 300)
    || /\b(?:bets? open|betting open|place your bets|you can bet now)\b/i.test(directPhaseText)
    ? 'BETTING_OPEN'
    : (Number.isFinite(countdownSeconds) && countdownSeconds <= 0)
      || /\b(?:bets? closed|betting closed|no more bets|betting is over)\b/i.test(directPhaseText)
      ? 'BETTING_CLOSED' : 'UNKNOWN';
  const explicitWindowEvidence = {
    source: 'AVIATOR_EXPLICIT_BETTING_PHASE_DOM', panel_index: 0,
    phase_indicator_visible: explicitWindowState !== 'UNKNOWN',
    explicit_window_state: explicitWindowState,
    text: visiblePhaseHints.map(item => item.text).join(' ').slice(0, 240),
    countdown_seconds: Number.isFinite(countdownSeconds) ? countdownSeconds : null,
    deadline_at: Number.isFinite(countdownSeconds) && countdownSeconds >= 0 && countdownSeconds <= 300
      ? new Date(Date.now() + countdownSeconds * 1000).toISOString() : null,
  };
  out.bettingWindow = {state: 'UNKNOWN', observedAt, evidence: explicitWindowEvidence};
  out.phaseHints.push(...visiblePhaseHints.slice(0, 6));

  for (let i = 0; i < 2; i++) {
    const panel = panelRoots[i];
    if (!panel) {
      out.betPanelStates.push({panel_id: `PANEL_${i + 1}`, slot: i, panel_visible: false,
        button_visible: false, button_enabled: false, source: 'AVIATOR_BETTING_PANEL_READ_ONLY',
        frame_identity: frameIdentity, observed_at: observedAt});
      continue;
    }
    const b = panel.querySelector('.bet-block');
    const inp = b?.querySelector('input[type="text"]:not([placeholder=""])');
    const inputVisible = visible(inp);
    const candidates = Array.from(panel.querySelectorAll('button, [role="button"], .btn'))
      .filter(node => !node.closest('.navigation-switcher'))
      .filter(visible).map(node => {
        const text = textOf(node);
        const aria = node.getAttribute('aria-label') || '';
        const title = node.getAttribute('title') || '';
        const combined = [aria, text, title].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
        const label = combined.match(actionPattern);
        const style = view.getComputedStyle(node);
        return {node, text, aria, title, combined, main: label ? label[1].replace(/\s+/g, ' ') : '',
          visible: style.display !== 'none' && style.visibility !== 'hidden' && visible(node)};
      }).filter(item => item.visible && item.main);
    const action = candidates[0] || null;
    const actionLabels = candidates.map(item => item.combined.slice(0, 120));
    const actionNode = action?.node || null;
    const actionStyle = actionNode ? view.getComputedStyle(actionNode) : null;
    const panelText = textOf(panel);
    const waitingText = panelText.match(/\bwaiting for (?:the )?next round\b/i)?.[0] || '';
    const buttonText = action?.text || action?.aria || action?.title || '';
    const mainLabel = action?.main.toLowerCase() === 'cash out' ? 'Cash Out'
      : action?.main ? action.main[0].toUpperCase() + action.main.slice(1).toLowerCase() : '';
    const fullActionText = action?.combined || '';
    const currencyCodes = [...new Set([...`${panelText} ${fullActionText}`.matchAll(/(?<![A-Z])([A-Z]{3})(?![A-Z])/g)]
      .map(item => item[1]).filter(code => !['BET', 'OUT'].includes(code)))];
    const secondaryText = [fullActionText.replace(actionPattern, '').replace(/\s+/g, ' ').trim(),
      waitingText].filter(Boolean).join(' ').slice(0, 160);
    const disabled = !!actionNode && (actionNode.disabled
      || actionNode.getAttribute('aria-disabled') === 'true'
      || actionNode.classList.contains('disabled'));
    const attrs = {};
    if (actionNode) for (const name of ['id', 'class', 'data-state', 'data-status', 'aria-label']) {
      const value = actionNode.getAttribute(name);
      if (value) attrs[name] = value.slice(0, 160);
    }
    const panelAttrs = {};
    for (const name of ['id', 'data-panel-id', 'data-slot', 'data-index']) {
      const value = panel.getAttribute(name);
      if (value) panelAttrs[name] = value.slice(0, 120);
    }
    const tabs = Array.from(panel.querySelectorAll('.navigation-switcher .tab'));
    const betTab = tabs.find(t => (t.textContent || '').trim() === 'Bet');
    const betTabActive = !!betTab && betTab.classList.contains('active');
    out.stakeInputs.push({
      slot: i, panel_id: `PANEL_${i + 1}`,
      selector: '.bet-block input[type="text"]:not([placeholder=""])',
      placeholder: inp ? (inp.placeholder || '') : null,
      value: inp ? (inp.value || '') : null,
      visible: inputVisible,
      minAttribute: inp ? (inp.getAttribute('min') || null) : null,
      maxAttribute: inp ? (inp.getAttribute('max') || null) : null,
    });
    out.betPanelStates.push({
      panel_id: `PANEL_${i + 1}`, slot: i,
      panel_visible: visible(panel), button_visible: !!action && !!actionStyle && action.visible,
      button_enabled: !!action && !disabled,
      main_label: mainLabel, button_text: buttonText.slice(0, 240),
      secondary_text: secondaryText.slice(0, 160),
      aria_label: action?.aria || null, title: action?.title || null,
      button_attributes: attrs, panel_attributes: panelAttrs,
      button_color: actionStyle ? actionStyle.backgroundColor : null,
      displayed_stake_text: mainLabel === 'Bet' ? fullActionText : (inp?.value || ''),
      cashout_text: mainLabel === 'Cash Out' ? fullActionText : '',
      visible_action_labels: actionLabels,
      pending_indicator: Boolean(waitingText) || /\bwaiting for (?:the )?next round\b/i.test(fullActionText),
      active_indicator: candidates.some(item => /^cash\s*out$/i.test(item.main)),
      currency_hint: currencyCodes.length === 1 ? currencyCodes[0] : null,
      data_state: actionNode?.getAttribute('data-state') || panel.getAttribute('data-state') || null,
      observed_at: observedAt, source: 'AVIATOR_BETTING_PANEL_READ_ONLY', frame_identity: frameIdentity,
      selector: 'app-bet-control .bet-block action button',
      stake_input_visible: inputVisible, bet_tab_active: betTabActive,
    });
    if (i === 0) out.betTabActive = betTabActive;
    if (mainLabel === 'Bet' && actionNode) out.placeBetButtons.push({
      slot: i, selector: 'app-bet-control .bet-block action button', associatedPanel: i,
      text: buttonText.slice(0, 240), disabled, visible: !!action.visible,
    });
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
  }

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
      const style = view.getComputedStyle(e);
      const visible = style.display !== 'none' && style.visibility !== 'hidden'
        && !!(e.offsetWidth || e.offsetHeight);
      if (visible) out.phaseHints.push({ cls: c,
        txt: (e.textContent || '').trim().replace(/\s+/g, ' ').slice(0, 80),
        visible: true, source: 'AVIATOR_STAGE_STATUS_DOM' });
    }
  });

  return out;
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
    round_phase: str = "UNKNOWN"
    deadline_evidence: dict = field(default_factory=lambda: {"status": "UNKNOWN"})
    observed_at: str | None = None
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
            from app.betting.platform_state import (
                classify_bet_panel_observation, classify_betting_window,
                classify_round_phase, observe_betting_deadline,
            )
            betting_window["state"] = classify_betting_window(
                betting_window.get("evidence"), panel=0,
            )
        except Exception:
            betting_window["state"] = "UNKNOWN"
        phase_hints = list(value.get("phaseHints") or [])
        status_messages = list(value.get("statusMessages") or [])
        try:
            from app.betting.platform_state import classify_round_phase, observe_betting_deadline
            round_phase = classify_round_phase(
                betting_window=betting_window, phase_hints=phase_hints,
                status_messages=status_messages,
            )
            deadline_evidence = observe_betting_deadline(
                phase_hints=phase_hints, status_messages=status_messages,
                observed_at=value.get("observedAt"),
            )
        except Exception:
            round_phase = "UNKNOWN"
            deadline_evidence = {"status": "UNKNOWN", "reason": "PHASE_PARSER_ERROR"}
        balance_text = value.get("balanceText") or ""
        parsed_balance = parse_balance_display(balance_text, value.get("balanceCurrency"))
        bal = parsed_balance["amount"]
        raw_panel_states = value.get("betPanelStates")
        raw_panel_states = raw_panel_states if isinstance(raw_panel_states, list) else []
        by_slot = {}
        for row in raw_panel_states:
            if not isinstance(row, dict):
                continue
            try:
                slot = int(row.get("slot", row.get("panel_index")))
            except (TypeError, ValueError):
                continue
            if slot in (0, 1):
                by_slot[slot] = row
        panel_states = []
        for slot in (0, 1):
            raw_panel = by_slot.get(slot, {
                "panel_id": f"PANEL_{slot + 1}", "panel_visible": False,
                "source": "AVIATOR_BETTING_PANEL_READ_ONLY", "observed_at": value.get("observedAt"),
            })
            normalized = classify_bet_panel_observation(raw_panel)
            if isinstance(raw_panel.get("stake_input_visible"), bool):
                normalized["stake_input_visible"] = raw_panel["stake_input_visible"]
            if isinstance(raw_panel.get("bet_tab_active"), bool):
                normalized["bet_tab_active"] = raw_panel["bet_tab_active"]
            normalized["panel_attributes"] = raw_panel.get("panel_attributes") or {}
            panel_states.append(normalized)
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
            phase_hints=phase_hints,
            bet_tab_active=bool(value.get("betTabActive")),
            cashout_controls=list(value.get("cashoutControls") or []),
            status_messages=status_messages,
            bet_panel_states=panel_states,
            betting_window=betting_window or {"state": "UNKNOWN"},
            round_phase=round_phase,
            deadline_evidence=deadline_evidence,
            observed_at=value.get("observedAt"),
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
            "phase_hints": self.phase_hints,
            "bet_tab_active": self.bet_tab_active,
            "cashout_controls": self.cashout_controls,
            "status_messages": self.status_messages,
            "bet_panel_states": self.bet_panel_states,
            "betting_window": self.betting_window,
            "round_phase": self.round_phase,
            "deadline_evidence": self.deadline_evidence,
            "observed_at": self.observed_at,
        }
