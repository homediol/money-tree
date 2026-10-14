/** Keep bot-triggered navigation links and form submissions in their page. */
export async function clickInCurrentTab(page, control, options = {}) {
  if (typeof control?.evaluate === 'function') {
    await control.evaluate(element => {
      const link = element.matches?.('a, area') ? element : element.closest?.('a, area');
      if (link) link.target = '_self';

      const form = element.closest?.('form');
      if (form) form.target = '_self';
    });
  }

  const openOverrideKey = 'winner-predict.same-tab-window-open';
  const canOverrideWindowOpen = typeof page?.evaluate === 'function';
  if (canOverrideWindowOpen) {
    await page.evaluate(keyName => {
      const key = Symbol.for(keyName);
      if (window[key]) return;
      Object.defineProperty(window, key, { value: window.open, configurable: true });
      window.open = url => {
        if (url && String(url) !== 'about:blank') window.location.assign(String(url));
        return window;
      };
    }, openOverrideKey);
  }

  try {
    return await control.click(options);
  } finally {
    if (canOverrideWindowOpen) {
      await page.evaluate(keyName => {
        const key = Symbol.for(keyName);
        if (!window[key]) return;
        window.open = window[key];
        delete window[key];
      }, openOverrideKey).catch(() => {});
    }
  }
}
