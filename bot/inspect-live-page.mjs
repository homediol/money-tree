/** Read-only selector/round/balance probe using the collector-owned game page. */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { BrowserManager } from './collector/BrowserManager.js';
import { FrameManager } from './collector/FrameManager.js';
import { LoginManager } from './collector/LoginManager.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const cfg = JSON.parse(fs.readFileSync(path.join(root, 'data/bot/config.json'), 'utf8'));
const browser = new BrowserManager(true);
const frameManager = new FrameManager();
const controller = new AbortController();
const timer = setTimeout(() => controller.abort(), 90000);
const safeUrl = value => {
  try { const url = new URL(value); return `${url.origin}${url.pathname}`; }
  catch { return String(value).slice(0, 120); }
};

try {
  const context = await browser.launch(controller.signal);
  const bettingPage = await browser.getPage();
  const login = new LoginManager({
    phone: process.env.WINNER_PHONE || cfg.phone || '',
    password: process.env.WINNER_PASSWORD || cfg.password || '',
  });
  browser._browser?.once('disconnected', () => controller.abort());
  if (!await login.isLoggedIn(bettingPage)) {
    await login.ensureLoggedIn(bettingPage, controller.signal);
  }
  if (!/aviator|crash/i.test(bettingPage.url())) {
    await login.goToAviator(bettingPage, controller.signal);
  }

  // Do not call getHistoryPage() here. When an Aviator page is already open,
  // cloning it can make Winner redirect one of the duplicate game tabs and
  // detach every frame while this probe is reading it. Betting and collection
  // both observe the one collector-owned game page through the same context.
  await bettingPage.waitForSelector(
    'iframe[src*="spribe"], iframe[src*="aviator"], iframe[src*="crash"]',
    { state: 'attached', timeout: 20000 },
  );
  const inspectFrame = () => {
    const visible = el => !!(el && (el.offsetWidth || el.offsetHeight));
    const compact = el => ({
      tag: el.tagName.toLowerCase(),
      classes: String(el.className || '').slice(0, 160),
      type: el.getAttribute('type'),
      placeholder: el.getAttribute('placeholder'),
      ariaLabel: el.getAttribute('aria-label'),
      text: (el.textContent || '').trim().slice(0, 80),
      visible: visible(el), disabled: !!el.disabled,
    });
    return {
      balance: Array.from(document.querySelectorAll('.header__balance, .header__wrap-balance')).map(el => (el.textContent || '').trim().slice(0, 80)),
      panels: Array.from(document.querySelectorAll('.bet-block')).map((el, slot) => ({
        slot, classes: String(el.className || ''),
        inputs: Array.from(el.querySelectorAll('input')).map(compact),
        buttons: Array.from(el.querySelectorAll('button')).map(compact),
      })),
      cashout: Array.from(document.querySelectorAll('[class*="cashout" i], [class*="cash-out" i]')).slice(0, 15).map(compact),
      roundHints: Array.from(document.querySelectorAll('[class*="round" i], [class*="stage" i], [class*="payout" i]')).slice(0, 15).map(compact),
    };
  };
  const observed = [];
  for (const frame of bettingPage.frames()) {
    const data = await frame.evaluate(inspectFrame).catch(error => ({error: String(error)}));
    observed.push({url: safeUrl(frame.url()), name: frame.name(), ...data});
  }
  const gameFrame = await frameManager.waitForFrame(
    bettingPage, { timeoutMs: 20000, signal: controller.signal },
  );
  const gameData = await gameFrame.evaluate(inspectFrame);
  process.stdout.write(`${JSON.stringify({
    authenticated: await login.isLoggedIn(bettingPage),
    browserContexts: browser._browser?.contexts?.().length ?? 1,
    pagesInContext: context.pages().length,
    collectorOwnedPage: true,
    bettingPage: safeUrl(bettingPage.url()),
    gameFrame: {
      url: safeUrl(gameFrame.url()),
      name: gameFrame.name(),
      ...gameData,
    },
    frames: observed,
  }, null, 2)}\n`);
} catch (error) {
  process.stderr.write(`READ_ONLY_INSPECTION_FAILED: ${error.message}\n`);
  process.exitCode = 1;
} finally {
  clearTimeout(timer);
  frameManager._directFrame?.close?.();
  await browser.close();
  // Playwright has no safe "disconnect only" API for connectOverCDP;
  // Browser.close() would terminate the user's collector-owned Chrome. End
  // this one-shot process after stdout/stderr has had time to flush instead.
  const exitCode = process.exitCode || 0;
  setTimeout(() => process.exit(exitCode), 250);
}
