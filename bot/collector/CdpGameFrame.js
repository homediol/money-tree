import { log } from './Logger.js';

/**
 * Minimal Playwright-compatible adapter for Spribe's out-of-process iframe.
 *
 * Some Chrome/CDP combinations expose the aviaport target through /json/list
 * but omit it from Playwright's page.frames(). Connecting to that target
 * directly and creating an isolated world in its aviator-next child keeps the
 * collector independent of cross-origin contentDocument access.
 */

const DEFAULT_ENDPOINT = process.env.BOT_CDP_ENDPOINT ||
  `http://127.0.0.1:${process.env.BOT_CDP_PORT || '9222'}`;
const CALL_TIMEOUT_MS = Number(process.env.BOT_CDP_CALL_TIMEOUT || 120000);
const MAX_BUFFERED_ROUND_EVENTS = 256;

function httpEndpoint(endpoint) {
  const parsed = new URL(endpoint);
  parsed.protocol = parsed.protocol === 'wss:' ? 'https:' : 'http:';
  parsed.pathname = '';
  parsed.search = '';
  parsed.hash = '';
  return parsed.toString().replace(/\/$/, '');
}

function findFrame(node, hint) {
  if (!node) return null;
  if ((node.frame?.url || '').includes(hint)) return node.frame;
  for (const child of node.childFrames || []) {
    const found = findFrame(child, hint);
    if (found) return found;
  }
  return null;
}

class CdpSocket {
  constructor(url, targetId = null) {
    this.url = url;
    this.targetId = targetId;
    this.ws = null;
    this.nextId = 0;
    this.pending = new Map();
    this.notificationHandlers = new Map();
    this.closed = true;
  }

  async connect() {
    const ws = new WebSocket(this.url);
    this.ws = ws;
    await new Promise((resolve, reject) => {
      const timer = setTimeout(() => reject(new Error('CDP websocket open timed out')), 5000);
      ws.addEventListener('open', () => {
        clearTimeout(timer);
        this.closed = false;
        resolve();
      }, { once: true });
      ws.addEventListener('error', () => {
        clearTimeout(timer);
        reject(new Error('CDP websocket open failed'));
      }, { once: true });
    });
    ws.addEventListener('message', event => this._onMessage(event.data));
    ws.addEventListener('close', event => this._onClose(event), { once: true });
  }

  _onMessage(raw) {
    let message;
    try {
      message = JSON.parse(typeof raw === 'string' ? raw : Buffer.from(raw).toString('utf8'));
    } catch (error) {
      log.debug(`Malformed CDP game-frame message ignored: ${error.message}`);
      return;
    }
    if (typeof message.method === 'string') {
      for (const handler of this.notificationHandlers.get(message.method) || []) {
        try { handler(message.params || {}); }
        catch (error) { log.debug(`CDP ${message.method} handler failed: ${error.message}`); }
      }
      return;
    }
    const pending = this.pending.get(message.id);
    if (!pending) return;
    this.pending.delete(message.id);
    clearTimeout(pending.timer);
    if (message.error) pending.reject(new Error(`CDP ${pending.method}: ${message.error.message}`));
    else pending.resolve(message.result || {});
  }

  onNotification(method, handler) {
    const handlers = this.notificationHandlers.get(method) || new Set();
    handlers.add(handler);
    this.notificationHandlers.set(method, handlers);
    return () => handlers.delete(handler);
  }

  _onClose(event = null) {
    this.closed = true;
    const target = this.targetId ? ` target=${this.targetId}` : '';
    const code = Number.isInteger(event?.code) ? ` code=${event.code}` : '';
    const clean = typeof event?.wasClean === 'boolean' ? ` clean=${event.wasClean}` : '';
    const message = `CDP websocket closed${target}${code}${clean}`;
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(new Error(message));
    }
    this.pending.clear();
    this.notificationHandlers.clear();
  }

  call(method, params = {}, timeoutMs = CALL_TIMEOUT_MS) {
    if (!this.ws || this.closed) return Promise.reject(new Error('CDP websocket is not open'));
    const id = ++this.nextId;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`CDP ${method} timed out`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer, method });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }

  close() {
    try { this.ws?.close(); }
    catch (error) { log.debug(`CDP game-frame socket was already closed: ${error.message}`); }
    this._onClose();
  }
}

export class CdpGameFrame {
  constructor(target) {
    this.target = target;
    this.socket = new CdpSocket(target.webSocketDebuggerUrl, target.id || null);
    this.contextId = null;
    this.worldName = 'winner-history-collector';
    this.collectorBindingName = `__winnerCollectorDispatch_${String(target.id || 'frame').replace(/[^a-zA-Z0-9_$]/g, '_')}_${Math.random().toString(36).slice(2)}`;
    this.eventDriven = false;
    this.maxQueuedEvents = MAX_BUFFERED_ROUND_EVENTS;
    this.eventQueue = [];
    this.eventQueueOverflow = false;
    this.latestPhaseEvent = null;
    this.eventWaiter = null;
    this.eventWaitTimer = null;
    this.socket.onNotification('Runtime.bindingCalled', payload => this._onCollectorBinding(payload));
  }

  async connect() {
    await this.socket.connect();
    await this.socket.call('Page.enable', {}, 10000);
    try {
      await this.socket.call('Runtime.enable', {}, 10000);
      await this.socket.call('Runtime.addBinding', { name: this.collectorBindingName }, 10000);
      this.eventDriven = true;
    } catch (error) {
      // Older Chromium builds can still use the existing in-frame queue. Keep
      // that path as a compatibility fallback; supported builds are pushed.
      this.collectorBindingName = null;
      log.warn(`CDP collector event binding unavailable; using compatibility polling: ${error.message}`);
    }
    const tree = await this.socket.call('Page.getFrameTree', {}, 10000);
    const frame = findFrame(tree.frameTree, 'aviator-next');
    if (!frame?.id) throw new Error('aviator-next frame missing from aviaport target');
    const world = await this.socket.call('Page.createIsolatedWorld', {
      frameId: frame.id,
      worldName: this.worldName,
      grantUniveralAccess: false,
    }, 10000);
    if (!world.executionContextId) throw new Error('aviator-next execution context unavailable');
    this.contextId = world.executionContextId;
    return this;
  }

  isDetached() {
    return this.socket.closed || !this.contextId;
  }

  name() {
    return 'aviator-next-cdp';
  }

  url() {
    return this.target.url || '';
  }

  _onCollectorBinding(payload) {
    if (payload?.name !== this.collectorBindingName
        || payload.executionContextId !== this.contextId
        || typeof payload.payload !== 'string') return;
    let event;
    try { event = JSON.parse(payload.payload); }
    catch (error) {
      log.debug(`Malformed collector CDP event ignored: ${error.message}`);
      return;
    }
    if (!event || typeof event !== 'object') return;
    if (event.phaseOnly === true) this.latestPhaseEvent = event;
    else if (this.eventQueue.length < this.maxQueuedEvents) this.eventQueue.push(event);
    else {
      if (!this.eventQueueOverflow) {
        log.error(`CDP collector event queue reached ${this.maxQueuedEvents}; forcing snapshot recovery`);
      }
      this.eventQueueOverflow = true;
    }
    this._resolveEventWaiter();
  }

  _takeCollectorEvent() {
    if (this.eventQueue.length) return this.eventQueue.shift();
    if (this.eventQueueOverflow) {
      this.eventQueueOverflow = false;
      return { error: 'collector_event_queue_overflow' };
    }
    if (this.latestPhaseEvent) {
      const event = this.latestPhaseEvent;
      this.latestPhaseEvent = null;
      return event;
    }
    return null;
  }

  _resolveEventWaiter() {
    if (!this.eventWaiter) return;
    const event = this._takeCollectorEvent();
    if (!event) return;
    const resolve = this.eventWaiter;
    this.eventWaiter = null;
    clearTimeout(this.eventWaitTimer);
    this.eventWaitTimer = null;
    resolve(event);
  }

  async evaluate(pageFunction, arg) {
    if (this.isDetached()) throw new Error('CDP game frame is detached');
    const source = typeof pageFunction === 'function' ? pageFunction.toString() : String(pageFunction);
    const expression = `(${source})(${JSON.stringify(arg)})`;
    try {
      const response = await this.socket.call('Runtime.evaluate', {
        expression,
        contextId: this.contextId,
        awaitPromise: true,
        returnByValue: true,
      });
      if (response.exceptionDetails) {
        const detail = response.exceptionDetails.exception?.description ||
          response.exceptionDetails.text || 'page-side exception';
        throw new Error(detail);
      }
      return response.result?.value;
    } catch (error) {
      if (/context|target|closed|detached|session/i.test(error.message)) this.close();
      throw error;
    }
  }

  /** Await pushed MutationObserver events without polling Runtime.evaluate. */
  async waitForCollectorMutation(idleMs) {
    if (!this.eventDriven) return this._waitForCollectorMutationPolling(idleMs);
    const ready = this._takeCollectorEvent();
    if (ready) return ready;
    if (this.isDetached()) return { error: 'collector_frame_detached' };
    return new Promise(resolve => {
      this.eventWaiter = resolve;
      this.eventWaitTimer = setTimeout(() => {
        this.eventWaitTimer = null;
        this.eventWaiter = null;
        resolve({ timeout: true });
      }, Math.max(0, Number(idleMs) || 0));
      // A binding event may have arrived between the initial queue read and
      // installing the waiter; check once more to close that race.
      this._resolveEventWaiter();
    });
  }

  async _waitForCollectorMutationPolling(idleMs) {
    const deadline = Date.now() + Math.max(0, Number(idleMs) || 0);
    while (Date.now() < deadline) {
      const event = await this.evaluate(() => {
        const collector = window.__aviatorCollector;
        if (!collector) return { error: 'collector_not_installed' };
        if (collector.queue.length) return collector.queue.shift();
        if (collector.queueOverflow) return { error: 'collector_page_event_queue_overflow' };
        if (collector.latestPhaseEvent) {
          const phaseEvent = collector.latestPhaseEvent;
          collector.latestPhaseEvent = null;
          return phaseEvent;
        }
        return null;
      });
      if (event) return event;
      await new Promise(resolve => setTimeout(resolve, 250));
    }
    return { timeout: true };
  }

  locator(selector) {
    const frame = this;
    return {
      first() {
        return {
          async waitFor({ state = 'attached', timeout = 30000 } = {}) {
            if (state !== 'attached') throw new Error(`Unsupported locator state: ${state}`);
            const deadline = Date.now() + timeout;
            while (Date.now() < deadline) {
              if (await frame.evaluate(sel => !!document.querySelector(sel), selector)) return;
              await new Promise(resolve => setTimeout(resolve, 100));
            }
            throw new Error(`Timeout waiting for selector: ${selector}`);
          },
        };
      },
    };
  }

  close() {
    this.contextId = null;
    clearTimeout(this.eventWaitTimer);
    this.eventWaitTimer = null;
    if (this.eventWaiter) {
      const resolve = this.eventWaiter;
      this.eventWaiter = null;
      resolve({ error: 'collector_frame_detached' });
    }
    this.socket.close();
  }
}

export async function connectToCdpGameFrame(endpoint = DEFAULT_ENDPOINT, parentId = null) {
  const response = await fetch(`${httpEndpoint(endpoint)}/json/list`, { signal: AbortSignal.timeout(4000) });
  if (!response.ok) throw new Error(`CDP target list returned HTTP ${response.status}`);
  const targets = await response.json();
  const candidates = targets.filter(target =>
    target.type === 'iframe' &&
    String(target.url || '').includes('aviaport.spribegaming.com') &&
    target.webSocketDebuggerUrl
  );
  if (!candidates.length) throw new Error('aviaport CDP target not found');
  const ordered = parentId
    ? [...candidates.filter(target => target.parentId === parentId),
       ...candidates.filter(target => target.parentId !== parentId)]
    : candidates;

  let lastError = null;
  for (const target of ordered) {
    const frame = new CdpGameFrame(target);
    try {
      return await frame.connect();
    } catch (error) {
      lastError = error;
      frame.close();
    }
  }
  throw lastError || new Error('could not connect to an aviaport target');
}
