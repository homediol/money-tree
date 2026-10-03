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
const STALE_AFTER_MS = Number(process.env.HISTORY_STALE_AFTER_MS || 180000);

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
      lastRoundId:        null,
      lastSuccessfulCollection: null,
      heartbeatAt:        null,
      lastObserverInstalledAt: null,
      network:            { status: 'UNKNOWN', reason: null, checked_at: null },
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
  setNetwork(value)          { this._metrics.network = value; }
  recordObserverInstalled()  { this._metrics.lastObserverInstalledAt = new Date().toISOString(); }
  recordRound(round = null) {
    this._metrics.lastRoundTime = round?.timestamp || new Date().toISOString();
    if (round?.round_id != null) this._metrics.lastRoundId = String(round.round_id);
    this._metrics.lastSuccessfulCollection = new Date().toISOString();
    this._metrics.heartbeatAt = this._metrics.lastSuccessfulCollection;
    this._metrics.totalRoundsSaved++;
  }
  recordRecovery(action)     { this._metrics.recoveryCount++; this._metrics.lastRecovery = { action, ts: new Date().toISOString() }; }
  recordBrowserRestart()     { this._metrics.browserRestartCount++; }
  recordLogin()              { this._metrics.loginCount++; }

  // ── Getters ───────────────────────────────────────────────────────────────

  snapshot() {
    let health = 'WAITING';
    if (!this._metrics.collectorRunning) health = 'STOPPED';
    else if (this._metrics.network.status !== 'ONLINE' && this._metrics.network.status !== 'UNKNOWN') health = 'WAITING_FOR_NETWORK';
    else if (!this._metrics.browserConnected) health = 'CONNECTING';
    else if (!this._metrics.frameConnected) health = 'DISCONNECTED';
    else if (this._metrics.lastRoundTime && Date.now() - Date.parse(this._metrics.lastRoundTime) > STALE_AFTER_MS) health = 'STALE';
    else if (this._metrics.state === 'COLLECTING') health = 'HEALTHY';
    return {
      ...this._metrics,
      health,
      uptime: Math.floor((Date.now() - this._startTime) / 1000),
    };
  }

  /** Seconds since the last round was saved. null if no round yet. */
  secondsSinceLastRound() {
    if (!this._metrics.lastRoundTime) return null;
    return (Date.now() - new Date(this._metrics.lastRoundTime).getTime()) / 1000;
  }

  secondsSinceCollectionActivity() {
    const times = [this._metrics.lastRoundTime, this._metrics.lastObserverInstalledAt]
      .map(value => value ? Date.parse(value) : NaN).filter(Number.isFinite);
    return times.length ? (Date.now() - Math.max(...times)) / 1000 : null;
  }

  // ── Persistence ───────────────────────────────────────────────────────────

  _flush() {
    try {
      if (this._metrics.collectorRunning) this._metrics.heartbeatAt = new Date().toISOString();
      const snap = this.snapshot();
      fs.mkdirSync(path.dirname(STATUS_PATH), { recursive: true });
      const tmp = STATUS_PATH + '.tmp';
      fs.writeFileSync(tmp, JSON.stringify(snap, null, 2), 'utf-8');
      fs.renameSync(tmp, STATUS_PATH);
    } catch { /* never crash on status write */ }
  }
}
