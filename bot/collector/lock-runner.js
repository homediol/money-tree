#!/usr/bin/env node
/** Run the collector under an OS advisory lock, released automatically on exit. */
import fs from 'node:fs';
import path from 'node:path';
import os from 'node:os';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..', '..');
const lockPath = process.env.COLLECTOR_LOCK_PATH || path.join(ROOT, 'data', 'bot', 'collector.lock');
const entrypoint = path.join(ROOT, 'bot', 'roundhistory-collector.js');

function loadBackendEnv() {
  const envPath = path.join(ROOT, 'backend', '.env');
  try {
    for (const line of fs.readFileSync(envPath, 'utf8').split(/\r?\n/)) {
      const match = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*?)\s*$/);
      if (!match || Object.hasOwn(process.env, match[1])) continue;
      let value = match[2];
      if ((value.startsWith('"') && value.endsWith('"')) || (value.startsWith("'") && value.endsWith("'"))) {
        value = value.slice(1, -1);
      }
      process.env[match[1]] = value;
    }
  } catch (error) {
    if (error?.code !== 'ENOENT') process.stderr.write(`[collector] backend environment could not be read: ${error.message}\n`);
  }
}

function processIsAlive(pid) {
  try { process.kill(pid, 0); return true; }
  catch (error) { return error?.code !== 'ESRCH'; }
}

function fallbackLock() {
  fs.mkdirSync(path.dirname(lockPath), { recursive: true });
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      const fd = fs.openSync(lockPath, 'wx', 0o600);
      fs.writeFileSync(fd, JSON.stringify({ pid: process.pid, started_at: new Date().toISOString(), host: os.hostname() }));
      fs.fsyncSync(fd);
      return fd;
    } catch (error) {
      if (error?.code !== 'EEXIST') throw error;
      let previous;
      try { previous = fs.readFileSync(lockPath, 'utf8'); }
      catch (readError) { if (readError?.code === 'ENOENT') continue; throw readError; }
      let owner;
      try { owner = JSON.parse(previous); }
      catch (parseError) {
        process.stderr.write(`[collector] lock owner metadata is invalid (${parseError.message}); refusing unsafe takeover\n`);
        return null;
      }
      if (!owner || !Number.isInteger(Number(owner.pid)) || Number(owner.pid) <= 1) {
        process.stderr.write('[collector] lock owner metadata is unverifiable; refusing unsafe takeover\n');
        return null;
      }
      if (owner && Number.isInteger(Number(owner.pid)) && processIsAlive(Number(owner.pid))) {
        process.stderr.write(`[collector] duplicate start refused; PID ${owner.pid} still owns ${lockPath}\n`);
        return null;
      }
      if (owner?.host && owner.host !== os.hostname()) {
        process.stderr.write(`[collector] lock owner host is unverifiable (${owner.host}); refusing takeover\n`);
        return null;
      }
      let current;
      try { current = fs.readFileSync(lockPath, 'utf8'); }
      catch (readError) { if (readError?.code === 'ENOENT') continue; throw readError; }
      if (current === previous) fs.unlinkSync(lockPath);
    }
  }
  return fallbackLock();
}

loadBackendEnv();
fs.mkdirSync(path.dirname(lockPath), { recursive: true });

let child;
let fallbackFd = null;
let stopping = false;
let fallbackStarted = false;
function forward(signal) {
  if (stopping) return;
  stopping = true;
  if (child && child.exitCode == null) child.kill(signal);
}
process.on('SIGINT', () => forward('SIGINT'));
process.on('SIGTERM', () => forward('SIGTERM'));

try {
  // --no-fork makes flock exec the worker in place. The monitored PID is
  // therefore the worker itself, and signals forwarded by this wrapper reach
  // the process that owns the lock. Without it, a killed flock parent could
  // release the lock while its child browser collector continued running.
  child = spawn('flock', ['--nonblock', '--conflict-exit-code', '75', '--no-fork', lockPath,
    process.execPath, entrypoint], { cwd: ROOT, env: process.env, stdio: 'inherit' });
  child.once('error', error => {
    if (error?.code !== 'ENOENT') {
      process.stderr.write(`[collector] lock runner failed: ${error.message}\n`);
      process.exitCode = 1;
      return;
    }
    fallbackStarted = true;
    try {
      fallbackFd = fallbackLock();
      if (fallbackFd == null) { process.exitCode = 75; return; }
      child = spawn(process.execPath, [entrypoint], { cwd: ROOT, env: process.env, stdio: 'inherit' });
      child.once('error', spawnError => {
        process.stderr.write(`[collector] worker start failed: ${spawnError.message}\n`);
        process.exitCode = 1;
      });
      child.once('close', (code, signal) => {
        try { fs.closeSync(fallbackFd); } catch (closeError) { process.stderr.write(`[collector] lock close failed: ${closeError.message}\n`); }
        try { fs.unlinkSync(lockPath); } catch (unlinkError) { if (unlinkError?.code !== 'ENOENT') process.stderr.write(`[collector] lock release failed: ${unlinkError.message}\n`); }
        process.exitCode = code ?? (signal ? 1 : 0);
      });
      if (stopping) child.kill('SIGTERM');
    } catch (fallbackError) {
      process.stderr.write(`[collector] safe fallback lock failed: ${fallbackError.message}\n`);
      process.exitCode = 1;
    }
  });
  child.once('close', (code, signal) => {
    if (fallbackStarted) return;
    if (code === 75) process.stderr.write('[collector] duplicate start refused; another collector owns the OS lock\n');
    process.exitCode = code ?? (signal ? 1 : 0);
  });
  if (stopping && child.exitCode == null) child.kill('SIGTERM');
} catch (error) {
  process.stderr.write(`[collector] lock runner failed: ${error.message}\n`);
  process.exitCode = 1;
}
