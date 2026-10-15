/** Read-only verification of the operator-configured Aviator cashout panels. */
import { inspectGameFrameSession } from './GameFrameAuth.js';
import { resolveSharedGameFrame } from './SharedGameFrame.js';

const CASHOUT_TARGET = '2.00';
const PANEL_COUNT = 2;
const RETRY_AFTER_MS = Number(process.env.BOT_CASHOUT_INIT_RETRY_MS || 3000);
const READBACK_FRESH_MS = 300000;

function unknownPanel(slot, reason) {
  return {
    slot,
    status: 'UNKNOWN',
    auto_bet: 'UNKNOWN',
    auto_cashout: 'UNKNOWN',
    multiplier: null,
    observed_at: new Date().toISOString(),
    error_reason: reason || null,
  };
}

export function readPanelSafety(slot) {
  const visible = element => {
    if (!element) return false;
    const style = getComputedStyle(element);
    return style.display !== 'none' && style.visibility !== 'hidden'
      && Boolean(element.offsetWidth || element.offsetHeight);
  };
  const normalizedText = element => (element?.innerText || element?.textContent || '')
    .replace(/\s+/g, ' ').trim();
  const panels = Array.from(document.querySelectorAll('app-bet-control'));
  const panel = panels[slot];
  if (!panel) return { ok: false, panelCount: panels.length, pendingBetState: 'UNKNOWN' };
  const controls = Array.from(panel.querySelectorAll('button, .btn'))
    .map(element => ({ element, text: normalizedText(element) }));
  if (controls.some(({ element, text }) => visible(element)
      && /^(?:cash\s*out|take\s*win|collect|cancel(?:\s*bet)?)\b/i.test(text))) {
    return { ok: true, panelCount: panels.length, pendingBetState: 'PENDING',
      betButtonVisible: false, betButtonEnabled: false, betButtonText: null };
  }
  const betButton = panel.querySelector('.btn-success.bet');
  const betText = normalizedText(betButton);
  return {
    ok: true,
    panelCount: panels.length,
    pendingBetState: /^Bet\b/i.test(betText) ? 'CLEAR' : 'UNKNOWN',
    betButtonVisible: visible(betButton),
    betButtonEnabled: Boolean(betButton && !betButton.disabled
      && betButton.getAttribute('aria-disabled') !== 'true'
      && !betButton.classList.contains('disabled')),
    betButtonText: betText,
  };
}

/** Read one real app-bet-control using selectors observed in the Aviator DOM. */
export function readCashoutPanel(slot) {
  const visible = element => {
    if (!element) return false;
    const style = getComputedStyle(element);
    return style.display !== 'none' && style.visibility !== 'hidden'
      && Boolean(element.offsetWidth || element.offsetHeight);
  };
  const normalizedText = element => (element?.innerText || element?.textContent || '')
    .replace(/\s+/g, ' ').trim();
  const panels = Array.from(document.querySelectorAll('app-bet-control'));
  const panel = panels[slot];
  if (!panel) return { ok: false, panelCount: panels.length, reason: 'BETTING_PANEL_NOT_FOUND' };
  const tabs = Array.from(panel.querySelectorAll('.navigation-switcher .tab'));
  const autoTab = tabs.find(element => (element.textContent || '').trim() === 'Auto');
  const autoBet = panel.querySelector('.auto-bet .input-switch');
  const cashout = panel.querySelector('.cash-out-switcher .input-switch');
  const input = panel.querySelector('.cashout-spinner input[type="text"]');
  const pendingControls = Array.from(panel.querySelectorAll('button, .btn'))
    .filter(element => visible(element) && /^(?:cash\s*out|take\s*win|collect|cancel(?:\s*bet)?)\b/i
      .test(normalizedText(element)));
  const betButton = panel.querySelector('.btn-success.bet');
  const betText = normalizedText(betButton);
  const pendingBetState = pendingControls.length ? 'PENDING'
    : /^Bet\b/i.test(betText) ? 'CLEAR' : 'UNKNOWN';
  return {
    ok: true,
    panelCount: panels.length,
    autoTabVisible: visible(autoTab),
    autoTabSelected: Boolean(autoTab?.classList.contains('active')),
    autoBetEnabled: autoBet ? !autoBet.classList.contains('off') : null,
    autoCashoutEnabled: cashout ? !cashout.classList.contains('off') : null,
    multiplier: input ? String(input.value || '').trim() : null,
    inputVisible: visible(input),
    pendingBetState,
    betButtonText: betText,
    missing: {
      autoTab: !autoTab,
      autoBet: !autoBet,
      autoCashout: !cashout,
      multiplierInput: !input,
    },
  };
}

/** Compatibility guard for legacy callers; manual setup is read-only. */
export function mutateCashoutPanel() {
  // Retained as a compatibility export for old callers/tests. There is no UI
  // mutation path: all setup is manual and this function fails closed.
  return { ok: false, reason: 'MANUAL_OPERATOR_CONFIGURATION_REQUIRED', mutation_performed: false };
}

function panelResult(slot, state, status, reason = null) {
  const autoBet = state?.autoBetEnabled === false ? 'OFF'
    : state?.autoBetEnabled === true ? 'ON' : 'UNKNOWN';
  const autoCashout = state?.autoCashoutEnabled === false ? 'OFF'
    : state?.autoCashoutEnabled === true ? 'ON' : 'UNKNOWN';
  return {
    slot,
    status,
    panel_id: `PANEL_${slot + 1}`,
    auto_bet: autoBet,
    auto_cashout: autoCashout,
    multiplier: state?.multiplier ?? null,
    auto_tab_selected: state?.autoTabSelected === true,
    observed_at: new Date().toISOString(),
    frame_identity: state?.frame_identity || null,
    error_reason: reason,
    manual_correction: status === 'CASHOUT_MISMATCH'
      ? 'In this panel, select Auto, enable Auto Cash Out, set 2.00x, and keep Auto Bet OFF.' : null,
  };
}

export function summarizeCashoutInitialization(panels, reason = null) {
  const bothVerified = panels.length === PANEL_COUNT && panels.every(panel => panel.status === 'CASHOUT_VERIFIED');
  const anyMismatch = panels.some(panel => panel.status === 'CASHOUT_MISMATCH');
  const autoBet = panels.length === PANEL_COUNT && panels.every(panel => panel.auto_bet === 'OFF')
    ? 'OFF' : panels.some(panel => panel.auto_bet === 'ON') ? 'ON' : 'UNKNOWN';
  return {
    target: CASHOUT_TARGET,
    auto_bet: autoBet,
    status: bothVerified ? 'CASHOUT_VERIFIED' : anyMismatch ? 'CASHOUT_MISMATCH' : 'CASHOUT_UNAVAILABLE',
    panel_1_cashout: panels[0]?.status || 'UNKNOWN',
    panel_2_cashout: panels[1]?.status || 'UNKNOWN',
    panels,
    updated_at: new Date().toISOString(),
    error_reason: reason || panels.map(panel => panel.error_reason).filter(Boolean).join('; ') || null,
  };
}

export class AutoCashoutInitializer {
  constructor({ frameManager, health, authInspector = inspectGameFrameSession,
    retryAfterMs = RETRY_AFTER_MS, panelWaitMs = 10000, coordinator = null }) {
    this.frameManager = frameManager;
    this.health = health;
    this.authInspector = authInspector;
    this.retryAfterMs = retryAfterMs;
    this.panelWaitMs = panelWaitMs;
    this.coordinator = coordinator;
    this.inFlight = null;
    this.lastAttemptAt = 0;
    this.readyPage = null;
    this.readyFrame = null;
    this.lastContext = {};
    this.trackedPages = new WeakSet();
    this.frameReadyHandler = event => {
      const page = event?.page;
      const frame = event?.frame;
      const current = this.health?.snapshot?.().cashout_initialization;
      const updatedAt = Date.parse(current?.updated_at || '');
      const readbackAge = Date.now() - updatedAt;
      const readbackFresh = current?.status === 'CASHOUT_VERIFIED'
        && Number.isFinite(updatedAt) && readbackAge >= 0 && readbackAge <= READBACK_FRESH_MS;
      if (page && frame && frame === this.readyFrame && readbackFresh) return;
      if (!page || !frame || page.isClosed?.() || frame.isDetached?.()) return;
      this.readyFrame = frame;
      this.readyPage = null;
      // Each attempt has a bounded frame wait and panel preflight. If one is
      // already running, it will resolve the fresh FrameManager reference.
      if (this.inFlight) return;
      if (Date.now() - this.lastAttemptAt < this.retryAfterMs) return;
      this.lastAttemptAt = 0;
      void this.initialize(page, { ...this.lastContext, force: true });
    };
    this.frameManager?.on?.('ready', this.frameReadyHandler);
  }

  _publish(panels, reason = null) {
    const status = summarizeCashoutInitialization(panels, reason);
    this.health?.setCashoutInitialization?.(status);
    return status;
  }

  _unknown(reason) {
    return this._publish(Array.from({ length: PANEL_COUNT }, (_, slot) => ({
      ...unknownPanel(slot, reason), status: 'CASHOUT_UNAVAILABLE', panel_id: `PANEL_${slot + 1}`,
    })), reason);
  }

  _trackPage(page, historyPage, signal) {
    if (!page || this.trackedPages.has(page) || typeof page.on !== 'function') return;
    this.trackedPages.add(page);
    const onFrameChange = frame => {
      try {
        if (!/spribegaming|aviator|crash-game/i.test(frame?.url?.() || '')) return;
      } catch { return; }
      this.readyPage = null;
      this.readyFrame = null;
      this._unknown('GAME_FRAME_CHANGED');
      setTimeout(() => { void this.initialize(page, { historyPage, signal, force: true }); }, 150).unref?.();
    };
    page.on('framenavigated', onFrameChange);
    page.on('frameattached', onFrameChange);
    page.on('close', () => {
      this.readyPage = null;
      this.readyFrame = null;
      this._unknown('SHARED_AVIATOR_PAGE_CLOSED');
    });
    page.on('crash', () => {
      this.readyPage = null;
      this.readyFrame = null;
      this._unknown('SHARED_AVIATOR_PAGE_CRASHED');
    });
  }

  initialize(page, { historyPage = null, signal, force = false } = {}) {
    this.lastContext = { historyPage, signal };
    this._trackPage(page, historyPage, signal);
    if (!page || page.isClosed?.()) {
      return Promise.resolve(this._unknown(!page ? 'SHARED_AVIATOR_PAGE_NOT_AVAILABLE' : 'SHARED_AVIATOR_PAGE_CLOSED'));
    }
    if (this.inFlight) return this.inFlight;
    if (!force && page === this.readyPage) return Promise.resolve(this.health?.snapshot?.().cashout_initialization);
    if (!force && Date.now() - this.lastAttemptAt < this.retryAfterMs) {
      return Promise.resolve(this.health?.snapshot?.().cashout_initialization);
    }
    this.lastAttemptAt = Date.now();
    this.inFlight = this._run(page, signal).catch(error => {
      return this._unknown(`INITIALIZATION_ERROR:${String(error?.message || error).slice(0, 160)}`);
    }).finally(() => { this.inFlight = null; });
    return this.inFlight;
  }

  async _run(page, signal) {
    const auth = await this.authInspector(page).catch(error => ({
      status: 'UNKNOWN', reasonCode: `AUTH_INSPECTION_FAILED:${String(error?.message || error).slice(0, 100)}`,
    }));
    this.health?.setAuthenticationState?.(auth?.status === 'AUTHENTICATED' ? 'AUTHENTICATED'
      : auth?.status === 'EXPIRED' ? 'AUTH_REQUIRED' : 'UNKNOWN', auth?.reasonCode || null);
    if (auth?.status !== 'AUTHENTICATED') {
      return this._unknown(`AUTHENTICATION_${auth?.status || 'UNKNOWN'}:${auth?.reasonCode || 'UNVERIFIED'}`);
    }
    if (page.isClosed?.()) return this._unknown('SHARED_AVIATOR_PAGE_CLOSED');

    let frame;
    try {
      frame = await resolveSharedGameFrame(this.frameManager, page, {
        timeoutMs: this.panelWaitMs,
        signal,
      });
    } catch (error) {
      return this._unknown(`AVIATOR_FRAME_UNAVAILABLE:${String(error?.code || error?.message || error).slice(0, 120)}`);
    }
    if (!frame || frame.isDetached?.()) return this._unknown('AVIATOR_FRAME_DETACHED');

    const initialStates = [];
    for (let slot = 0; slot < PANEL_COUNT; slot += 1) {
      try { initialStates.push(await this._read(frame, slot)); }
      catch (error) {
        return this._unknown(`PANEL_PREFLIGHT_FAILED:${String(error?.message || error).slice(0, 120)}`);
      }
    }
    if (initialStates.some(state => !state?.ok || state.panelCount !== PANEL_COUNT)) {
      return this._unknown('TWO_PANEL_PREFLIGHT_UNAVAILABLE');
    }
    if (initialStates.some(state => state.pendingBetState !== 'CLEAR')) {
      const pendingPanel = initialStates.findIndex(state => state.pendingBetState === 'PENDING');
      const reason = pendingPanel >= 0 ? `ACTIVE_BET_PENDING_PANEL_${pendingPanel + 1}`
        : 'BETTING_WINDOW_STATE_UNKNOWN';
      return this._publish(initialStates.map((state, slot) => panelResult(slot, state, 'CASHOUT_UNAVAILABLE',
        state.pendingBetState === 'CLEAR' ? reason : state.pendingBetState === 'PENDING'
          ? 'ACTIVE_BET_PENDING' : 'BETTING_WINDOW_STATE_UNKNOWN')), reason);
    }

    let frameIdentity = null;
    try { frameIdentity = typeof frame.url === 'function' ? frame.url() : null; } catch { /* identity unavailable */ }
    for (const state of initialStates) state.frame_identity = frameIdentity;
    const panels = [];
    for (let slot = 0; slot < PANEL_COUNT; slot += 1) panels.push(this._verifyPanel(slot, initialStates[slot]));
    const status = this._publish(panels);
    this.readyPage = status.status === 'CASHOUT_VERIFIED' ? page : null;
    this.readyFrame = status.status === 'CASHOUT_VERIFIED' ? frame : this.readyFrame;
    return status;
  }

  async _read(frame, slot) {
    return frame.evaluate(readCashoutPanel, slot);
  }

  _verifyPanel(slot, state) {
    if (!state?.ok || state.panelCount !== PANEL_COUNT) {
      return panelResult(slot, state, 'CASHOUT_UNAVAILABLE', state?.reason || 'TWO_PANEL_CONTROLS_UNAVAILABLE');
    }
    if (state.pendingBetState !== 'CLEAR') {
      return panelResult(slot, state, 'CASHOUT_UNAVAILABLE', state.pendingBetState === 'PENDING'
        ? 'ACTIVE_OR_PENDING_BET' : 'BET_STATE_UNKNOWN');
    }
    if (Object.values(state.missing || {}).some(Boolean) || state.autoTabSelected !== true
        || state.inputVisible !== true || state.autoBetEnabled == null
        || state.autoCashoutEnabled == null || !state.multiplier) {
      return panelResult(slot, state, 'CASHOUT_UNAVAILABLE', 'MANUAL_AUTO_TAB_AND_CONTROLS_MUST_BE_VISIBLE');
    }
    const valid = state.autoBetEnabled === false && state.autoCashoutEnabled === true
      && String(state.multiplier).trim() === CASHOUT_TARGET;
    return panelResult(slot, state, valid ? 'CASHOUT_VERIFIED' : 'CASHOUT_MISMATCH',
      valid ? null : 'MANUAL_CASHOUT_SETTINGS_DO_NOT_MATCH_2_00X');
  }
}
