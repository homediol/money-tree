/**
 * FrameManager.js — Always obtains a fresh Aviator iframe reference.
 * Never caches a frame across calls. Discards stale/detached frames immediately.
 *
 * The Spribe Aviator game loads in stages:
 *   1. Outer iframe appears (spribegaming.com) — detectable by URL hint
 *   2. Game bundle loads inside the iframe
 *   3. .payouts-block .payout elements appear
 */

import { EventEmitter } from 'node:events';
import { log } from './Logger.js';
import { sleep } from './RetryManager.js';
import { connectToCdpGameFrame } from './CdpGameFrame.js';

export const PAYOUT_SELECTOR = '.payouts-block .payout';

const FRAME_HINTS = ['spribe', 'spribegaming', 'aviator', 'crash', 'crash-games', 'turbo-games', 'game'];
const FRAME_TIMEOUT_MS  = Number(process.env.BOT_FRAME_TIMEOUT  || 120000);
const PAYOUT_TIMEOUT_MS = Number(process.env.BOT_PAYOUT_TIMEOUT || 30000);

export class FrameManager extends EventEmitter {
  constructor({ pageManager = null } = {}) {
    super();
    this.pageManager = pageManager;
    // Playwright frames are always resolved fresh. The direct CDP adapter is
    // retained only while its websocket/context remain alive.
    this._directFrame = null;
    this._lastReadiness = { frameAttached: false, contentLoaded: false, payoutsReady: false, reason: 'NOT_CHECKED' };
  }

  _emitReady(page, frame, method) {
    this.emit('ready', { page, frame, method, observed_at: new Date().toISOString() });
  }

  async _connectDirectFrame(page) {
    let parentId = null;
    let session = null;
    try {
      session = await page.context().newCDPSession(page);
      const info = await session.send('Target.getTargetInfo');
      parentId = info.targetInfo?.targetId || null;
    } catch (error) {
      // Target pairing is an optimisation; discovery still works without it.
      log.debug(`Direct CDP frame pairing is unavailable: ${error?.message || error}`);
    } finally {
      await session?.detach().catch(error => log.debug(`Temporary CDP session detach failed: ${error.message}`));
    }
    return connectToCdpGameFrame(undefined, parentId);
  }

  /** True if a frame reference is still usable. */
  isFrameAlive(frame) {
    if (!frame) return false;
    try {
      return !frame.isDetached();
    } catch (error) {
      log.debug(`Frame liveness check failed: ${error.message}`);
      return false;
    }
  }

  /** Verify a frame is alive AND has the payout selector. */
  async isFrameReady(frame) {
    if (!this.isFrameAlive(frame)) return false;
    try {
      await frame.locator(PAYOUT_SELECTOR).first().waitFor({ state: 'attached', timeout: 800 });
      return true;
    } catch (error) {
      log.debug(`Payout selector is not ready in the candidate frame: ${error.message}`);
      return false;
    }
  }

  _frameLooksLikeAviator(frame) {
    try {
      const haystack = `${frame.name()} ${frame.url()}`.toLowerCase();
      return FRAME_HINTS.some(h => haystack.includes(h));
    } catch (error) {
      log.debug(`Frame URL hint could not be read: ${error.message}`);
      return false;
    }
  }

  _frameIsChild(page, frame) {
    try {
      return frame !== page.mainFrame() && !frame.isDetached();
    } catch (error) {
      log.debug(`Frame parent relationship could not be read: ${error.message}`);
      return false;
    }
  }

  /** Scan all frames on the page and return the best Aviator frame candidate. */
  async _scan(page) {
    if (this.pageManager?.getAviatorFrame) {
      try {
        const sharedPage = await this.pageManager.getAviatorPage();
        if (sharedPage !== page) throw new Error('COLLECTOR_PAGE_IS_NOT_SHARED_AVIATOR_PAGE');
        const sharedFrame = await this.pageManager.getAviatorFrame();
        if (this._frameIsChild(page, sharedFrame)) {
          if (await this.isFrameReady(sharedFrame)) {
            this._lastReadiness = { frameAttached: true, contentLoaded: true, payoutsReady: true,
              reason: 'SHARED_GAME_FRAME_PAYOUT_SELECTOR_PRESENT' };
            return { frame: sharedFrame, method: 'payout-selector' };
          }
          this._lastReadiness = { frameAttached: true, contentLoaded: false, payoutsReady: false,
            reason: 'SHARED_GAME_FRAME_WAITING_FOR_PAYOUTS' };
          return { frame: sharedFrame, method: 'url-hint' };
        }
      } catch (error) {
        if (/SESSION_ENDED|MULTIPLE_ACTIVE_AVIATOR_FRAMES|COLLECTOR_PAGE_IS_NOT_SHARED|DUPLICATE_AVIATOR/.test(String(error?.message || error))) {
          throw error;
        }
      }
    }
    const frames = page.frames().filter(f => this._frameIsChild(page, f));

    // Prioritise frames that look like Aviator by URL/name
    const sorted = [
      ...frames.filter(f => this._frameLooksLikeAviator(f)),
      ...frames.filter(f => !this._frameLooksLikeAviator(f)),
    ];

    // Best: frame that already has payout elements
    for (const frame of sorted) {
      if (await this.isFrameReady(frame)) {
        this._lastReadiness = { frameAttached: true, contentLoaded: true, payoutsReady: true, reason: 'PAYOUT_SELECTOR_PRESENT' };
        return { frame, method: 'payout-selector' };
      }
    }

    // Fallback: frame that looks like Aviator even if payouts not yet visible
    const hinted = sorted.find(f => this._frameLooksLikeAviator(f));
    if (hinted && this.isFrameAlive(hinted)) {
      this._lastReadiness = { frameAttached: true, contentLoaded: false, payoutsReady: false, reason: 'FRAME_ATTACHED_WAITING_FOR_PAYOUTS' };
      return { frame: hinted, method: 'url-hint' };
    }

    this._lastReadiness = { frameAttached: false, contentLoaded: false, payoutsReady: false, reason: 'GAME_FRAME_NOT_FOUND' };
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
  async waitForFrame(page, { timeoutMs = FRAME_TIMEOUT_MS, signal, shouldInterrupt } = {}) {
    const deadline    = Date.now() + timeoutMs;
    let hintLogged = false;

    while (!signal?.aborted && Date.now() < deadline) {
      const interruption = shouldInterrupt?.();
      if (interruption) throw interruption instanceof Error ? interruption
        : Object.assign(new Error(String(interruption)), { code: 'NETWORK_UNAVAILABLE' });
      // Always re-scan — never use a cached reference
      const found = await this._scan(page).catch(error => {
        if (/SESSION_ENDED|DUPLICATE_AVIATOR/.test(String(error?.message || error))) throw error;
        log.debug(`Frame scan failed and will be retried: ${error.message}`);
        return null;
      });

      if (found?.method === 'payout-selector') {
        log.info(`FrameManager: frame ready (payout-selector) — ${this._describe(found.frame)}`);
        this._emitReady(page, found.frame, found.method);
        return found.frame;
      }

      // Chrome may expose Spribe as a separate OOPIF target without adding it
      // to Playwright's page.frames(). Use a target-local isolated world in
      // that case; it has the same evaluate/locator surface used downstream.
      if (!this.isFrameAlive(this._directFrame)) {
        this._directFrame = await this._connectDirectFrame(page).catch(error => {
          log.debug(`Direct game-frame connection failed and will be retried: ${error.message}`);
          return null;
        });
      }
      if (await this.isFrameReady(this._directFrame)) {
        this._lastReadiness = { frameAttached: true, contentLoaded: true, payoutsReady: true, reason: 'DIRECT_CDP_PAYOUT_SELECTOR_PRESENT' };
        log.info(`FrameManager: frame ready (direct-cdp) — ${this._describe(this._directFrame)}`);
        this._emitReady(page, this._directFrame, 'direct-cdp');
        return this._directFrame;
      }
      if (this.isFrameAlive(this._directFrame) && !this._lastReadiness.frameAttached) {
        this._lastReadiness = { frameAttached: true, contentLoaded: true, payoutsReady: false, reason: 'DIRECT_CDP_FRAME_WAITING_FOR_PAYOUTS' };
      }

      if (found?.method === 'url-hint') {
        if (!hintLogged) {
          hintLogged = true;
          log.info(`FrameManager: frame hinted — waiting for game to initialise`);
        }
      }

      const waitMs = Math.max(1, Math.min(found?.method === 'url-hint' ? 1000 : 2000, deadline - Date.now()));

      await Promise.race([
        page.waitForEvent('frameattached',  { timeout: waitMs }).catch(error => {
          if (!/timeout/i.test(error?.message || '')) log.debug(`Frame attach wait failed: ${error.message}`);
        }),
        page.waitForEvent('framenavigated', { timeout: waitMs }).catch(error => {
          if (!/timeout/i.test(error?.message || '')) log.debug(`Frame navigation wait failed: ${error.message}`);
        }),
        sleep(waitMs, signal),
      ]);
    }

    if (signal?.aborted) throw new Error('Aborted');
    const attached = this._lastReadiness.frameAttached === true;
    throw Object.assign(new Error(attached
      ? 'Aviator frame attached but payouts/history are unavailable'
      : 'Timed out waiting for Aviator game frame attachment'), {
      code: attached ? 'PAYOUTS_UNAVAILABLE' : 'GAME_FRAME_MISSING',
      reasonCode: attached ? 'PAYOUT_SOURCE_NOT_READY' : 'GAME_FRAME_NOT_ATTACHED',
      evidence: { ...this._lastReadiness, observedFrameCount: (() => {
        try { return page.frames().filter(frame => this._frameIsChild(page, frame)).length; }
        catch (error) { log.debug(`Frame count evidence unavailable: ${error.message}`); return null; }
      })() },
    });
  }

  /**
   * Wait for payout elements to appear inside a frame.
   */
  async waitForPayouts(frame, signal, { timeoutMs = PAYOUT_TIMEOUT_MS, shouldInterrupt } = {}) {
    const deadline = Date.now() + timeoutMs;
    while (!signal?.aborted && Date.now() < deadline) {
      const interruption = shouldInterrupt?.();
      if (interruption) throw interruption instanceof Error ? interruption
        : Object.assign(new Error(String(interruption)), { code: 'NETWORK_UNAVAILABLE' });
      if (!this.isFrameAlive(frame)) throw Object.assign(new Error('Frame detached while waiting for payouts'), { code: 'FRAME_DETACHED' });
      try {
        await frame.locator(PAYOUT_SELECTOR).first().waitFor({
          state: 'attached', timeout: Math.min(5000, Math.max(1, deadline - Date.now())),
        });
        this._lastReadiness = { frameAttached: true, contentLoaded: true, payoutsReady: true, reason: 'PAYOUT_SELECTOR_PRESENT' };
        return true;
      } catch (error) {
        log.debug(`Payout selector attachment is still waiting: ${error.message}`);
        await sleep(Math.min(1000, Math.max(1, deadline - Date.now())), signal);
      }
    }
    if (signal?.aborted) throw new Error('Aborted');
    this._lastReadiness = { frameAttached: this.isFrameAlive(frame), contentLoaded: true, payoutsReady: false, reason: 'PAYOUT_SOURCE_TIMEOUT' };
    throw Object.assign(new Error('Timed out waiting for payout elements'), {
      code: 'PAYOUTS_UNAVAILABLE', reasonCode: 'PAYOUT_SOURCE_TIMEOUT', evidence: { ...this._lastReadiness },
    });
  }

  readiness() { return { ...this._lastReadiness }; }

  _describe(frame) {
    try {
      const url = new URL(frame.url());
      return `name="${frame.name()}" url="${url.origin}${url.pathname}"`;
    } catch (error) {
      log.debug(`Frame diagnostic is unavailable after detach: ${error.message}`);
      return 'detached';
    }
  }
}
