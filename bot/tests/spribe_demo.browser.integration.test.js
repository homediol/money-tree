import test from 'node:test';
import assert from 'node:assert/strict';
import os from 'node:os';
import path from 'node:path';

const configured = Boolean(process.env.AVIATOR_DEMO_URL);

test('SPRIBE Demo read-only probe inspects the existing BrowserManager page', {
  skip: !configured,
  timeout: 120000,
}, async (t) => {
  process.env.WINNER_NONFINANCIAL_TEST_MODE = 'true';
  process.env.BOT_CDP_ENDPOINT ||= 'http://127.0.0.1:9222';
  process.env.BOT_PROFILE_DIR = path.join(os.tmpdir(), `winner-predict-no-demo-profile-${process.pid}`);
  process.env.BOT_GOOGLE_USER_DATA_DIR = path.join(os.tmpdir(), `winner-predict-no-google-profile-${process.pid}`);
  process.env.BOT_CDP_PORT = '0';

  let manager;
  try {
    const { getBrowserSupervisor, validateSpribeDemoUrl } = await import('../collector/BrowserManager.js');
    const target = validateSpribeDemoUrl();
    manager = getBrowserSupervisor(true);
    manager._persistMetadata = () => {};
    let page;
    try { page = await manager.attachSpribeDemoPage(); }
    catch (error) {
      if (String(error?.message || error).startsWith('BROWSER_SUPERVISOR_ALREADY_ACTIVE')) {
        t.skip('the existing collector owns the shared BrowserManager; attach-only Demo probe was not run');
        return;
      }
      throw error;
    }

    const safeUrl = raw => {
      try { const url = new URL(raw); return `${url.origin}${url.pathname}`; }
      catch { return 'UNKNOWN_URL'; }
    };
    const scanFrame = frame => frame.evaluate(() => {
      const visible = node => {
        const style = getComputedStyle(node);
        return node.getClientRects().length > 0 && style.display !== 'none'
          && style.visibility !== 'hidden' && Number(style.opacity || 1) > 0;
      };
      const shortText = node => String(node.innerText || node.textContent || '')
        .replace(/\s+/g, ' ').trim().slice(0, 140);
      const selectorFor = node => {
        const parts = [];
        let current = node;
        for (let depth = 0; current && current !== document.body && depth < 5; depth += 1) {
          let part = current.tagName.toLowerCase();
          if (current.id && /^[\w-]+$/.test(current.id)) part += `#${current.id}`;
          else if (current.classList?.length) {
            part += [...current.classList].filter(value => /^[\w-]+$/.test(value))
              .slice(0, 3).map(value => `.${value}`).join('');
          }
          parts.unshift(part);
          const candidate = parts.join(' > ');
          try { if (document.querySelectorAll(candidate).length === 1) return candidate; }
          catch { /* Continue with the observed ancestor chain. */ }
          current = current.parentElement;
        }
        return parts.join(' > ');
      };
      const actionPattern = /\b(bet|cancel|cash\s*out|flew away|place your bets|next round|no more bets)\b/i;
      const discovered = Array.from(document.querySelectorAll('body *')).filter(node => {
        if (!visible(node)) return false;
        const text = shortText(node);
        return text && text.length <= 140 && actionPattern.test(text)
          && !Array.from(node.children).some(child => actionPattern.test(shortText(child)));
      }).slice(0, 20).map(node => {
        const style = getComputedStyle(node);
        const attrs = {};
        for (const name of ['id', 'class', 'role', 'type', 'aria-label', 'aria-pressed',
          'aria-checked', 'aria-disabled', 'title', 'data-testid', 'data-state', 'disabled']) {
          const value = node.getAttribute(name);
          if (value !== null) attrs[name] = value.slice(0, 160);
        }
        const ancestors = [];
        for (let parent = node.parentElement, depth = 0; parent && parent !== document.body && depth < 4;
          parent = parent.parentElement, depth += 1) {
          ancestors.push({ tag: parent.tagName.toLowerCase(), id: parent.id || null,
            class: typeof parent.className === 'string' ? parent.className.slice(0, 100) : null });
        }
        return { selector: selectorFor(node), tag: node.tagName.toLowerCase(), text: shortText(node),
          attrs, disabled: Boolean(node.disabled || node.getAttribute('aria-disabled') === 'true'),
          background: style.backgroundColor, color: style.color, ancestors };
      });
      const controls = Array.from(document.querySelectorAll('button, input, [role="button"], [role="switch"], [role="checkbox"]'))
        .filter(visible).slice(0, 40).map(node => {
          const attrs = {};
          for (const name of ['id', 'class', 'role', 'type', 'aria-label', 'aria-checked',
            'aria-pressed', 'aria-disabled', 'title', 'name', 'data-testid', 'disabled']) {
            const value = node.getAttribute(name);
            if (value !== null) attrs[name] = value.slice(0, 160);
          }
          return { selector: selectorFor(node), tag: node.tagName.toLowerCase(), text: shortText(node), attrs,
            value: node instanceof HTMLInputElement ? node.value.slice(0, 80) : null,
            checked: 'checked' in node ? Boolean(node.checked) : null,
            disabled: Boolean(node.disabled || node.getAttribute('aria-disabled') === 'true'),
            background: getComputedStyle(node).backgroundColor };
        });
      const lines = String(document.body?.innerText || '').split(/\n+/).map(line => line.trim()).filter(Boolean);
      const evidenceLines = lines.filter(line => /demo|practice|balance|\bUSD\b|bet|cancel|cash|round|multiplier|flew away|waiting|closes|seconds/i.test(line))
        .filter(line => !/chat|player|leaderboard|top bet|biggest win/i.test(line)).slice(0, 24);
      const canvases = Array.from(document.querySelectorAll('canvas')).filter(visible).map(canvas => ({
        selector: selectorFor(canvas), width: canvas.width, height: canvas.height,
      }));
      return { title: document.title.slice(0, 160), ready_state: document.readyState,
        evidence_lines: evidenceLines, discovered, controls, canvases };
    }).catch(error => ({ scan_error: error?.name || 'FRAME_SCAN_FAILED' }));

    const started = Date.now();
    const timeline = [];
    let lastKey = '';
    let latest = [];
    while (Date.now() - started < 55000) {
      const frames = page.frames();
      latest = await Promise.all(frames.map(async frame => ({
        url: safeUrl(frame.url()),
        is_main_frame: frame === page.mainFrame(),
        observation: await scanFrame(frame),
      })));
      const compact = latest.filter(item => item.observation.evidence_lines?.length
        || item.observation.discovered?.length || item.observation.controls?.length)
        .map(item => ({ url: item.url,
          text: item.observation.evidence_lines?.map(value => value.replace(/\d+(?:[.,]\d+)?/g, '#')) || [],
          actions: item.observation.discovered?.map(action => [action.selector,
            action.text.replace(/\d+(?:[.,]\d+)?/g, '#'), action.attrs.class]) || [],
          controls: item.observation.controls?.map(control => [control.selector,
            control.text.replace(/\d+(?:[.,]\d+)?/g, '#'), control.value, control.checked]) || [],
        }));
      const key = JSON.stringify(compact);
      if (key !== lastKey && compact.some(item => item.text.length || item.actions.length || item.controls.length)) {
        timeline.push({ observed_at: new Date().toISOString(), frames: compact });
        lastKey = key;
      }
      if (timeline.length >= 20) break;
      await page.waitForTimeout(600);
    }

    const topology = await manager.getBrowserStatus();
    const report = {
      provider: 'SPRIBE_DEMO',
      configured_origin: target.origin,
      actual_page: safeUrl(page.url()),
      topology: { browser_count: topology.browser_count, context_count: topology.context_count,
        page_count: topology.page_count, aviator_page_count: topology.aviator_page_count,
        demo_page_count: manager._demoTopology?.demo_page_count ?? null,
        frame_count: page.frames().length },
      timeline,
      final_frames: latest.filter(item => item.is_main_frame || /aviator|spribegaming/i.test(item.url))
        .map(item => ({ url: item.url, is_main_frame: item.is_main_frame,
          title: item.observation.title, ready_state: item.observation.ready_state,
          evidence_lines: item.observation.evidence_lines, discovered: item.observation.discovered,
          controls: item.observation.controls, canvases: item.observation.canvases,
          scan_error: item.observation.scan_error })),
      demo_only: true,
      no_settings_changed: true,
      bet_submitted: false,
    };
    assert.equal(report.actual_page.startsWith(target.origin), true);
    assert.deepEqual({ browser: report.topology.browser_count, contexts: report.topology.context_count,
      demo_pages: report.topology.demo_page_count }, { browser: 1, contexts: 1, demo_pages: 1 });
    assert.equal(report.topology.page_count, manager._demoTopology.page_count,
      'Demo attach changed the number of browser pages');
    assert.ok(timeline.length > 0, 'Demo page produced no visible DOM evidence');
    console.log(`SPRIBE_DEMO_OBSERVATION ${JSON.stringify(report)}`);
  } finally {
    if (manager) await manager.close().catch(() => {});
  }
});
