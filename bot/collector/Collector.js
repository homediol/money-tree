/**
 * Collector.js — Injects MutationObserver into the Aviator iframe and
 * streams payout events back to Node.js.
 *
 * This module is stateless with respect to the frame — it always receives
 * a fresh frame from FrameManager and installs a new observer.
 */

import { log } from './Logger.js';
import { normalizeMultiplier, snapshotSignature } from './HistoryManager.js';
import { PAYOUT_SELECTOR } from './FrameManager.js';
import { classifyPanelConfigurationObservation, classifyPanelObservation,
  panelObservationTransition } from './PanelStateDetector.js';

const MUTATION_IDLE_MS = Number(process.env.BOT_IDLE_TIMEOUT || 60000);
// MutationObserver is primary; a low-frequency refresh bounds telemetry age
// when the panel remains unchanged for longer than the dashboard's freshness window.
const PANEL_OBSERVATION_REFRESH_MS = Number(process.env.BOT_PANEL_OBSERVATION_REFRESH_MS || 10000);

// ── In-frame code (evaluated directly inside the iframe) ────────────────────
// These are plain function expressions passed to frame.evaluate().
// Playwright serialises the function body and runs it in the frame context.
// Arguments are passed as the second parameter to frame.evaluate().

async function _installObserver(frame, selector) {
  return frame.evaluate(({selector: sel, refresh_ms: refreshMs}) => {
    function parse(text) {
      if (!text) return null;
      const c = String(text).trim().replace(/,/g,'').replace(/[xX×]/g,'').replace(/[^0-9.]/g,'');
      if (!c) return null;
      const v = parseFloat(c);
      return (isFinite(v) && v > 0) ? Math.round(v * 100) / 100 : null;
    }
    function readAll() {
      return Array.from(document.querySelectorAll(sel))
        .map(el => parse(el.textContent)).filter(v => v !== null);
    }
    function sig(arr) { return arr.map(v => v.toFixed(2)).join('|'); }

    function readGamePhase() {
      const now = new Date().toISOString();
      const visible = node => Boolean(node && (node.offsetWidth || node.offsetHeight));
      const textOf = node => (node?.textContent || '').trim().replace(/\s+/g, ' ');
      const frameIdentity = `${location.origin}${location.pathname}#${Math.trunc(performance.timeOrigin)}`.slice(0, 180);
      const panels = Array.from(document.querySelectorAll('app-bet-control')).slice(0, 2);
      const panelObservations = [0, 1].map(slot => {
        const panel = panels[slot];
        if (!panel) return {panel_id: `PANEL_${slot + 1}`, panel_visible: false,
          button_visible: false, button_enabled: false, observed_at: now,
          source: 'AVIATOR_BETTING_PANEL_MUTATION_OBSERVER', frame_identity: frameIdentity};
        const block = panel.querySelector('.bet-block');
        const input = block?.querySelector('input[type="text"]:not([placeholder=""])');
        const actions = Array.from(panel.querySelectorAll('button, [role="button"], .btn'))
          .filter(node => !node.closest('.navigation-switcher'))
          .filter(visible).map(node => {
            const text = textOf(node);
            const aria = node.getAttribute('aria-label') || '';
            const title = node.getAttribute('title') || '';
            const combined = [aria, text, title].filter(Boolean).join(' ').replace(/\s+/g, ' ').trim();
            const main = combined.match(/^(bet|cancel|cash\s*out)\b/i)?.[1] || '';
            const style = window.getComputedStyle(node);
            return {node, text, aria, title, combined, main: main.replace(/\s+/g, ' '),
              visible: style.display !== 'none' && style.visibility !== 'hidden' && visible(node)};
          }).filter(item => item.visible && item.main);
        const action = actions[0] || null;
        const actionNode = action?.node || null;
        const combined = action?.combined || '';
        const style = actionNode ? window.getComputedStyle(actionNode) : null;
        const panelText = textOf(panel);
        const waitingText = panelText.match(/\bwaiting for (?:the )?next round\b/i)?.[0] || '';
        const currencyCodes = [...new Set([...`${panelText} ${combined}`.matchAll(/(?<![A-Z])([A-Z]{3})(?![A-Z])/g)]
          .map(match => match[1]).filter(code => !['BET', 'OUT'].includes(code)))];
        const label = action?.main.toLowerCase() === 'cash out' ? 'Cash Out'
          : action?.main ? action.main[0].toUpperCase() + action.main.slice(1).toLowerCase() : '';
        const attrs = {};
        if (actionNode) for (const name of ['id', 'class', 'data-state', 'data-status', 'aria-label']) {
          const value = actionNode.getAttribute(name);
          if (value) attrs[name] = value.slice(0, 160);
        }
        const panelAttributes = {};
        for (const name of ['id', 'data-panel-id', 'data-slot', 'data-index']) {
          const value = panel.getAttribute(name);
          if (value) panelAttributes[name] = value.slice(0, 120);
        }
        return {
          panel_id: `PANEL_${slot + 1}`, panel_visible: visible(panel),
          button_visible: Boolean(action && style && action.visible),
          button_enabled: Boolean(action && !actionNode.disabled
            && actionNode.getAttribute('aria-disabled') !== 'true'
            && !actionNode.classList.contains('disabled')),
          main_label: label, button_text: action?.text || action?.aria || action?.title || '',
          secondary_text: [combined.replace(/^(bet|cancel|cash\s*out)\b/i, '').replace(/\s+/g, ' ').trim(),
            waitingText].filter(Boolean).join(' ').slice(0, 160),
          aria_label: action?.aria || null, title: action?.title || null,
          button_attributes: attrs, panel_attributes: panelAttributes,
          button_color: style?.backgroundColor || null,
          displayed_stake_text: label === 'Bet' ? combined : (input?.value || ''),
          cashout_text: label === 'Cash Out' ? combined : '',
          visible_action_labels: actions.map(item => item.combined.slice(0, 120)),
          pending_indicator: Boolean(waitingText) || /\bwaiting for (?:the )?next round\b/i.test(combined),
          active_indicator: actions.some(item => /^cash\s*out$/i.test(item.main)),
          data_state: actionNode?.getAttribute('data-state') || panel.getAttribute('data-state') || null,
          currency_hint: currencyCodes.length === 1 ? currencyCodes[0] : null, observed_at: now,
          source: 'AVIATOR_BETTING_PANEL_MUTATION_OBSERVER', frame_identity: frameIdentity,
        };
      });
      const panelConfigurationObservations = [0, 1].map(slot => {
        const panel = panels[slot];
        if (!panel) return {panel_id: `PANEL_${slot + 1}`, panel_visible: false,
          auto_tab_selected: false, observed_at: now, frame_identity: frameIdentity,
          source: 'AVIATOR_BETTING_PANEL_CONFIGURATION_OBSERVER'};
        const activeTab = panel.querySelector('button.tab.active');
        const autoTabSelected = /^auto$/i.test(textOf(activeTab));
        const autoBetSwitch = panel.querySelector('.auto-bet app-ui-switcher .input-switch');
        const autoCashoutSwitch = panel.querySelector('.cash-out-switcher app-ui-switcher .input-switch');
        const stakeInput = panel.querySelector('.bet-block input[type="text"]');
        const cashoutInput = panel.querySelector('.cashout-block input[type="text"]');
        const betButton = panel.querySelector('button.bet');
        const currencyText = [textOf(panel), textOf(betButton)].filter(Boolean).join(' ');
        const configurationCurrencies = [...new Set([...currencyText.matchAll(/(?<![A-Z])([A-Z]{3})(?![A-Z])/g)]
          .map(match => match[1]).filter(code => !['BET', 'OUT'].includes(code)))];
        const switchObservation = (node, selector, input = null) => ({
          selector,
          visible: Boolean(node && visible(node)),
          class: typeof node?.className === 'string' ? node.className.slice(0, 100) : '',
          input_enabled: input ? !input.disabled : null,
          input_visible: Boolean(input && visible(input)),
          input_value: input?.value || null,
        });
        return {
          panel_id: `PANEL_${slot + 1}`, panel_visible: visible(panel), auto_tab_selected: autoTabSelected,
          observed_at: now, frame_identity: frameIdentity,
          source: 'AVIATOR_BETTING_PANEL_CONFIGURATION_OBSERVER',
          stake: {
            selector: '.bet-block input[type="text"]',
            value: stakeInput?.value || null,
            visible: Boolean(stakeInput && visible(stakeInput)),
            enabled: Boolean(stakeInput && !stakeInput.disabled),
          },
          auto_bet: switchObservation(autoBetSwitch, '.auto-bet app-ui-switcher .input-switch'),
          auto_cashout: switchObservation(autoCashoutSwitch,
            '.cash-out-switcher app-ui-switcher .input-switch', cashoutInput),
          currency_text: currencyText.slice(0, 180),
          currency_hint: configurationCurrencies.length === 1 ? configurationCurrencies[0] : null,
        };
      });
      const phaseSelector = '[class*="betting" i], [class*="bet-status" i], [data-phase], [data-testid*="betting" i], [role="alert"], [aria-live="assertive"]';
      const stageSelector = '[class*="stage" i]';
      const phaseNodes = Array.from(document.querySelectorAll(phaseSelector));
      const stageNodes = Array.from(document.querySelectorAll(stageSelector));
      const phaseHints = [...new Set([...phaseNodes, ...stageNodes])]
        .filter(visible).slice(0, 20).map(node => ({
          cls: String(node.className || '').slice(0, 90),
          txt: textOf(node).slice(0, 120),
          visible: true,
          source: stageNodes.includes(node) ? 'AVIATOR_STAGE_STATUS_DOM' : 'VISIBLE_GAME_PHASE_DOM',
          selector: stageNodes.includes(node) ? stageSelector : phaseSelector,
        })).filter(item => item.txt);
      const phaseText = phaseHints.map(item => item.txt).join(' ');
      const stageText = phaseHints.filter(item => item.source === 'AVIATOR_STAGE_STATUS_DOM')
        .map(item => item.txt).join(' ');
      const countdown = phaseText.match(/\b(?:betting|bets?)(?:\s+window)?\s+(?:closes|ends)\s+in\s+(\d+(?:\.\d+)?)\s*(?:s|sec|seconds?)\b/i);
      const countdownSeconds = countdown ? Number(countdown[1]) : null;
      const phase = (Number.isFinite(countdownSeconds) && countdownSeconds > 0 && countdownSeconds <= 300)
        || /\b(?:bets? open|betting open|place your bets|you can bet now)\b/i.test(phaseText)
        ? 'BETTING_OPEN'
        : (Number.isFinite(countdownSeconds) && countdownSeconds <= 0)
          || /\b(?:bets? closed|betting closed|no more bets|betting is over)\b/i.test(phaseText)
          ? 'BETTING_CLOSED' : 'UNKNOWN';
      const transitionText = /\b(?:ufc\s*(?:\/|vs\.?|versus)\s*aviator|aviator\s*(?:\/|vs\.?|versus)\s*ufc)\b|\b(?:ufc|aviator)\b.{0,32}\b(?:transition|intermission)\b/i;
      const roundPhase = /\bflew away\b|\b(?:round|game)\s+(?:is\s+)?(?:complete|completed|finished|crashed)\b/i.test(phaseText)
        ? 'ROUND_ENDED'
        : (transitionText.test(stageText)
          || /\b(?:waiting for (?:the )?next round|next round (?:starts|starting)|waiting for bets)\b/i.test(phaseText))
          ? 'INTERMISSION'
          : phase !== 'UNKNOWN' ? phase
            : (/\b(?:round|game)\s+(?:is\s+)?(?:in progress|running|flying)\b/i.test(stageText)
              || /^\s*\d+(?:[.,]\d+)?\s*x\s*$/i.test(stageText.trim()))
              ? 'ROUND_RUNNING' : 'UNKNOWN';
      return {
        phase, round_phase: roundPhase, observed_at: now, panel_observations: panelObservations,
        panel_configuration_observations: panelConfigurationObservations,
        phase_hints: phaseHints,
        evidence: {
          source: 'AVIATOR_EXPLICIT_BETTING_PHASE_DOM', selector: phaseSelector,
          phase_indicator_visible: phase !== 'UNKNOWN', explicit_window_state: phase,
          frame_identity: frameIdentity,
          text: phaseText.slice(0, 240),
          round_phase: roundPhase,
          phase_hints: phaseHints,
          stage_selector: stageSelector,
          stage_hints: phaseHints.filter(item => item.source === 'AVIATOR_STAGE_STATUS_DOM'),
          countdown_seconds: Number.isFinite(countdownSeconds) ? countdownSeconds : null,
          deadline_at: Number.isFinite(countdownSeconds) && countdownSeconds >= 0 && countdownSeconds <= 300
            ? new Date(Date.now() + countdownSeconds * 1000).toISOString() : null,
        },
      };
    }

    const first = document.querySelector(sel);
    const block = first?.closest('.payouts-block') || document.querySelector('.payouts-block');
    const target = block?.parentElement || block || document.body;
    if (!first || !block) return { ok: false, error: 'payout target not found' };

    if (window.__aviatorCollector?.observer) {
      window.__aviatorCollector.observer.disconnect();
      window.__aviatorCollector.phaseObserver?.disconnect();
      clearInterval(window.__aviatorCollector.phaseTimer);
      window.__aviatorCollector = null;
    }

    const c = { queue: [], waiter: null, lastSig: sig(readAll()), observer: null,
      phaseObserver: null, phaseTimer: null, lastPhaseSig: null };

    function publish(reason) {
      const mults = readAll();
      if (!mults.length) return;
      const nextSig = sig(mults);
      if (nextSig === c.lastSig) return;
      c.lastSig = nextSig;
      const ts = new Date().toISOString();
      const ev = { reason, ts, multipliers: mults, sig: nextSig, gamePhase: readGamePhase() };
      if (c.waiter) { const w = c.waiter; c.waiter = null; w(ev); return; }
      c.queue.push(ev);
      if (c.queue.length > 30) c.queue = c.queue.slice(-30);
    }

    function publishPhase(reason, force = false) {
      const phase = readGamePhase();
      const phaseSig = JSON.stringify({phase: phase.phase, round_phase: phase.round_phase, evidence: {
        source: phase.evidence.source,
        explicit_window_state: phase.evidence.explicit_window_state,
        text: phase.evidence.text,
        countdown_seconds: phase.evidence.countdown_seconds,
      },
        panels: phase.panel_observations.map(row => ({
          panel_id: row.panel_id, panel_visible: row.panel_visible,
          button_visible: row.button_visible, button_enabled: row.button_enabled,
          main_label: row.main_label,
          button_text: row.main_label === 'Cash Out' ? 'Cash Out' : row.button_text,
          secondary_text: row.main_label === 'Cash Out' ? '' : row.secondary_text,
          displayed_stake_text: row.displayed_stake_text,
          cashout_text: row.cashout_text,
          button_color: row.button_color, visible_action_labels: row.visible_action_labels.map(label =>
            /^cash\s*out\b/i.test(label) ? 'Cash Out' : label),
          pending_indicator: row.pending_indicator, active_indicator: row.active_indicator,
        })),
        panel_configurations: phase.panel_configuration_observations.map(row => ({
          panel_id: row.panel_id, panel_visible: row.panel_visible,
          auto_tab_selected: row.auto_tab_selected, stake: row.stake,
          auto_bet: row.auto_bet, auto_cashout: row.auto_cashout,
        })),
      });
      if (!force && phaseSig === c.lastPhaseSig) return;
      c.lastPhaseSig = phaseSig;
      const ev = { phaseOnly: true, reason, ts: phase.observed_at, gamePhase: phase };
      if (c.waiter) { const w = c.waiter; c.waiter = null; w(ev); return; }
      c.queue.push(ev);
      if (c.queue.length > 30) c.queue = c.queue.slice(-30);
    }

    const obs = new MutationObserver(mutations => {
      const relevant = mutations.some(m =>
        m.type === 'childList' || m.type === 'characterData' ||
        (m.type === 'attributes' && ['class','style','hidden'].includes(m.attributeName))
      );
      if (relevant) publish('mutation');
    });
    obs.observe(target, {
      childList: true, subtree: true, characterData: true,
      attributes: true, attributeFilter: ['class','style','hidden'],
    });
    c.observer = obs;
    const phaseObserver = new MutationObserver(() => publishPhase('betting-panel-mutation'));
    phaseObserver.observe(document.body, {
      childList: true, subtree: true, characterData: true,
      attributes: true, attributeFilter: ['class', 'style', 'hidden', 'disabled', 'aria-disabled'],
    });
    c.phaseObserver = phaseObserver;
    c.phaseTimer = setInterval(() => publishPhase('bounded-panel-state-refresh', true), refreshMs);
    window.__aviatorCollector = c;

    publishPhase('observer-installed');

    const mults = readAll();
    return { ok: true, count: mults.length, multipliers: mults, sig: sig(mults) };
  }, {selector, refresh_ms: Math.max(5000, PANEL_OBSERVATION_REFRESH_MS)});
}

async function _waitForMutation(frame, idleMs) {
  if (typeof frame.waitForCollectorMutation === 'function') {
    return frame.waitForCollectorMutation(idleMs);
  }
  return frame.evaluate((ms) => {
    const c = window.__aviatorCollector;
    if (!c) return { error: 'collector_not_installed' };
    if (c.queue.length > 0) return c.queue.shift();
    return new Promise(resolve => {
      let settled = false;
      const done = payload => {
        if (settled) return; settled = true;
        clearTimeout(timer);
        if (c.waiter === done) c.waiter = null;
        resolve(payload);
      };
      const timer = setTimeout(() => done({ timeout: true }), ms);
      c.waiter = done;
    });
  }, idleMs);
}

// ── Collector class ───────────────────────────────────────────────────────────

export class Collector {
  constructor() {
    this._lastPanelObservations = new Map();
  }

  async install(frame) {
    this._lastPanelObservations.clear();
    const result = await _installObserver(frame, PAYOUT_SELECTOR);
    if (!result?.ok) {
      throw new Error(result?.error || 'MutationObserver install failed');
    }
    const multipliers = result.multipliers.map(normalizeMultiplier).filter(v => v !== null);
    log.info(`Collector: observer installed — ${multipliers.length} rounds visible`);
    return { multipliers, signature: snapshotSignature(multipliers) };
  }

  async waitForMutation(frame, idleMs = MUTATION_IDLE_MS) {
    const raw = await _waitForMutation(frame, idleMs);
    if (raw?.error) return { error: raw.error };
    if (raw?.timeout) return { timeout: true };
    const multipliers = (raw?.multipliers ?? []).map(normalizeMultiplier).filter(v => v !== null);
    const gamePhase = raw?.gamePhase || null;
    const panelObservations = Array.isArray(gamePhase?.panel_observations)
      ? gamePhase.panel_observations.map(rawPanel => {
        const observed = classifyPanelObservation(rawPanel);
        const previous = this._lastPanelObservations.get(observed.panel_id);
        const transition = panelObservationTransition(previous, observed);
        observed.transition = transition;
        observed.transition_timestamp = transition.changed ? observed.observed_at : null;
        if (!transition.stale && !transition.duplicate) {
          this._lastPanelObservations.set(observed.panel_id, observed);
        }
        return observed;
      }) : [];
    const panelConfigurations = Array.isArray(gamePhase?.panel_configuration_observations)
      ? gamePhase.panel_configuration_observations.map(rawPanel =>
        classifyPanelConfigurationObservation(rawPanel)) : [];
    if (gamePhase && panelObservations.length) gamePhase.panel_observations = panelObservations;
    if (gamePhase && panelConfigurations.length) gamePhase.panel_configurations = panelConfigurations;
    return {
      multipliers,
      signature: snapshotSignature(multipliers),
      timestamp: raw?.ts || null,
      phaseOnly: raw?.phaseOnly === true,
      gamePhase,
    };
  }

  async isInstalled(frame) {
    try {
      return await frame.evaluate(() => !!window.__aviatorCollector?.observer);
    } catch (error) {
      log.debug(`Collector observer status could not be read from the current frame: ${error?.message || error}`);
      return false;
    }
  }
}
