import assert from 'node:assert/strict';
import test from 'node:test';
import { forwardVerifiedProxyClientAddress } from '../vite.config.js';

test('Vite forwards its observed client address for HTTP and WebSocket proxy requests', () => {
  const handlers = new Map();
  forwardVerifiedProxyClientAddress({ on: (event, handler) => handlers.set(event, handler) });

  for (const event of ['proxyReq', 'proxyReqWs']) {
    let forwardedAddress = null;
    handlers.get(event)(
      { setHeader: (name, value) => { if (name === 'X-Winner-Proxy-Client') forwardedAddress = value; } },
      { socket: { remoteAddress: '192.0.2.25' } },
    );
    assert.equal(forwardedAddress, '192.0.2.25', event);
  }
});
