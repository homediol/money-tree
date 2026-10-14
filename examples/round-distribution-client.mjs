#!/usr/bin/env node

// Node 18+ receiver example. Stores only the non-secret stream cursor locally.
const baseUrl = String(process.env.WINNER_PREDICT_URL || '').replace(/\/$/, '');
const token = process.env.WINNER_PREDICT_ROUND_TOKEN || '';
const cursorPath = process.env.ROUND_CURSOR_FILE || '.round-stream-cursor.json';
if (!baseUrl || !token) {
  console.error('Set WINNER_PREDICT_URL (HTTPS) and WINNER_PREDICT_ROUND_TOKEN.');
  process.exit(2);
}

const fs = await import('node:fs/promises');
let cursor = null;
const byRoundId = new Map();
try { cursor = JSON.parse(await fs.readFile(cursorPath, 'utf8')); }
catch (error) { if (error?.code !== 'ENOENT') throw error; }

async function api(path, headers = {}) {
  const response = await fetch(`${baseUrl}${path}`, {
    headers: { authorization: `Bearer ${token}`, accept: 'application/json', ...headers },
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    const error = new Error(body?.detail?.error || body?.error || `HTTP ${response.status}`);
    error.status = response.status;
    error.body = body;
    throw error;
  }
  return body;
}

async function saveCursor() {
  if (cursor) await fs.writeFile(cursorPath, `${JSON.stringify(cursor)}\n`, { mode: 0o600 });
}

function acceptRound(event) {
  const key = String(event.event_id || event.round_id);
  const previous = byRoundId.get(key);
  if (previous?.durability === 'COMMITTED' && event.durability !== 'COMMITTED') return;
  if (previous && previous.event_type === event.event_type
      && previous.durability === event.durability
      && previous.stream_sequence === event.stream_sequence) return;
  byRoundId.set(key, event);
  if (byRoundId.size > 10000) byRoundId.delete(byRoundId.keys().next().value);
  console.log(JSON.stringify(event));
}

async function initializeCursor() {
  const latest = await api('/api/v1/rounds/latest');
  if (!cursor) {
    const earliest = latest.retention.earliest_sequence;
    cursor = {
      stream_id: latest.stream_id,
      after_sequence: earliest == null ? 0 : Math.max(0, Number(earliest) - 1),
    };
  } else if (cursor.stream_id !== latest.stream_id) {
    const earliest = latest.retention.earliest_sequence;
    console.warn('Round stream identity changed; resynchronizing retained events.');
    cursor = { stream_id: latest.stream_id,
      after_sequence: earliest == null ? Number(latest.retention.latest_sequence || 0)
        : Math.max(0, Number(earliest) - 1) };
  }
  await saveCursor();
}

async function catchUp() {
  for (;;) {
    const query = new URLSearchParams({
      after_sequence: String(cursor.after_sequence),
      stream_id: cursor.stream_id,
      limit: '500',
    });
    let page;
    try { page = await api(`/api/v1/rounds?${query}`); }
    catch (error) {
      if (error.status !== 409 || error.body?.detail?.error !== 'resync_required') throw error;
      const detail = error.body.detail;
      console.warn(`Replay cursor requires resync (${detail.reason}); reading retained committed events.`);
      cursor = { stream_id: detail.stream_id,
        after_sequence: Number(detail.resync_after_sequence || 0) };
      await saveCursor();
      continue;
    }
    for (const event of page.rounds || []) acceptRound(event);
    cursor = { stream_id: page.stream_id, after_sequence: Number(page.next_cursor || cursor.after_sequence) };
    await saveCursor();
    if (!page.has_more) return;
  }
}

function parseSseFrame(frame) {
  const parsed = { data: [] };
  for (const line of frame.split(/\r?\n/)) {
    if (!line || line.startsWith(':')) continue;
    const split = line.indexOf(':');
    const field = split < 0 ? line : line.slice(0, split);
    const value = split < 0 ? '' : line.slice(split + 1).replace(/^ /, '');
    if (field === 'data') parsed.data.push(value);
    else if (field === 'id') parsed.id = value;
    else if (field === 'event') parsed.event = value;
  }
  if (!parsed.data.length) return null;
  try { parsed.payload = JSON.parse(parsed.data.join('\n')); }
  catch { return null; }
  return parsed;
}

async function consumeStream() {
  const response = await fetch(`${baseUrl}/api/v1/rounds/stream`, {
    headers: {
      authorization: `Bearer ${token}`,
      accept: 'text/event-stream',
      'last-event-id': `${cursor.stream_id}:${cursor.after_sequence}`,
    },
  });
  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => ({}));
    const error = new Error(body?.detail?.error || `SSE HTTP ${response.status}`);
    error.status = response.status;
    error.body = body;
    throw error;
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return;
    buffer += decoder.decode(value, { stream: true });
    let boundary;
    while ((boundary = buffer.search(/\r?\n\r?\n/)) >= 0) {
      const frame = buffer.slice(0, boundary);
      const delimiter = buffer.slice(boundary).match(/^\r?\n\r?\n/)[0];
      buffer = buffer.slice(boundary + delimiter.length);
      const message = parseSseFrame(frame);
      if (!message) continue;
      const event = message.payload;
      let committedCursor = null;
      if (message.id && event.event_type === 'round.persisted') {
        const [streamId, sequenceText] = message.id.split(':');
        committedCursor = { stream_id: streamId, after_sequence: Number(sequenceText) };
      }
      if (event.event_type === 'round.observed' || event.event_type === 'round.persisted') {
        acceptRound(event);
      } else if (event.event_type === 'heartbeat') {
        console.error(`heartbeat collector=${event.collector_status} continuity=${event.continuity_status}`);
      } else if (event.event_type === 'stream.overflow') {
        console.warn('Server queue overflow; reconnecting and catching up from REST.');
        return;
      } else if (event.event_type === 'auth.revoked') {
        throw new Error('This project token was revoked.');
      } else if (event.event_type === 'server.shutdown') {
        console.warn('Winner Predict is shutting down; reconnecting after a short delay.');
        return;
      }
      // Advance only after handing the event to the example's receiver logic.
      // A crash in between causes a harmless duplicate on reconnect, not a skip.
      if (committedCursor) {
        cursor = committedCursor;
        await saveCursor();
      }
    }
  }
}

await initializeCursor();
let delay = 500;
for (;;) {
  try {
    await catchUp();
    await consumeStream();
    delay = 500;
  } catch (error) {
    if (error.status === 401 || error.message.includes('token was revoked')) {
      console.error(error.message);
      process.exit(1);
    }
    console.error(`Round stream unavailable (${error.message}); retrying.`);
    if (error.status === 409 && error.body?.detail?.error === 'resync_required') {
      const detail = error.body.detail;
      cursor = { stream_id: detail.stream_id,
        after_sequence: Number(detail.resync_after_sequence || 0) };
      await saveCursor();
    }
    await new Promise(resolve => setTimeout(resolve, delay));
    delay = Math.min(15000, delay * 2);
  }
}
