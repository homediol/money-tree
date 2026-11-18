import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';
import { OrbitDataWatchClient } from '../collector/OrbitDataWatchClient.js';

async function serverFor(t, handler) {
  const server = http.createServer(handler);
  await new Promise(resolve => server.listen(0, '127.0.0.1', resolve));
  t.after(() => new Promise(resolve => server.close(resolve)));
  return `http://127.0.0.1:${server.address().port}`;
}

test('sends bearer authorization and preserves large string cursors', async t => {
  const requests = [];
  const baseUrl = await serverFor(t, (req, res) => {
    requests.push({ path: req.url, auth: req.headers.authorization });
    res.setHeader('content-type', 'application/json');
    res.end(JSON.stringify({ ok: true }));
  });
  const client = new OrbitDataWatchClient({ baseUrl, apiKey: 'test-key' });
  await client.listWatches();
  await client.latest('watch', { windowSize: 12 });
  await client.history('watch', '9007199254740993', 100);
  assert.deepEqual(requests.map(x => x.path), ['/api/data-watches', '/api/data-watches/watch/latest?windowSize=12',
    '/api/data-watches/watch/history?after=9007199254740993&limit=100']);
  assert.ok(requests.every(x => x.auth === 'Bearer test-key'));
  assert.throws(() => client.history('watch', '9223372036854775808'), { code: 'INVALID_CURSOR' });
});

test('retries transient errors and does not retry invalid credentials', async t => {
  let calls = 0;
  const baseUrl = await serverFor(t, (_req, res) => {
    calls++;
    if (calls < 3) { res.writeHead(503).end(); return; }
    if (calls === 4) { res.writeHead(401).end(); return; }
    res.end('{}');
  });
  const client = new OrbitDataWatchClient({ baseUrl, apiKey: 'test-key', retryBaseMs: 1 });
  await client.listWatches();
  assert.equal(calls, 3);
  await assert.rejects(client.listWatches(), error => error.status === 401 && !error.message.includes('test-key'));
  assert.equal(calls, 4);
});

test('bounds request time and rejects invalid responses without revealing the key', async t => {
  const baseUrl = await serverFor(t, (_req, res) => setTimeout(() => res.end('not json'), 150));
  const client = new OrbitDataWatchClient({ baseUrl, apiKey: 'test-key', requestTimeoutMs: 100, maxAttempts: 1 });
  await assert.rejects(client.listWatches(), error => error.code === 'REQUEST_TIMEOUT' && !error.message.includes('test-key'));
  const second = new OrbitDataWatchClient({ baseUrl, apiKey: 'test-key', requestTimeoutMs: 1000, maxAttempts: 1 });
  await assert.rejects(second.listWatches(), { code: 'INVALID_JSON' });
});

test('receives authenticated SSE readings and resumes from the committed cursor', async t => {
  const requests = [];
  const watchId = 'cccccccc-cccc-4ccc-8ccc-cccccccccccc';
  const baseUrl = await serverFor(t, (req, res) => {
    requests.push({ path: req.url, auth: req.headers.authorization,
      cursor: req.headers['last-event-id'], accept: req.headers.accept });
    res.writeHead(200, { 'content-type': 'text/event-stream; charset=utf-8' });
    res.write(`event: status\ndata: ${JSON.stringify({ watchId, status: 'running' })}\n\n`);
    const reading = `id: 42\nevent: reading\ndata: ${JSON.stringify({ watchId, id: '42', checkedAt: '2026-01-01T00:00:00Z', count: 1, items: [] })}\n\n`;
    res.write(reading.slice(0, 31));
    setTimeout(() => res.write(reading.slice(31)), 5);
  });
  const client = new OrbitDataWatchClient({ baseUrl, apiKey: 'test-key' });
  const controller = new AbortController();
  const events = [];
  let opened = false;
  await client.stream(watchId, '41', { signal: controller.signal, onOpen: () => { opened = true; },
    onEvent: event => { events.push(event); if (event.type === 'reading') controller.abort(); }, windowSize: 12 });
  assert.equal(opened, true);
  assert.deepEqual(events.map(event => event.type), ['status', 'reading']);
  assert.equal(events[1].id, '42');
  assert.ok(Number.isFinite(Date.parse(events[1].receivedAt)));
  assert.notEqual(events[1].receivedAt, events[1].data.checkedAt);
  assert.deepEqual(requests, [{ path: `/api/data-watches/${watchId}/stream?after=41&windowSize=12`,
    auth: 'Bearer test-key', cursor: '41', accept: 'text/event-stream' }]);
});

test('rejects an oversized unterminated SSE event before it can grow without bound', async t => {
  const baseUrl = await serverFor(t, (_req, res) => {
    res.writeHead(200, { 'content-type': 'text/event-stream' });
    res.write(`data: ${'x'.repeat(8 * 1024 * 1024 + 1)}`);
  });
  const client = new OrbitDataWatchClient({ baseUrl, apiKey: 'test-key', requestTimeoutMs: 5000 });
  await assert.rejects(client.stream('watch-id', '0'), { code: 'STREAM_EVENT_TOO_LARGE' });
});
