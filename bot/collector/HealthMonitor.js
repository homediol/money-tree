/**
 * HealthMonitor.js — Tracks collector health metrics and writes status.json.
 * Exposes a snapshot for the Watchdog and the Flask /health endpoint.
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { log } from './Logger.js';

const __dirname    = path.dirname(fileURLToPath(import.meta.url));
const STATUS_PATH  = path.join(__dirname, '..', '..', 'data', 'bot', 'status.json');
const FLUSH_INTERVAL_MS = 5000;

export class HealthMonitor {
  constructor() {
    this._startTime = Date.now();
    this._metrics   = {
      collectorRunning:   false,
      browserConnected:   false,
      pageConnected:      false,
      frameConnected:     false,
      loggedIn:           false,
      state:              'STARTING',
      lastRoundTime:      null,
      lastRecovery:       null,
      recoveryCount:      0,
      browserRestartCount:0,
      loginCount:         0,
      totalRoundsSaved:   0,
      uptime:             0,
    };
    this._flushTimer = null;
  }

  start() {
    this._flushTimer = setInterval(() => this._flush(), FLUSH_INTERVAL_MS);
    this._flushTimer.unref?.(); // don't keep process alive
  }

  stop() {
    if (this._flushTimer) { clearInterval(this._flushTimer); this._flushTimer = null; }
    this._flush();
  }

  // ── Setters ───────────────────────────────────────────────────────────────

  setState(s)                { this._metrics.state = s; }
  setCollectorRunning(v)     { this._metrics.collectorRunning = v; }
  setBrowserConnected(v)     { this._metrics.browserConnected = v; }
  setPageConnected(v)        { this._metrics.pageConnected = v; }
  setFrameConnected(v)       { this._metrics.frameConnected = v; }
  setLoggedIn(v)             { this._metrics.loggedIn = v; }
  recordRound()              { this._metrics.lastRoundTime = new Date().toISOString(); this._metrics.totalRoundsSaved++; }
  recordRecovery(action)     { this._metrics.recoveryCount++; this._metrics.lastRecovery = { action, ts: new Date().toISOString() }; }
  recordBrowserRestart()     { this._metrics.browserRestartCount++; }
  recordLogin()              { this._metrics.loginCount++; }

  // ── Getters ───────────────────────────────────────────────────────────────

  snapshot() {
    return {
      ...this._metrics,
      uptime: Math.floor((Date.now() - this._startTime) / 1000),
    };
  }

  /** Seconds since the last round was saved. null if no round yet. */
  secondsSinceLastRound() {
    if (!this._metrics.lastRoundTime) return null;
    return (Date.now() - new Date(this._metrics.lastRoundTime).getTime()) / 1000;
  }

  // ── Persistence ───────────────────────────────────────────────────────────

  _flush() {
    try {
      const snap = this.snapshot();
      fs.mkdirSync(path.dirname(STATUS_PATH), { recursive: true });
      const tmp = STATUS_PATH + '.tmp';
      fs.writeFileSync(tmp, JSON.stringify(snap, null, 2), 'utf-8');
      fs.renameSync(tmp, STATUS_PATH);
    } catch { /* never crash on status write */ }
  }
}
