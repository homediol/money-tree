#!/usr/bin/env node
import { startOrbitRoundHistoryCollector } from './collector/OrbitRoundHistoryCollector.js';
import { log, formatError } from './collector/Logger.js';

const controller = new AbortController();
for (const signal of ['SIGINT', 'SIGTERM']) {
  process.on(signal, () => controller.abort());
}
process.on('uncaughtException', error => log.error(`[SAFETY NET] uncaughtException: ${formatError(error)}`));
process.on('unhandledRejection', reason => log.error(`[SAFETY NET] unhandledRejection: ${formatError(reason)}`));

startOrbitRoundHistoryCollector(controller.signal).catch(error => {
  log.error(`Orbit Round History collector stopped: ${formatError(error)}`);
  process.exitCode = 1;
});
