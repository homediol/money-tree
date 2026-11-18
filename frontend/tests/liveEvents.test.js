import test from 'node:test';
import assert from 'node:assert/strict';
import { subscribeLiveEvents } from '../src/services/liveEvents.js';

test('a closed socket from a removed subscriber cannot announce a false disconnect', () => {
  const sockets = [];
  const timers = new Set();
  class FakeWebSocket {
    static CONNECTING = 0;
    static OPEN = 1;
    static CLOSING = 2;
    static CLOSED = 3;
    readyState = FakeWebSocket.CONNECTING;
    constructor() { sockets.push(this); }
    close() { this.readyState = FakeWebSocket.CLOSING; }
  }
  globalThis.WebSocket = FakeWebSocket;
  globalThis.localStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
  globalThis.sessionStorage = { getItem: () => null, setItem: () => {}, removeItem: () => {} };
  globalThis.window = {
    location: { protocol: 'http:', host: '127.0.0.1:5174' },
    setTimeout: (callback) => { timers.add(callback); return callback; },
    clearTimeout: (timer) => timers.delete(timer),
  };

  const oldEvents = [];
  const unsubscribeOld = subscribeLiveEvents((frame) => oldEvents.push(frame));
  const staleSocket = sockets[0];
  unsubscribeOld();
  const activeEvents = [];
  const unsubscribeActive = subscribeLiveEvents((frame) => activeEvents.push(frame));
  const activeSocket = sockets[1];

  staleSocket.onclose();
  assert.deepEqual(activeEvents, []);
  activeSocket.onclose();
  assert.deepEqual(activeEvents.map((item) => item.status), ['disconnected']);

  unsubscribeActive();
  assert.equal(timers.size, 0);
  assert.deepEqual(oldEvents, []);
});
