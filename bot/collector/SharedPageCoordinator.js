/** Serialize short writes to the two Aviator betting panels. History reads do not use this lock. */
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { randomUUID } from 'node:crypto';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const DEFAULT_LOCK_DIRECTORY = path.join(ROOT, 'data', 'bot', 'shared-aviator-ui.lock');

function processExists(pid) {
  if (!Number.isInteger(pid) || pid <= 1) return null;
  try { process.kill(pid, 0); return true; }
  catch (error) { return error?.code === 'ESRCH' ? false : true; }
}

function processStartTicks(pid) {
  try {
    const stat = fs.readFileSync(`/proc/${pid}/stat`, 'utf8');
    return Number(stat.slice(stat.lastIndexOf(')') + 2).trim().split(/\s+/)[19]) || null;
  } catch { return null; }
}

export class SharedPageCoordinator {
  constructor({ lockDirectory = DEFAULT_LOCK_DIRECTORY, acquireTimeoutMs = 10000, pollMs = 25 } = {}) {
    this._tail = Promise.resolve();
    this._pending = 0;
    this.lockDirectory = lockDirectory;
    this.acquireTimeoutMs = acquireTimeoutMs;
    this.pollMs = pollMs;
  }

  get bettingUiBusy() { return this._pending > 0; }

  async withBettingUiLock(operation, { signal } = {}) {
    if (typeof operation !== 'function') throw new TypeError('operation must be a function');
    if (signal?.aborted) throw new Error('Aborted');
    let release;
    const predecessor = this._tail;
    this._tail = new Promise(resolve => { release = resolve; });
    this._pending += 1;
    await predecessor;
    try {
      if (signal?.aborted) throw new Error('Aborted');
      const owner = await this._acquireExternal(signal);
      try { return await operation(); }
      finally { this._releaseExternal(owner); }
    } finally {
      this._pending = Math.max(0, this._pending - 1);
      release();
    }
  }

  async _acquireExternal(signal) {
    const deadline = Date.now() + this.acquireTimeoutMs;
    const owner = { pid: process.pid, host: os.hostname(), token: randomUUID(),
      process_start_ticks: processStartTicks(process.pid), acquired_at: new Date().toISOString() };
    fs.mkdirSync(path.dirname(this.lockDirectory), { recursive: true });
    while (Date.now() < deadline) {
      if (signal?.aborted) throw new Error('Aborted');
      try {
        fs.mkdirSync(this.lockDirectory, { mode: 0o700 });
        const ownerPath = path.join(this.lockDirectory, 'owner.json');
        fs.writeFileSync(ownerPath, JSON.stringify(owner), { encoding: 'utf8', mode: 0o600, flag: 'wx' });
        return owner;
      } catch (error) {
        if (error?.code !== 'EEXIST') {
          try { fs.rmdirSync(this.lockDirectory); } catch { /* retain ambiguous ownership state */ }
          throw error;
        }
        try {
          const existing = JSON.parse(fs.readFileSync(path.join(this.lockDirectory, 'owner.json'), 'utf8'));
          const existingPid = Number(existing.pid);
          const startTicks = processStartTicks(existingPid);
          const ownerWasReused = Number.isFinite(Number(existing.process_start_ticks))
            && startTicks != null && Number(existing.process_start_ticks) !== startTicks;
          if (existing.host === owner.host
              && (processExists(existingPid) === false || ownerWasReused)) {
            fs.unlinkSync(path.join(this.lockDirectory, 'owner.json'));
            fs.rmdirSync(this.lockDirectory);
            continue;
          }
        } catch (readError) {
          if (readError?.code !== 'ENOENT' && !(readError instanceof SyntaxError)) throw readError;
        }
        await new Promise(resolve => setTimeout(resolve, this.pollMs));
      }
    }
    throw new Error('SHARED_AVIATOR_UI_LOCK_TIMEOUT');
  }

  _releaseExternal(owner) {
    const ownerPath = path.join(this.lockDirectory, 'owner.json');
    try {
      const current = JSON.parse(fs.readFileSync(ownerPath, 'utf8'));
      if (current.token !== owner.token || Number(current.pid) !== process.pid) return;
      fs.unlinkSync(ownerPath);
      fs.rmdirSync(this.lockDirectory);
    } catch (error) {
      if (error?.code !== 'ENOENT') throw error;
    }
  }
}
