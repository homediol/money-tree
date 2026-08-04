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

    const first = document.querySelector(sel);
    const block = first?.closest('.payouts-block') || document.querySelector('.payouts-block');
    const target = block?.parentElement || block || document.body;
    if (!first || !block) return { ok: false, error: 'payout target not found' };

    if (window.__aviatorCollector?.observer) {
      window.__aviatorCollector.observer.disconnect();
      window.__aviatorCollector = null;
    }

    const c = { queue: [], waiter: null, lastSig: sig(readAll()), observer: null };

    function publish(reason) {
      const mults = readAll();
      if (!mults.length) return;
      const nextSig = sig(mults);
      if (nextSig === c.lastSig) return;
      c.lastSig = nextSig;
      const ev = { reason, ts: new Date().toISOString(), multipliers: mults, sig: nextSig };
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
    window.__aviatorCollector = c;

    const mults = readAll();
    return { ok: true, count: mults.length, multipliers: mults, sig: sig(mults) };
  }, selector);
}

async function _waitForMutation(frame, idleMs) {
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
    return { multipliers, signature: snapshotSignature(multipliers) };
  }

  async isInstalled(frame) {
    try {
      return await frame.evaluate(() => !!window.__aviatorCollector?.observer);
    } catch {
      return false;
    }
  }
}
