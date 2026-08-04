/**
 * FrameManager.js — Always obtains a fresh Aviator iframe reference.
 * Never caches a frame across calls. Discards stale/detached frames immediately.
 *
 * The Spribe Aviator game loads in stages:
 *   1. Outer iframe appears (spribegaming.com) — detectable by URL hint
 *   2. Game bundle loads inside the iframe
 *   3. .payouts-block .payout elements appear
 */

import { log } from './Logger.js';
import { sleep } from './RetryManager.js';

export const PAYOUT_SELECTOR = '.payouts-block .payout';

const FRAME_HINTS = ['spribe', 'spribegaming', 'aviator', 'crash', 'crash-games', 'turbo-games', 'game'];
const FRAME_TIMEOUT_MS  = Number(process.env.BOT_FRAME_TIMEOUT  || 120000);
const PAYOUT_TIMEOUT_MS = Number(process.env.BOT_PAYOUT_TIMEOUT || 90000);

export class FrameManager {
  constructor() {
    // No cached frame — always resolve fresh
  }

  /** True if a frame reference is still usable. */
  isFrameAlive(frame) {
    if (!frame) return false;
    try {
      return !frame.isDetached();
    } catch {
      return false;
    }
  }

  /** Verify a frame is alive AND has the payout selector. */
  async isFrameReady(frame) {
    if (!this.isFrameAlive(frame)) return false;
    try {
      await frame.locator(PAYOUT_SELECTOR).first().waitFor({ state: 'attached', timeout: 800 });
      return true;
    } catch {
      return false;
    }
  }

  _frameLooksLikeAviator(frame) {
    try {
      const haystack = `${frame.name()} ${frame.url()}`.toLowerCase();
      return FRAME_HINTS.some(h => haystack.includes(h));
    } catch {
      return false;
    }
  }

  _frameIsChild(page, frame) {
    try {
      return frame !== page.mainFrame() && !frame.isDetached();
    } catch {
      return false;
    }
  }

  /** Scan all frames on the page and return the best Aviator frame candidate. */
  async _scan(page) {
    const frames = page.frames().filter(f => this._frameIsChild(page, f));

    // Prioritise frames that look like Aviator by URL/name
    const sorted = [
      ...frames.filter(f => this._frameLooksLikeAviator(f)),
      ...frames.filter(f => !this._frameLooksLikeAviator(f)),
    ];

    // Best: frame that already has payout elements
    for (const frame of sorted) {
      if (await this.isFrameReady(frame)) {
        return { frame, method: 'payout-selector' };
      }
    }

    // Fallback: frame that looks like Aviator even if payouts not yet visible
    const hinted = sorted.find(f => this._frameLooksLikeAviator(f));
    if (hinted && this.isFrameAlive(hinted)) {
      return { frame: hinted, method: 'url-hint' };
    }

    return null;
  }

  /**
   * Wait until a usable Aviator frame is available.
   * Returns a fresh Frame object every time — never a cached reference.
   *
   * @param {Page} page
   * @param {object} opts
   * @param {number} [opts.timeoutMs]
   * @param {AbortSignal} [opts.signal]
   * @returns {Promise<Frame>}
   */
  async waitForFrame(page, { timeoutMs = FRAME_TIMEOUT_MS, signal } = {}) {
    const deadline    = Date.now() + timeoutMs;
    const HINT_GRACE  = 20000; // ms to wait after first URL hint for payouts to appear
    let hintedFrame   = null;
    let hintDeadline  = 0;

    while (!signal?.aborted && Date.now() < deadline) {
      // Always re-scan — never use a cached reference
      const found = await this._scan(page).catch(() => null);

      if (found?.method === 'payout-selector') {
        log.info(`FrameManager: frame ready (payout-selector) — ${this._describe(found.frame)}`);
        return found.frame;
      }

      if (found?.method === 'url-hint') {
        if (!hintedFrame || !this.isFrameAlive(hintedFrame)) {
          hintedFrame  = found.frame;
          hintDeadline = Date.now() + HINT_GRACE;
          log.info(`FrameManager: frame hinted — waiting for game to initialise`);
        }
        if (Date.now() >= hintDeadline) {
          log.info(`FrameManager: returning hinted frame after grace period`);
          return hintedFrame;
        }
      }

      const waitMs = hintedFrame
        ? Math.min(1000, Math.max(0, hintDeadline - Date.now()))
        : 2000;

      await Promise.race([
        page.waitForEvent('frameattached',  { timeout: waitMs }).catch(() => {}),
        page.waitForEvent('framenavigated', { timeout: waitMs }).catch(() => {}),
        sleep(waitMs, signal),
      ]);
    }

    if (hintedFrame && this.isFrameAlive(hintedFrame)) {
      log.info('FrameManager: returning hinted frame (deadline fallback)');
      return hintedFrame;
    }

    throw new Error('Timed out waiting for Aviator iframe');
  }

  /**
   * Wait for payout elements to appear inside a frame.
   */
  async waitForPayouts(frame, signal) {
    const deadline = Date.now() + PAYOUT_TIMEOUT_MS;
    while (!signal?.aborted && Date.now() < deadline) {
      if (!this.isFrameAlive(frame)) throw new Error('Frame detached while waiting for payouts');
      try {
        await frame.locator(PAYOUT_SELECTOR).first().waitFor({ state: 'attached', timeout: 5000 });
        return true;
      } catch {
        await sleep(1000, signal);
      }
    }
    throw new Error('Timed out waiting for payout elements');
  }

  _describe(frame) {
    try { return `name="${frame.name()}" url="${frame.url().slice(0, 80)}"`; }
    catch { return 'detached'; }
  }
}
