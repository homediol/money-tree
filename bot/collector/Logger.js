/**
 * Logger.js — Structured, levelled logger with recovery event tracking.
 * All output goes to stdout so start-all.js prefix-coloring works correctly.
 */

import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const LOG_FILE   = path.join(__dirname, '..', '..', 'data', 'bot', 'collector.log');
const MAX_LOG_BYTES = 5 * 1024 * 1024; // 5 MB — rotate when exceeded

const LEVELS = { DEBUG: 0, INFO: 1, WARN: 2, ERROR: 3 };
const COLORS = {
  DEBUG: '\x1b[90m',
  INFO:  '\x1b[37m',
  WARN:  '\x1b[33m',
  ERROR: '\x1b[31m',
  RESET: '\x1b[0m',
};

let _minLevel = LEVELS.INFO;

function ts() {
  return new Date().toLocaleTimeString('en-US', { hour12: false });
}

function serialize(v) {
  try { return typeof v === 'object' ? JSON.stringify(v) : String(v); }
  catch { return String(v); }
}

function rotatIfNeeded() {
  try {
    if (!fs.existsSync(LOG_FILE)) return;
    const { size } = fs.statSync(LOG_FILE);
    if (size > MAX_LOG_BYTES) {
      fs.renameSync(LOG_FILE, LOG_FILE + '.1');
    }
  } catch { /* ignore */ }
}

function writeFile(line) {
  try {
    rotatIfNeeded();
    fs.mkdirSync(path.dirname(LOG_FILE), { recursive: true });
    fs.appendFileSync(LOG_FILE, line + '\n', 'utf-8');
  } catch { /* never crash on log write */ }
}

function emit(level, tag, message, extras) {
  if (LEVELS[level] < _minLevel) return;
  const extra = extras.length ? ' ' + extras.map(serialize).join(' ') : '';
  const plain = `[${ts()}] [${tag}] [${level}] ${message}${extra}`;
  const colored = `${COLORS[level]}${plain}${COLORS.RESET}`;
  process.stdout.write(colored + '\n');
  writeFile(plain);
}

export class Logger {
  constructor(tag = 'COLLECTOR') {
    this.tag = tag;
  }

  setLevel(level) { _minLevel = LEVELS[level] ?? LEVELS.INFO; }

  debug(msg, ...args) { emit('DEBUG', this.tag, msg, args); }
  info (msg, ...args) { emit('INFO',  this.tag, msg, args); }
  warn (msg, ...args) { emit('WARN',  this.tag, msg, args); }
  error(msg, ...args) { emit('ERROR', this.tag, msg, args); }

  /** Log a structured recovery event. */
  recovery(action, reason, durationMs, success, retryCount = 0) {
    const payload = { action, reason, durationMs, success, retryCount, ts: new Date().toISOString() };
    emit('WARN', this.tag, `RECOVERY action=${action} success=${success} retries=${retryCount} duration=${durationMs}ms reason="${reason}"`, []);
    writeFile('RECOVERY_EVENT ' + JSON.stringify(payload));
  }
}

export const log = new Logger('COLLECTOR');
