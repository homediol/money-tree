#!/usr/bin/env node
/**
 * roundhistory-collector.js — Entry point.
 *
 * Thin launcher. All logic lives in ./collector/.
 * Handles graceful shutdown via AbortController.
 * Never calls process.exit() from inside the collector.
 */

import { startCollector } from './collector/index.js';
import { log, formatError } from './collector/Logger.js';

// ── Graceful shutdown ─────────────────────────────────────────────────────────

const ac = new AbortController();

function shutdown(sig) {
  log.info(`Received ${sig} — shutting down gracefully...`);
  ac.abort();
  // Force exit after 8 seconds if clean shutdown hangs
  setTimeout(() => process.exit(0), 8000).unref();
}

process.on('SIGINT',  () => shutdown('SIGINT'));
process.on('SIGTERM', () => shutdown('SIGTERM'));

// ── Global error safety net ───────────────────────────────────────────────────
// These should never fire — every error is caught inside the collector.
// They are here purely as a last-resort guard.

process.on('uncaughtException', (err) => {
  log.error(`[SAFETY NET] uncaughtException: ${formatError(err)}`);
  // Do NOT exit — let the collector's outer loop recover
});

process.on('unhandledRejection', (reason) => {
  log.error(`[SAFETY NET] unhandledRejection: ${formatError(reason)}`);
  // Do NOT exit
});

// ── Start ─────────────────────────────────────────────────────────────────────

startCollector(ac.signal).catch(err => {
  log.error(`startCollector threw unexpectedly: ${formatError(err)}`);
});
