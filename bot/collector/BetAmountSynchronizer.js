import { inspectGameFrameSession } from './GameFrameAuth.js';
import { readCashoutPanel, readPanelSafety } from './AutoCashoutInitializer.js';
import { resolveSharedGameFrame } from './SharedGameFrame.js';

const RETRY_AFTER_MS = Number(process.env.BOT_BET_AMOUNT_RETRY_MS || 5000);

function result(status, value = {}) {
  return {
    status,
    execution_panel: Number.isInteger(value.execution_panel) ? value.execution_panel : null,
    requested_stake: value.requested_stake ?? null,
    currency: value.platform_currency || value.requested_stake_currency || null,
    requested_stake_currency: value.requested_stake_currency || null,
    platform_currency: value.platform_currency || null,
    observed_value: value.observed_value ?? null,
    session_id: value.session_id || null,
    updated_at: new Date().toISOString(),
    error_reason: value.error_reason || null,
  };
}

export function readStakePanel(slot) {
  const panels = Array.from(document.querySelectorAll('app-bet-control'));
  const panel = panels[slot];
  if (!panel) return { ok: false, panelCount: panels.length, reason: 'BETTING_PANEL_NOT_FOUND' };
  const betBlock = panel.querySelector('.bet-block');
  const input = betBlock?.querySelector('input[type="text"]:not([placeholder=""])');
  const betTab = Array.from(panel.querySelectorAll('.navigation-switcher .tab'))
    .find(element => (element.textContent || '').trim() === 'Bet');
  return {
    ok: true,
    panelCount: panels.length,
    betTabVisible: Boolean(betTab && (betTab.offsetWidth || betTab.offsetHeight)),
    betTabSelected: Boolean(betTab?.classList.contains('active')),
    inputFound: Boolean(input),
    inputVisible: Boolean(input && (input.offsetWidth || input.offsetHeight)),
    inputType: input?.type || null,
    value: input ? String(input.value || '').trim() : null,
    minAttribute: input?.getAttribute('min') || null,
    maxAttribute: input?.getAttribute('max') || null,
    pendingBetState: readPanelSafety(slot).pendingBetState,
  };
}

export function mutateStakePanel({ slot, action, value }) {
  const panel = Array.from(document.querySelectorAll('app-bet-control'))[slot];
  if (!panel) return { ok: false, reason: 'BETTING_PANEL_NOT_FOUND' };
  const safety = readPanelSafety(slot);
  if (safety.pendingBetState !== 'CLEAR') {
    return { ok: false, reason: safety.pendingBetState === 'PENDING'
      ? 'ACTIVE_BET_PENDING' : 'BETTING_STATE_UNKNOWN' };
  }
  if (action === 'select_bet_tab') {
    const tab = Array.from(panel.querySelectorAll('.navigation-switcher .tab'))
      .find(element => (element.textContent || '').trim() === 'Bet');
    if (!tab) return { ok: false, reason: 'BET_TAB_NOT_FOUND' };
    const changed = !tab.classList.contains('active');
    if (changed) tab.click();
    return { ok: true, changed };
  }
  if (action === 'select_auto_tab') {
    const tab = Array.from(panel.querySelectorAll('.navigation-switcher .tab'))
      .find(element => (element.textContent || '').trim() === 'Auto');
    if (!tab) return { ok: false, reason: 'AUTO_TAB_NOT_FOUND' };
    const changed = !tab.classList.contains('active');
    if (changed) tab.click();
    return { ok: true, changed };
  }
  if (action === 'set_stake') {
    const input = panel.querySelector('.bet-block input[type="text"]:not([placeholder=""])');
    if (!input || input.type !== 'text' || !(input.offsetWidth || input.offsetHeight)) {
      return { ok: false, reason: 'VISIBLE_EDITABLE_STAKE_INPUT_NOT_FOUND' };
    }
    const next = String(value);
    const changed = String(input.value || '').trim() !== next;
    if (changed) {
      const setter = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value')?.set;
      if (!setter) return { ok: false, reason: 'STAKE_INPUT_SETTER_UNAVAILABLE' };
      setter.call(input, next);
      input.dispatchEvent(new Event('input', { bubbles: true }));
      input.dispatchEvent(new Event('change', { bubbles: true }));
      input.blur();
    }
    return { ok: true, changed, value: String(input.value || '').trim() };
  }
  return { ok: false, reason: 'STAKE_ACTION_UNKNOWN' };
}

export async function fetchRulesEngineUiSetup({ baseUrl = process.env.WINNER_PREDICT_API_URL
  || process.env.BACKEND_URL || 'http://127.0.0.1:8000', signal } = {}) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 3500);
  const abortFromCaller = () => controller.abort(signal?.reason);
  signal?.addEventListener?.('abort', abortFromCaller, { once: true });
  try {
    const response = await fetch(`${baseUrl.replace(/\/$/, '')}/api/rules-engine/ui-setup`, {
      method: 'GET', headers: { accept: 'application/json' }, signal: controller.signal,
    });
    if (!response.ok) throw new Error(`RULES_ENGINE_UI_SETUP_HTTP_${response.status}`);
    return await response.json();
  } finally {
    clearTimeout(timeout);
    signal?.removeEventListener?.('abort', abortFromCaller);
  }
}

export class BetAmountSynchronizer {
  constructor({ frameManager, health, configProvider = fetchRulesEngineUiSetup,
    authInspector = inspectGameFrameSession, coordinator = null }) {
    this.frameManager = frameManager;
    this.health = health;
    this.configProvider = configProvider;
    this.authInspector = authInspector;
    this.coordinator = coordinator;
    this.inFlight = null;
    this.lastAttemptAt = 0;
    this.retryAfterMs = RETRY_AFTER_MS;
  }

  _publish(value) {
    this.health?.setBetAmountInitialization?.(value);
    return value;
  }

  initialize(page, { historyPage = null, signal, force = false } = {}) {
    if (!page || page.isClosed?.()) {
      return Promise.resolve(this._publish(result('UNKNOWN', {
        error_reason: !page ? 'SHARED_AVIATOR_PAGE_NOT_AVAILABLE' : 'SHARED_AVIATOR_PAGE_CLOSED',
      })));
    }
    if (this.inFlight) return this.inFlight;
    if (!force && Date.now() - this.lastAttemptAt < this.retryAfterMs) {
      return Promise.resolve(this.health?.snapshot?.().bet_amount_initialization);
    }
    this.lastAttemptAt = Date.now();
    this.inFlight = this._run(page, signal).catch(error => this._publish(result('UNKNOWN', {
      error_reason: `STAKE_SYNC_ERROR:${String(error?.code || error?.message || error).slice(0, 140)}`,
    }))).finally(() => { this.inFlight = null; });
    return this.inFlight;
  }

  async _run(page, signal) {
    let config;
    try { config = await this.configProvider({ signal }); }
    catch (error) {
      return this._publish(result('UNKNOWN', {
        error_reason: `RULES_ENGINE_SETUP_UNAVAILABLE:${String(error?.code || error?.message || error).slice(0, 120)}`,
      }));
    }
    const value = {
      execution_panel: config?.execution_panel,
      requested_stake: config?.requested_stake,
      requested_stake_currency: config?.requested_stake_currency,
      platform_currency: config?.platform_currency,
      session_id: config?.session_id,
    };
    if (config?.status !== 'READY') {
      return this._publish(result('BLOCKED', { ...value,
        error_reason: config?.reason || 'RULES_ENGINE_STAKE_CONFIGURATION_NOT_READY' }));
    }
    if (!Number.isInteger(value.execution_panel) || ![0, 1].includes(value.execution_panel)) {
      return this._publish(result('BLOCKED', { ...value, error_reason: 'EXECUTION_PANEL_NOT_SELECTED' }));
    }
    const amount = Number(value.requested_stake);
    if (!Number.isFinite(amount) || amount <= 0 || !value.session_id) {
      return this._publish(result('BLOCKED', { ...value, error_reason: 'SESSION_STAKE_CONFIGURATION_INVALID' }));
    }
    if (!value.requested_stake_currency || !value.platform_currency) {
      return this._publish(result('BLOCKED', { ...value, error_reason: 'STAKE_CURRENCY_UNVERIFIED' }));
    }
    if (String(value.requested_stake_currency).toUpperCase() !== String(value.platform_currency).toUpperCase()) {
      return this._publish(result('BLOCKED', { ...value, error_reason: 'STAKE_CURRENCY_MISMATCH' }));
    }
    if (config.authentication_state !== 'AUTHENTICATED') {
      return this._publish(result('BLOCKED', { ...value, error_reason: 'SHARED_AVIATOR_AUTHENTICATION_UNVERIFIED' }));
    }
    if (config.page_role !== 'SHARED_AVIATOR' || config.browser_topology_verified !== true) {
      return this._publish(result('BLOCKED', { ...value, error_reason: 'SHARED_AVIATOR_TOPOLOGY_UNVERIFIED' }));
    }
    const auth = await this.authInspector(page).catch(() => ({ status: 'UNKNOWN' }));
    if (auth?.status !== 'AUTHENTICATED') {
      return this._publish(result('BLOCKED', { ...value, error_reason: `AUTHENTICATION_${auth?.status || 'UNKNOWN'}` }));
    }

    let frame;
    try { frame = await resolveSharedGameFrame(this.frameManager, page, { timeoutMs: 10000, signal }); }
    catch (error) {
      return this._publish(result('UNKNOWN', { ...value,
        error_reason: `AVIATOR_FRAME_UNAVAILABLE:${String(error?.code || error?.message || error).slice(0, 100)}` }));
    }
    if (!frame || frame.isDetached?.()) {
      return this._publish(result('UNKNOWN', { ...value, error_reason: 'AVIATOR_FRAME_DETACHED' }));
    }

    const synchronize = async () => {
    for (let slot = 0; slot < 2; slot += 1) {
      const safety = await frame.evaluate(readPanelSafety, slot).catch(() => null);
      if (!safety?.ok || safety.panelCount !== 2 || safety.pendingBetState !== 'CLEAR') {
        const reason = safety?.pendingBetState === 'PENDING'
          ? `ACTIVE_BET_PENDING_PANEL_${slot + 1}` : `PENDING_BET_STATE_UNKNOWN_PANEL_${slot + 1}`;
        return this._publish(result('UNKNOWN', { ...value, error_reason: reason }));
      }
    }

    const slot = value.execution_panel;
    let state = await frame.evaluate(readStakePanel, slot).catch(() => null);
    if (!state?.ok || state.panelCount !== 2) {
      return this._publish(result('FAILED', { ...value, error_reason: 'TWO_PANEL_STAKE_INPUT_PREFLIGHT_FAILED' }));
    }
    if (!state.betTabSelected) {
      const clicked = await frame.evaluate(mutateStakePanel, { slot, action: 'select_bet_tab' });
      if (clicked?.ok !== true) {
        return this._publish(result('UNKNOWN', { ...value, error_reason: clicked?.reason || 'BET_TAB_SELECTION_FAILED' }));
      }
      await new Promise(resolve => setTimeout(resolve, 120));
      state = await frame.evaluate(readStakePanel, slot).catch(() => null);
    }
    if (!state?.ok || state.betTabSelected !== true || state.inputVisible !== true
        || state.inputType !== 'text' || state.pendingBetState !== 'CLEAR') {
      return this._publish(result('UNKNOWN', { ...value, observed_value: state?.value,
        error_reason: state?.pendingBetState === 'PENDING' ? 'ACTIVE_BET_PENDING'
          : 'EDITABLE_STAKE_INPUT_OR_CLEAR_BET_STATE_UNVERIFIED' }));
    }

    for (let panelSlot = 0; panelSlot < 2; panelSlot += 1) {
      const safety = await frame.evaluate(readPanelSafety, panelSlot).catch(() => null);
      if (!safety?.ok || safety.panelCount !== 2 || safety.pendingBetState !== 'CLEAR') {
        const reason = safety?.pendingBetState === 'PENDING'
          ? `ACTIVE_BET_PENDING_PANEL_${panelSlot + 1}` : `PENDING_BET_STATE_UNKNOWN_PANEL_${panelSlot + 1}`;
        return this._publish(result('UNKNOWN', { ...value, observed_value: state.value, error_reason: reason }));
      }
    }
    const requested = Number.isInteger(amount) ? String(amount) : String(amount);
    const applied = await frame.evaluate(mutateStakePanel, { slot, action: 'set_stake', value: requested });
    if (applied?.ok !== true) {
      return this._publish(result('FAILED', { ...value, observed_value: state.value,
        error_reason: applied?.reason || 'STAKE_INPUT_UPDATE_FAILED' }));
    }
    const readback = await frame.evaluate(readStakePanel, slot).catch(() => null);
    const accepted = readback?.ok === true && readback.inputVisible === true
      && Number(readback.value) === amount && readback.pendingBetState === 'CLEAR';
    if (!accepted) {
      return this._publish(result('FAILED', { ...value, observed_value: readback?.value,
        error_reason: 'STAKE_INPUT_READBACK_MISMATCH' }));
    }

    for (let panelSlot = 0; panelSlot < 2; panelSlot += 1) {
      const safety = await frame.evaluate(readPanelSafety, panelSlot).catch(() => null);
      if (!safety?.ok || safety.panelCount !== 2 || safety.pendingBetState !== 'CLEAR') {
        return this._publish(result('UNKNOWN', { ...value, observed_value: readback.value,
          error_reason: safety?.pendingBetState === 'PENDING'
            ? `ACTIVE_BET_PENDING_PANEL_${panelSlot + 1}`
            : `PENDING_BET_STATE_UNKNOWN_PANEL_${panelSlot + 1}` }));
      }
    }
    let cashout = await frame.evaluate(readCashoutPanel, slot).catch(() => null);
    if (cashout?.ok && cashout.autoTabSelected !== true) {
      const switched = await frame.evaluate(mutateStakePanel, { slot, action: 'select_auto_tab' }).catch(() => null);
      if (switched?.ok !== true) {
        return this._publish(result('FAILED', { ...value, observed_value: readback.value,
          error_reason: switched?.reason || 'AUTO_TAB_RESTORE_FAILED' }));
      }
      await new Promise(resolve => setTimeout(resolve, 120));
      cashout = await frame.evaluate(readCashoutPanel, slot).catch(() => null);
    }
    if (!cashout?.ok || cashout.autoBetEnabled !== false || cashout.autoCashoutEnabled !== true
        || cashout.autoTabSelected !== true || String(cashout.multiplier).trim() !== '2.00') {
      return this._publish(result('FAILED', { ...value, observed_value: readback.value,
        error_reason: 'CASHOUT_READBACK_FAILED_AFTER_STAKE_SYNC' }));
    }
    return this._publish(result('READY', { ...value, observed_value: readback.value }));
    };
    return this.coordinator?.withBettingUiLock
      ? this.coordinator.withBettingUiLock(synchronize, { signal })
      : synchronize();
  }
}
