import { inspectGameFrameSession } from './GameFrameAuth.js';
import { readPanelSafety } from './AutoCashoutInitializer.js';
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
    observed_currency: value.observed_currency ?? null,
    minimum_stake: value.minimum_stake ?? null,
    pending_bet_state: value.pending_bet_state || 'UNKNOWN',
    manual_correction: status === 'STAKE_MISMATCH'
      ? `Manually set Panel ${(Number(value.execution_panel) || 0) + 1} to ${value.requested_stake} ${value.requested_stake_currency}, then refresh verification.` : null,
    session_id: value.session_id || null,
    updated_at: new Date().toISOString(),
    error_reason: value.error_reason || null,
  };
}

export function readStakePanel(slot) {
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
  const pendingControls = Array.from(panel.querySelectorAll('button, .btn'))
    .filter(element => visible(element) && /^(?:cash\s*out|take\s*win|collect|cancel(?:\s*bet)?)\b/i
      .test(normalizedText(element)));
  const betButton = panel.querySelector('.btn-success.bet');
  const betText = normalizedText(betButton);
  const pendingBetState = pendingControls.length ? 'PENDING'
    : /^Bet\b/i.test(betText) ? 'CLEAR' : 'UNKNOWN';
  const betBlock = panel.querySelector('.bet-block');
  const input = betBlock?.querySelector('input[type="text"]:not([placeholder=""])');
  const betTab = Array.from(panel.querySelectorAll('.navigation-switcher .tab'))
    .find(element => (element.textContent || '').trim() === 'Bet');
  return {
    ok: true,
    panelCount: panels.length,
    betTabVisible: visible(betTab),
    betTabSelected: Boolean(betTab?.classList.contains('active')),
    inputFound: Boolean(input),
    inputVisible: visible(input),
    inputType: input?.type || null,
    value: input ? String(input.value || '').trim() : null,
    buttonText: betText,
    buttonVisible: visible(betButton),
    buttonEnabled: Boolean(betButton && !betButton.disabled
      && betButton.getAttribute('aria-disabled') !== 'true'
      && !betButton.classList.contains('disabled')),
    currency: (() => {
      const matches = [...new Set((betText.match(/\b[A-Z]{3}\b/g) || [])
        .filter(code => !['BET', 'OUT'].includes(code)))];
      return matches.length === 1 ? matches[0] : null;
    })(),
    minAttribute: input?.getAttribute('min') || null,
    maxAttribute: input?.getAttribute('max') || null,
    pendingBetState,
  };
}

export function mutateStakePanel({ slot, action, value }) {
  void slot; void action; void value;
  return { ok: false, reason: 'MANUAL_OPERATOR_CONFIGURATION_REQUIRED', mutation_performed: false };
}

export async function fetchRulesEngineUiSetup({ baseUrl = process.env.WINNER_PREDICT_API_URL
  || process.env.BACKEND_URL || 'http://127.0.0.1:8000', signal } = {}) {
  const controller = new AbortController();
  // This read performs an authenticated shared-page/topology and two-panel
  // snapshot in the backend; measured live calls can take several seconds.
  // Keep it bounded, but do not abort before the read-only verification can
  // return under normal CDP/UI load.
  const timeoutMs = Math.max(3500, Number(process.env.COLLECTOR_RULES_ENGINE_SETUP_TIMEOUT_MS) || 20_000);
  const timeout = setTimeout(() => controller.abort(), timeoutMs);
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

    for (let slot = 0; slot < 2; slot += 1) {
      const safety = await frame.evaluate(readPanelSafety, slot).catch(() => null);
      if (!safety?.ok || safety.panelCount !== 2 || safety.pendingBetState !== 'CLEAR') {
        const reason = safety?.pendingBetState === 'PENDING'
          ? `ACTIVE_BET_PENDING_PANEL_${slot + 1}` : `PENDING_BET_STATE_UNKNOWN_PANEL_${slot + 1}`;
        return this._publish(result('STAKE_UNAVAILABLE', { ...value, pending_bet_state: safety?.pendingBetState,
          error_reason: reason }));
      }
    }

    const slot = value.execution_panel;
    const state = await frame.evaluate(readStakePanel, slot).catch(() => null);
    if (!state?.ok || state.panelCount !== 2 || state.betTabSelected !== true
        || state.inputVisible !== true || state.inputType !== 'text' || state.pendingBetState !== 'CLEAR'
        || state.buttonVisible !== true || state.buttonEnabled !== true) {
      return this._publish(result('STAKE_UNAVAILABLE', { ...value,
        observed_value: state?.value, observed_currency: state?.currency,
        pending_bet_state: state?.pendingBetState,
        error_reason: state?.pendingBetState === 'PENDING' ? 'ACTIVE_OR_PENDING_BET'
          : 'VISIBLE_BET_TAB_STAKE_AND_CLEAR_STATE_REQUIRED' }));
    }
    const displayedButtonStake = Number((state.buttonText || '').match(/^Bet\s+([\d,]+(?:\.\d+)?)/i)?.[1]?.replaceAll(',', ''));
    const observedStake = Number(state.value);
    const expectedCurrency = String(value.requested_stake_currency).toUpperCase();
    const observedCurrency = String(state.currency || value.platform_currency || '').toUpperCase() || null;
    const minimumStake = state.minAttribute != null && state.minAttribute !== ''
      && Number.isFinite(Number(state.minAttribute)) ? Number(state.minAttribute) : null;
    if (!Number.isFinite(observedStake) || !Number.isFinite(displayedButtonStake) || !observedCurrency) {
      return this._publish(result('STAKE_UNAVAILABLE', { ...value, observed_value: state.value,
        observed_currency: observedCurrency, minimum_stake: minimumStake,
        pending_bet_state: state.pendingBetState, error_reason: 'STAKE_AMOUNT_OR_CURRENCY_UNAVAILABLE' }));
    }
    const matches = observedStake === amount && displayedButtonStake === amount
      && observedCurrency === expectedCurrency && observedCurrency === String(value.platform_currency).toUpperCase();
    return this._publish(result(matches ? 'STAKE_VERIFIED' : 'STAKE_MISMATCH', {
      ...value, observed_value: state.value, observed_currency: observedCurrency,
      minimum_stake: minimumStake, pending_bet_state: state.pendingBetState,
      error_reason: matches ? null : 'MANUAL_STAKE_OR_CURRENCY_DOES_NOT_MATCH_SAVED_SESSION',
    }));
  }
}
