import http from 'node:http';
import https from 'node:https';
import { StringDecoder } from 'node:string_decoder';

const MAX_RESPONSE_BYTES = 8 * 1024 * 1024;
const MAX_STREAM_EVENT_BYTES = 8 * 1024 * 1024;
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
const isCursor = value => typeof value === 'string' && /^\d{1,19}$/.test(value)
  && BigInt(value) <= 9223372036854775807n;

export class OrbitApiError extends Error {
  constructor(code, { status = null, retryable = false } = {}) {
    super(`Orbit API request failed: ${code}`);
    this.code = code;
    this.status = status;
    this.retryable = retryable;
  }
}

/** Backend-only, read-only transport for the Orbit Data Watch API. */
export class OrbitDataWatchClient {
  #apiKey;

  constructor({ baseUrl, apiKey, connectTimeoutMs = 5000, requestTimeoutMs = 15000,
    maxAttempts = 3, retryBaseMs = 250 } = {}) {
    let url;
    try { url = new URL(baseUrl); } catch { throw new OrbitApiError('INVALID_BASE_URL'); }
    if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password
        || url.search || url.hash || (url.protocol === 'http:'
          && !['localhost', '127.0.0.1', '[::1]'].includes(url.hostname))) {
      throw new OrbitApiError('INVALID_BASE_URL');
    }
    if (typeof apiKey !== 'string' || !apiKey.trim() || /[\r\n]/.test(apiKey)) {
      throw new OrbitApiError('API_KEY_REQUIRED');
    }
    this.base = url.toString().replace(/\/+$/, '');
    this.#apiKey = apiKey.trim();
    this.connectTimeoutMs = Math.max(100, Number(connectTimeoutMs) || 5000);
    this.requestTimeoutMs = Math.max(100, Number(requestTimeoutMs) || 15000);
    this.maxAttempts = Math.min(10, Math.max(1, Number(maxAttempts) || 3));
    this.retryBaseMs = Math.max(1, Number(retryBaseMs) || 250);
  }

  async request(path) {
    for (let attempt = 1; ; attempt++) {
      try { return await this._requestOnce(path); }
      catch (error) {
        if (!(error instanceof OrbitApiError) || !error.retryable || attempt >= this.maxAttempts) throw error;
        await sleep(Math.min(10000, this.retryBaseMs * 2 ** (attempt - 1)));
      }
    }
  }

  _requestOnce(path) {
    const url = new URL(`${this.base}${path}`);
    return new Promise((resolve, reject) => {
      let finished = false;
      let connectTimer;
      let requestTimer;
      const finish = (error, result) => {
        if (finished) return;
        finished = true;
        clearTimeout(connectTimer);
        clearTimeout(requestTimer);
        if (error) reject(error); else resolve(result);
      };
      const req = (url.protocol === 'https:' ? https : http).get(url, {
        headers: { authorization: `Bearer ${this.#apiKey}`, accept: 'application/json' },
      }, response => {
        clearTimeout(connectTimer);
        const status = response.statusCode || 0;
        if (status !== 200) {
          response.resume();
          finish(new OrbitApiError('HTTP_ERROR', { status,
            retryable: status === 408 || status === 429 || status >= 500 }));
          return;
        }
        let length = 0;
        const chunks = [];
        response.on('data', chunk => {
          length += chunk.length;
          if (length > MAX_RESPONSE_BYTES) {
            finish(new OrbitApiError('RESPONSE_TOO_LARGE'));
            req.destroy();
          } else chunks.push(chunk);
        });
        response.on('error', () => finish(new OrbitApiError('RESPONSE_ERROR', { retryable: true })));
        response.on('end', () => {
          if (finished) return;
          try {
            const data = JSON.parse(Buffer.concat(chunks).toString('utf8'));
            if (!data || typeof data !== 'object' || Array.isArray(data)) throw new Error();
            finish(null, data);
          } catch { finish(new OrbitApiError('INVALID_JSON')); }
        });
      });
      req.on('socket', socket => {
        if (!socket.connecting) return;
        connectTimer = setTimeout(() => {
          finish(new OrbitApiError('CONNECT_TIMEOUT', { retryable: true }));
          req.destroy();
        }, this.connectTimeoutMs);
        socket.once(url.protocol === 'https:' ? 'secureConnect' : 'connect', () => clearTimeout(connectTimer));
      });
      requestTimer = setTimeout(() => {
        finish(new OrbitApiError('REQUEST_TIMEOUT', { retryable: true }));
        req.destroy();
      }, this.requestTimeoutMs);
      req.on('error', () => finish(new OrbitApiError('NETWORK_ERROR', { retryable: true })));
    });
  }

  listWatches() { return this.request('/api/data-watches'); }
  latest(watchId, { windowSize = null } = {}) {
    const query = Number.isInteger(windowSize) ? `?windowSize=${windowSize}` : '';
    return this.request(`/api/data-watches/${encodeURIComponent(watchId)}/latest${query}`);
  }
  history(watchId, after = '0', limit = 100) {
    if (!isCursor(after)
        || !Number.isInteger(limit) || limit < 1 || limit > 500) throw new OrbitApiError('INVALID_CURSOR');
    return this.request(`/api/data-watches/${encodeURIComponent(watchId)}/history?after=${after}&limit=${limit}`);
  }

  /** Hold an authenticated owner stream open until it closes or is aborted. */
  stream(watchId, after = '0', { signal, onOpen = () => {}, onEvent = () => {}, windowSize = null } = {}) {
    if (!isCursor(after)) throw new OrbitApiError('INVALID_CURSOR');
    const query = new URLSearchParams({ after });
    if (Number.isInteger(windowSize)) query.set('windowSize', String(windowSize));
    const url = new URL(`${this.base}/api/data-watches/${encodeURIComponent(watchId)}/stream?${query}`);
    return new Promise((resolve, reject) => {
      let settled = false;
      let connectTimer;
      let requestTimer;
      let idleTimer;
      let eventName = '';
      let eventId = '';
      let dataLines = [];
      let lineBuffer = '';
      let eventBytes = 0;
      let eventChain = Promise.resolve();
      const decoder = new StringDecoder('utf8');
      const finish = error => {
        if (settled) return;
        settled = true;
        clearTimeout(connectTimer);
        clearTimeout(requestTimer);
        clearTimeout(idleTimer);
        signal?.removeEventListener('abort', onAbort);
        if (error) reject(error); else resolve();
      };
      const timeout = code => {
        finish(new OrbitApiError(code, { retryable: true }));
        req.destroy();
      };
      const onAbort = () => {
        req.destroy();
        finish();
      };
      if (signal?.aborted) { resolve(); return; }

      const req = (url.protocol === 'https:' ? https : http).get(url, {
        headers: { authorization: `Bearer ${this.#apiKey}`, accept: 'text/event-stream',
          'last-event-id': after },
      }, response => {
        clearTimeout(connectTimer);
        clearTimeout(requestTimer);
        if (response.statusCode !== 200) {
          const status = response.statusCode || 0;
          response.resume();
          finish(new OrbitApiError('HTTP_ERROR', { status,
            retryable: status === 408 || status === 429 || status >= 500 }));
          return;
        }
        if (!String(response.headers['content-type'] || '').toLowerCase().startsWith('text/event-stream')) {
          response.resume();
          finish(new OrbitApiError('INVALID_STREAM_CONTENT_TYPE'));
          return;
        }
        try { onOpen(); }
        catch { finish(new OrbitApiError('STREAM_HANDLER_FAILED', { retryable: true })); response.destroy(); return; }
        const armIdleTimer = () => {
          clearTimeout(idleTimer);
          idleTimer = setTimeout(() => timeout('STREAM_IDLE_TIMEOUT'), 30000);
          idleTimer.unref?.();
        };
        const dispatch = () => {
          if (!dataLines.length) {
            eventName = '';
            eventId = '';
            eventBytes = 0;
            return;
          }
          let data;
          try { data = JSON.parse(dataLines.join('\n')); }
          catch { finish(new OrbitApiError('INVALID_STREAM_EVENT')); response.destroy(); return; }
          const type = eventName || 'message';
          if (type === 'reading' && (!isCursor(eventId) || data?.id !== eventId || data?.watchId !== watchId)) {
            finish(new OrbitApiError('INVALID_STREAM_READING'));
            response.destroy();
            return;
          }
          if (type === 'status' && data?.watchId !== watchId) {
            finish(new OrbitApiError('INVALID_STREAM_STATUS'));
            response.destroy();
            return;
          }
          // Capture receipt when the complete SSE event is decoded, before it
          // waits behind any earlier asynchronous event handler.
          const message = { type, id: eventId || null, data, receivedAt: new Date().toISOString(),
            receivedMonotonicMs: performance.now() };
          eventChain = eventChain.then(() => onEvent(message));
          eventChain.catch(error => {
            finish(error instanceof OrbitApiError ? error : new OrbitApiError('STREAM_HANDLER_FAILED', { retryable: true }));
            response.destroy();
          });
          eventName = '';
          eventId = '';
          dataLines = [];
          eventBytes = 0;
        };
        const consumeLine = line => {
          eventBytes += Buffer.byteLength(line, 'utf8') + 1;
          if (eventBytes > MAX_STREAM_EVENT_BYTES) {
            finish(new OrbitApiError('STREAM_EVENT_TOO_LARGE'));
            response.destroy();
            return false;
          }
          if (line === '') { dispatch(); return true; }
          if (line.startsWith(':')) return true;
          const separator = line.indexOf(':');
          const field = separator < 0 ? line : line.slice(0, separator);
          const value = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /, '');
          if (field === 'event') eventName = value;
          else if (field === 'id' && !value.includes('\0')) eventId = value;
          else if (field === 'data') dataLines.push(value);
          return true;
        };

        armIdleTimer();
        response.on('data', chunk => {
          armIdleTimer();
          lineBuffer += decoder.write(chunk);
          let newline;
          while ((newline = lineBuffer.indexOf('\n')) >= 0) {
            let line = lineBuffer.slice(0, newline);
            lineBuffer = lineBuffer.slice(newline + 1);
            if (line.endsWith('\r')) line = line.slice(0, -1);
            if (!consumeLine(line)) return;
            if (settled) return;
          }
          if (Buffer.byteLength(lineBuffer, 'utf8') + eventBytes > MAX_STREAM_EVENT_BYTES) {
            finish(new OrbitApiError('STREAM_EVENT_TOO_LARGE'));
            response.destroy();
          }
        });
        response.on('end', () => {
          const trailing = lineBuffer + decoder.end();
          if (trailing && !consumeLine(trailing.replace(/\r$/, ''))) return;
          if (settled) return;
          dispatch();
          eventChain.then(
            () => finish(new OrbitApiError('STREAM_DISCONNECTED', { retryable: true })),
            () => finish(new OrbitApiError('STREAM_DISCONNECTED', { retryable: true })),
          );
        });
        response.on('error', () => finish(new OrbitApiError('STREAM_DISCONNECTED', { retryable: true })));
        response.on('close', () => {
          if (!settled) eventChain.then(
            () => finish(new OrbitApiError('STREAM_DISCONNECTED', { retryable: true })),
            () => finish(new OrbitApiError('STREAM_DISCONNECTED', { retryable: true })),
          );
        });
      });
      req.on('socket', socket => {
        if (!socket.connecting) return;
        connectTimer = setTimeout(() => timeout('CONNECT_TIMEOUT'), this.connectTimeoutMs);
        socket.once(url.protocol === 'https:' ? 'secureConnect' : 'connect', () => clearTimeout(connectTimer));
      });
      requestTimer = setTimeout(() => timeout('REQUEST_TIMEOUT'), this.requestTimeoutMs);
      requestTimer.unref?.();
      req.on('error', () => {
        if (!settled) finish(new OrbitApiError('NETWORK_ERROR', { retryable: true }));
      });
      signal?.addEventListener('abort', onAbort, { once: true });
    });
  }
}
