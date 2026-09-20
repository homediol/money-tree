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
  constructor(url) {
    this.url = url;
    this.ws = null;
    this.nextId = 0;
    this.pending = new Map();
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
    ws.addEventListener('close', () => this._onClose());
  }

  _onMessage(raw) {
    let message;
    try {
      message = JSON.parse(typeof raw === 'string' ? raw : Buffer.from(raw).toString('utf8'));
    } catch {
      return;
    }
    const pending = this.pending.get(message.id);
    if (!pending) return;
    this.pending.delete(message.id);
    clearTimeout(pending.timer);
    if (message.error) pending.reject(new Error(`CDP ${pending.method}: ${message.error.message}`));
    else pending.resolve(message.result || {});
  }

  _onClose() {
    this.closed = true;
    for (const pending of this.pending.values()) {
      clearTimeout(pending.timer);
      pending.reject(new Error('CDP websocket closed'));
    }
    this.pending.clear();
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
    try { this.ws?.close(); } catch {}
    this._onClose();
  }
}

export class CdpGameFrame {
  constructor(target) {
    this.target = target;
    this.socket = new CdpSocket(target.webSocketDebuggerUrl);
    this.contextId = null;
  }

  async connect() {
    await this.socket.connect();
    await this.socket.call('Page.enable', {}, 10000);
    const tree = await this.socket.call('Page.getFrameTree', {}, 10000);
    const frame = findFrame(tree.frameTree, 'aviator-next');
    if (!frame?.id) throw new Error('aviator-next frame missing from aviaport target');
    const world = await this.socket.call('Page.createIsolatedWorld', {
      frameId: frame.id,
      worldName: 'winner-history-collector',
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

  /**
   * Poll the observer queue with Node timers. Chrome can throttle setTimeout
   * inside a background OOPIF, which would otherwise leave Runtime.evaluate
   * awaiting a page-side promise long after the collector's idle deadline.
   */
  async waitForCollectorMutation(idleMs) {
    const deadline = Date.now() + idleMs;
    while (Date.now() < deadline) {
      const event = await this.evaluate(() => {
        const collector = window.__aviatorCollector;
        if (!collector) return { error: 'collector_not_installed' };
        return collector.queue.length ? collector.queue.shift() : null;
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
