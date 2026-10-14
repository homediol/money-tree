/** Configure and verify the two Aviator auto-cashout panels. */
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

/** Apply one idempotent settings change; this function never targets a bet button. */
export function mutateCashoutPanel({ slot, action, value = '2.00' }) {
  const panel = Array.from(document.querySelectorAll('app-bet-control'))[slot];
  if (!panel) return { ok: false, reason: 'BETTING_PANEL_NOT_FOUND' };
  const visible = element => {
    if (!element) return false;
    const style = getComputedStyle(element);
    return style.display !== 'none' && style.visibility !== 'hidden'
      && Boolean(element.offsetWidth || element.offsetHeight);
  };
  const normalizedText = element => (element?.innerText || element?.textContent || '')
    .replace(/\s+/g, ' ').trim();
  const pendingControls = Array.from(panel.querySelectorAll('button, .btn'))
    .filter(element => visible(element) && /^(?:cash\s*out|take\s*win|collect|cancel(?:\s*bet)?)\b/i
      .test(normalizedText(element)));
  const betText = normalizedText(panel.querySelector('.btn-success.bet'));
  const pending = pendingControls.length ? 'PENDING'
    : /^Bet\b/i.test(betText) ? 'CLEAR' : 'UNKNOWN';
  if (pending !== 'CLEAR') {
    return { ok: false, reason: pending === 'PENDING' ? 'ACTIVE_BET_PENDING' : 'BETTING_STATE_UNKNOWN' };
  }

  if (action === 'select_auto_tab') {
    const tab = Array.from(panel.querySelectorAll('.navigation-switcher .tab'))
      .find(element => (element.textContent || '').trim() === 'Auto');
    if (!tab) return { ok: false, reason: 'AUTO_TAB_NOT_FOUND' };
    const changed = !tab.classList.contains('active');
    if (changed) tab.click();
    return { ok: true, changed };
  }
  if (action === 'disable_auto_bet') {
    const toggle = panel.querySelector('.auto-bet .input-switch');
    if (!toggle) return { ok: false, reason: 'AUTO_BET_TOGGLE_NOT_FOUND' };
    const changed = !toggle.classList.contains('off');
    if (changed) toggle.click();
    return { ok: true, changed };
  }
  if (action === 'enable_auto_cashout') {
    const toggle = panel.querySelector('.cash-out-switcher .input-switch');
    if (!toggle) return { ok: false, reason: 'AUTO_CASHOUT_TOGGLE_NOT_FOUND' };
    const changed = toggle.classList.contains('off');
    if (changed) toggle.click();
    return { ok: true, changed };
  }
  if (action === 'set_multiplier') {
    const input = panel.querySelector('.cashout-spinner input[type="text"]');
    if (!input) return { ok: false, reason: 'CASHOUT_VALUE_INPUT_NOT_FOUND' };
    const changed = String(input.value || '').trim() !== value;
    if (changed) {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
      if (!setter) return { ok: false, reason: 'INPUT_VALUE_SETTER_UNAVAILABLE' };
      setter.call(input, value);
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
      input.blur();
    }
    return { ok: true, changed, multiplier: String(input.value || '').trim() };
  }
  return { ok: false, reason: 'CASHOUT_ACTION_UNKNOWN' };
}

function panelResult(slot, state, status, reason = null) {
  const autoBet = state?.autoBetEnabled === false ? 'OFF'
    : state?.autoBetEnabled === true ? 'ON' : 'UNKNOWN';
  const autoCashout = state?.autoCashoutEnabled === false ? 'OFF'
    : state?.autoCashoutEnabled === true ? 'ON' : 'UNKNOWN';
  return {
    slot,
    status,
    auto_bet: autoBet,
    auto_cashout: autoCashout,
    multiplier: state?.multiplier ?? null,
    auto_tab_selected: state?.autoTabSelected === true,
    observed_at: new Date().toISOString(),
    error_reason: reason,
  };
}

export function summarizeCashoutInitialization(panels, reason = null) {
  const bothReady = panels.length === PANEL_COUNT && panels.every(panel => panel.status === 'READY');
  const anyFailed = panels.some(panel => panel.status === 'FAILED');
  const autoBet = panels.length === PANEL_COUNT && panels.every(panel => panel.auto_bet === 'OFF')
    ? 'OFF' : panels.some(panel => panel.auto_bet === 'ON') ? 'ON' : 'UNKNOWN';
  return {
    target: CASHOUT_TARGET,
    auto_bet: autoBet,
    status: bothReady ? 'READY' : anyFailed ? 'FAILED' : 'UNKNOWN',
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
      const readbackFresh = current?.status === 'READY'
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
    return this._publish(Array.from({ length: PANEL_COUNT }, (_, slot) => unknownPanel(slot, reason)), reason);
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
      return this._publish(initialStates.map((state, slot) => panelResult(slot, state, 'UNKNOWN',
        state.pendingBetState === 'CLEAR' ? reason : state.pendingBetState === 'PENDING'
          ? 'ACTIVE_BET_PENDING' : 'BETTING_WINDOW_STATE_UNKNOWN')), reason);
    }

    const panels = [];
    for (let slot = 0; slot < PANEL_COUNT; slot += 1) {
      const configure = () => this._initializePanel(frame, slot);
      panels.push(await (this.coordinator?.withBettingUiLock
        ? this.coordinator.withBettingUiLock(configure, { signal })
        : configure()));
    }
    const status = this._publish(panels);
    this.readyPage = status.status === 'READY' ? page : null;
    this.readyFrame = status.status === 'READY' ? frame : this.readyFrame;
    return status;
  }

  async _read(frame, slot) {
    return frame.evaluate(readCashoutPanel, slot);
  }

  async _act(frame, slot, action) {
    // Recheck both panels immediately before each setting mutation so an
    // active or ambiguous bet in either panel blocks changes in both.
    for (let otherSlot = 0; otherSlot < PANEL_COUNT; otherSlot += 1) {
      const otherState = await this._read(frame, otherSlot);
      if (!otherState?.ok || otherState.panelCount !== PANEL_COUNT) {
        throw Object.assign(new Error('TWO_PANEL_PREFLIGHT_UNAVAILABLE'), { code: 'TWO_PANEL_PREFLIGHT_UNAVAILABLE' });
      }
      if (otherState.pendingBetState !== 'CLEAR') {
        const reason = otherState.pendingBetState === 'PENDING'
          ? `ACTIVE_BET_PENDING_PANEL_${otherSlot + 1}` : 'BETTING_WINDOW_STATE_UNKNOWN';
        throw Object.assign(new Error(reason), { code: reason });
      }
    }
    const result = await frame.evaluate(mutateCashoutPanel, { slot, action, value: CASHOUT_TARGET });
    if (result?.ok !== true) throw Object.assign(new Error(result?.reason || 'CASHOUT_ACTION_FAILED'), {
      code: result?.reason || 'CASHOUT_ACTION_FAILED',
    });
    if (result.changed) await new Promise(resolve => setTimeout(resolve, 100));
  }

  async _initializePanel(frame, slot) {
    let state;
    try { state = await this._read(frame, slot); }
    catch (error) { return unknownPanel(slot, `PANEL_READ_FAILED:${String(error?.message || error).slice(0, 120)}`); }
    if (!state?.ok) return panelResult(slot, state, 'FAILED', state?.reason || 'BETTING_PANEL_NOT_FOUND');
    if (state.panelCount !== PANEL_COUNT) return panelResult(slot, state, 'FAILED', 'EXPECTED_TWO_BETTING_PANELS');
    if (state.pendingBetState !== 'CLEAR') {
      return panelResult(slot, state, 'UNKNOWN', state.pendingBetState === 'PENDING'
        ? 'ACTIVE_BET_PENDING' : 'BETTING_WINDOW_STATE_UNKNOWN');
    }
    if (Object.values(state.missing || {}).some(Boolean)) {
      const reason = Object.entries(state.missing).filter(([, missing]) => missing)
        .map(([name]) => `${name.toUpperCase()}_NOT_FOUND`).join(',');
      return panelResult(slot, state, 'FAILED', reason || 'CASHOUT_CONTROL_NOT_FOUND');
    }

    try {
      if (!state.autoTabSelected) {
        await this._act(frame, slot, 'select_auto_tab');
        state = await this._read(frame, slot); // Re-query after the tab rerender.
      }
      if (!state?.ok) throw new Error('PANEL_RERENDER_READ_FAILED');
      if (state.pendingBetState !== 'CLEAR') throw new Error(state.pendingBetState === 'PENDING'
        ? 'ACTIVE_BET_PENDING' : 'BETTING_WINDOW_STATE_UNKNOWN');
      if (state.autoBetEnabled !== false) {
        if (state.autoBetEnabled !== true) throw new Error('AUTO_BET_STATE_UNKNOWN');
        await this._act(frame, slot, 'disable_auto_bet');
        state = await this._read(frame, slot);
      }
      if (!state?.ok || state.autoBetEnabled !== false) throw new Error('AUTO_BET_COULD_NOT_BE_VERIFIED_OFF');
      if (state.pendingBetState !== 'CLEAR') throw new Error(state.pendingBetState === 'PENDING'
        ? 'ACTIVE_BET_PENDING' : 'BETTING_WINDOW_STATE_UNKNOWN');
      if (state.autoCashoutEnabled !== true) {
        if (state.autoCashoutEnabled !== false) throw new Error('AUTO_CASHOUT_STATE_UNKNOWN');
        await this._act(frame, slot, 'enable_auto_cashout');
        state = await this._read(frame, slot);
      }
      if (!state?.ok || state.autoCashoutEnabled !== true) throw new Error('AUTO_CASHOUT_COULD_NOT_BE_VERIFIED_ON');
      if (state.pendingBetState !== 'CLEAR') throw new Error(state.pendingBetState === 'PENDING'
        ? 'ACTIVE_BET_PENDING' : 'BETTING_WINDOW_STATE_UNKNOWN');
      if (String(state.multiplier).trim() !== CASHOUT_TARGET) {
        await this._act(frame, slot, 'set_multiplier');
        state = await this._read(frame, slot);
      }
      const exactMultiplier = String(state?.multiplier || '').trim() === CASHOUT_TARGET;
      const ready = state?.ok && state.autoTabSelected === true
        && state.autoBetEnabled === false && state.autoCashoutEnabled === true && exactMultiplier;
      return panelResult(slot, state, ready ? 'READY' : 'FAILED', ready ? null : 'CASHOUT_READBACK_MISMATCH');
    } catch (error) {
      try { state = await this._read(frame, slot); } catch { /* Preserve last known readback. */ }
      const reason = String(error?.code || error?.message || error).slice(0, 160);
      const status = /ACTIVE_BET_PENDING|BETTING_WINDOW_STATE_UNKNOWN|PENDING/.test(reason) ? 'UNKNOWN' : 'FAILED';
      return panelResult(slot, state, status, reason);
    }
  }
}
