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

const MUTATION_IDLE_MS = Number(process.env.BOT_IDLE_TIMEOUT || 60000);

// ── In-frame code (evaluated directly inside the iframe) ────────────────────
// These are plain function expressions passed to frame.evaluate().
// Playwright serialises the function body and runs it in the frame context.
// Arguments are passed as the second parameter to frame.evaluate().

async function _installObserver(frame, selector) {
  return frame.evaluate((sel) => {
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
      const block = document.querySelector('.bet-block');
      const button = block?.querySelector('.btn-success.bet');
      const input = block?.querySelector('input[type="text"]:not([placeholder=""])');
      const style = button ? window.getComputedStyle(button) : null;
      const visible = Boolean(button && style && style.display !== 'none'
        && style.visibility !== 'hidden' && (button.offsetWidth || button.offsetHeight));
      const disabled = Boolean(button && (button.disabled
        || button.getAttribute('aria-disabled') === 'true'
        || button.classList.contains('disabled')));
      const inputVisible = Boolean(input && (input.offsetWidth || input.offsetHeight));
      const tabs = block ? Array.from(block.querySelectorAll('.tab')).map(el =>
        (el.textContent || '').trim().toLowerCase()) : [];
      const betTabActive = tabs.includes('bet');
      const phase = visible && inputVisible && betTabActive
        ? (disabled ? 'BETTING_CLOSED' : 'BETTING_OPEN') : 'UNKNOWN';
      return {
        phase,
        observed_at: new Date().toISOString(),
        evidence: {
          source: 'AVIATOR_BETTING_PANEL_READ_ONLY',
          selector: '.bet-block .btn-success.bet',
          button_visible: visible,
          button_enabled: visible && !disabled,
          stake_input_visible: inputVisible,
          bet_tab_active: betTabActive,
          button_text: button ? (button.textContent || '').trim().slice(0, 40) : null,
          button_class: button ? String(button.className || '').slice(0, 160) : null,
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
      window.__aviatorCollector = null;
    }

    const c = { queue: [], waiter: null, lastSig: sig(readAll()), observer: null,
      phaseObserver: null, lastPhaseSig: null };

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

    function publishPhase(reason) {
      const phase = readGamePhase();
      const phaseSig = JSON.stringify({
        phase: phase.phase, evidence: phase.evidence,
      });
      if (phaseSig === c.lastPhaseSig) return;
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
    window.__aviatorCollector = c;

    publishPhase('observer-installed');

    const mults = readAll();
    return { ok: true, count: mults.length, multipliers: mults, sig: sig(mults) };
  }, selector);
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
  async install(frame) {
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
    return {
      multipliers,
      signature: snapshotSignature(multipliers),
      timestamp: raw?.ts || null,
      phaseOnly: raw?.phaseOnly === true,
      gamePhase: raw?.gamePhase || null,
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
