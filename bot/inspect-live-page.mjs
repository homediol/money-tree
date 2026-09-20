/** Read-only selector/round/balance probe using the collector's one browser/context. */
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { BrowserManager } from './collector/BrowserManager.js';
import { LoginManager } from './collector/LoginManager.js';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const cfg = JSON.parse(fs.readFileSync(path.join(root, 'data/bot/config.json'), 'utf8'));
const browser = new BrowserManager(true);
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
  await login.ensureLoggedIn(bettingPage, controller.signal);
  await login.goToAviator(bettingPage, controller.signal);
  const historyPage = await browser.getHistoryPage();
  const observed = [];
  for (const frame of bettingPage.frames()) {
    const data = await frame.evaluate(() => {
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
    }).catch(error => ({error: String(error)}));
    observed.push({url: safeUrl(frame.url()), name: frame.name(), ...data});
  }
  process.stdout.write(`${JSON.stringify({
    authenticated: await login.isLoggedIn(bettingPage),
    browserContexts: browser._browser?.contexts?.().length ?? 1,
    pagesInContext: context.pages().length,
    historyAndBettingSeparate: historyPage !== bettingPage,
    bettingPage: safeUrl(bettingPage.url()),
    historyPage: safeUrl(historyPage.url()),
    frames: observed,
  }, null, 2)}\n`);
} catch (error) {
  process.stderr.write(`READ_ONLY_INSPECTION_FAILED: ${error.message}\n`);
  process.exitCode = 1;
} finally {
  clearTimeout(timer);
  await browser.close();
}
