/**
 * Read authentication evidence from the Spribe game frame itself.
 * The Winner page shell and the embedded game can have different auth state.
 */

const SPRIBE_HOST = /(^|\.)spribegaming\.com$/i;
const GAME_LOGIN_PROMPT = /\b(?:please\s+)?(?:log\s*in|sign\s*in)\s+(?:to\s+)?(?:play|continue)\b|\b(?:authentication|session)\s+(?:is\s+)?required\b/i;
const PASSWORD_INPUT_SELECTOR = 'input[type="password"], input[autocomplete*="password" i]';

function safeUrl(frame) {
  try { return new URL(frame.url()); }
  catch { return null; }
}

function hasSessionParameters(url) {
  const names = new Set(Array.from(url.searchParams.keys(), key => key.toLowerCase()));
  return names.has('token') && names.has('user');
}

async function hasExplicitLoginPrompt(frame) {
  try {
    if (await frame.locator(PASSWORD_INPUT_SELECTOR).first().isVisible({ timeout: 250 })) return true;
  } catch { /* The provider may not expose form controls yet. */ }

  for (const role of ['button', 'link']) {
    try {
      const control = frame.getByRole(role, { name: /^(?:log\s*in|login|sign\s*in)$/i }).first();
      if (await control.isVisible({ timeout: 250 })) return true;
    } catch { /* Older Playwright frame adapters may not support role locators. */ }
  }

  try {
    const bodyText = await frame.locator('body').innerText({ timeout: 500 });
    return GAME_LOGIN_PROMPT.test(bodyText);
  } catch {
    return false;
  }
}

/**
 * @returns {Promise<{
 *   status: 'NO_GAME_FRAME'|'AUTHENTICATED'|'EXPIRED'|'UNKNOWN',
 *   frameFound: boolean,
 *   reasonCode: string
 * }>}
 */
export async function inspectGameFrameSession(page) {
  let frames = [];
  let mainFrame = null;
  let iframeUrls = [];
  try {
    frames = page?.frames?.() || [];
    mainFrame = page?.mainFrame?.() || null;
  } catch { /* Treat unavailable frame topology as no readable game frame. */ }
  try {
    iframeUrls = await page?.locator?.('iframe')?.evaluateAll?.(elements =>
      elements.map(element => element.src || element.getAttribute('src') || '').filter(Boolean),
    ) || [];
  } catch { /* Some page adapters cannot inspect iframe elements. */ }

  const gameFrames = frames.filter(frame => {
    try {
      if (frame === mainFrame || frame.isDetached?.()) return false;
      const url = safeUrl(frame);
      return Boolean(url && SPRIBE_HOST.test(url.hostname));
    } catch { return false; }
  });
  const gameIframeUrls = iframeUrls.filter(rawUrl => {
    try { return SPRIBE_HOST.test(new URL(rawUrl).hostname); }
    catch { return false; }
  });

  if (!gameFrames.length && !gameIframeUrls.length) {
    return { status: 'NO_GAME_FRAME', frameFound: false, reasonCode: 'GAME_FRAME_NOT_FOUND' };
  }

  let hasAuthenticatedFrame = false;
  let hasLoginPrompt = false;
  const gameUrls = [
    ...gameFrames.map(safeUrl).filter(Boolean),
    ...gameIframeUrls.map(rawUrl => {
      try { return new URL(rawUrl); }
      catch { return null; }
    }).filter(Boolean),
  ];
  for (const url of gameUrls) {
    if (url && hasSessionParameters(url)) hasAuthenticatedFrame = true;
  }
  for (const frame of gameFrames) {
    if (await hasExplicitLoginPrompt(frame)) hasLoginPrompt = true;
  }

  // An in-frame login surface takes precedence over a stale token-bearing
  // ancestor frame; the visible game UI is the freshest auth evidence.
  if (hasLoginPrompt) {
    return { status: 'EXPIRED', frameFound: true, reasonCode: 'GAME_FRAME_LOGIN_REQUIRED' };
  }
  if (hasAuthenticatedFrame) {
    return { status: 'AUTHENTICATED', frameFound: true, reasonCode: 'AUTHENTICATED_GAME_SESSION' };
  }
  return { status: 'UNKNOWN', frameFound: true, reasonCode: 'GAME_FRAME_SESSION_UNVERIFIED' };
}
