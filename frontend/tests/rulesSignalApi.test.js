import test from 'node:test';
import assert from 'node:assert/strict';
import { copyTextToClipboard } from '../src/services/rulesSignalApi.js';

test('Copy key writes the one-time secret to the clipboard unchanged', async () => {
  const writes = [];
  const clipboard = { async writeText(value) { writes.push(value); } };
  const secret = 'wpc_one-time-secret-value';
  assert.equal(await copyTextToClipboard(secret, clipboard), true);
  assert.deepEqual(writes, [secret]);
});

test('Copy response normalizes non-string values and fails when clipboard is unavailable', async () => {
  const writes = [];
  await copyTextToClipboard({ ok: true }, { async writeText(value) { writes.push(value); } });
  assert.deepEqual(writes, ['[object Object]']);
  await assert.rejects(copyTextToClipboard('secret', null), /Clipboard access is unavailable/);
});
