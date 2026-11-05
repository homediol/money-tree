/**
 * Collector.js — Injects MutationObserver into the Aviator iframe and
 * streams payout events back to Node.js.
 *
 * The module is stateless with respect to the frame; Playwright supplies a
 * fresh frame when the observer is installed.
 */

import { log } from './Logger.js';
import { normalizeMultiplier, snapshotSignature } from './HistoryManager.js';
import { PAYOUT_SELECTOR } from './FrameManager.js';

const MUTATION_IDLE_MS = Number(process.env.BOT_IDLE_TIMEOUT || 60000);
const MAX_BUFFERED_ROUND_EVENTS = 256;

// ── In-frame code (evaluated directly inside the iframe) ────────────────────
// These are plain function expressions passed to frame.evaluate().
// Playwright serialises the function body and runs it in the frame context.
// Arguments are passed as the second parameter to frame.evaluate().

export async function _installObserver(frame, selector) {
  return frame.evaluate(({ sel, notificationBindingName, maxQueuedRoundEvents }) => {
    // The backend deliberately expires phase evidence quickly and clears its
    // volatile copy on restart. Re-read the owned Aviator frame regularly so
    // an unchanged provider state remains fresh at the API.
    const phaseHeartbeatMs = 5000;

    function parse(text) {
      if (!text) return null;
      const c = String(text).trim().replace(/,/g,'').replace(/[xX×]/g,'').replace(/[^0-9.]/g,'');
      if (!c) return null;
      const v = parseFloat(c);
      return (isFinite(v) && v > 0) ? Math.round(v * 100) / 100 : null;
    }
    function readEntries() {
      return Array.from(document.querySelectorAll(sel))
        .map(element => ({ element, multiplier: parse(element.textContent) }));
    }
    function readAll() {
      return readEntries().map(entry => entry.multiplier).filter(v => v !== null);
    }
    function sig(arr) { return arr.map(v => v.toFixed(2)).join('|'); }

    function readGamePhase() {
      const block = document.querySelector('.bet-block');
      const button = block?.querySelector('.btn-success.bet');
      const input = block?.querySelector('input[type="text"]:not([placeholder=""])');
      const style = button ? window.getComputedStyle(button) : null;
      const visible = Boolean(button && style && style.display !== 'none'
        && style.visibility !== 'hidden' && (button.offsetWidth || button.offsetHeight));
      const disabled = Boolean(button && (button.disabled
        || button.getAttribute('aria-disabled') === 'true'
        || button.classList.contains('disabled')));
      const inputVisible = Boolean(input && (input.offsetWidth || input.offsetHeight));
      const tabs = block ? Array.from(block.querySelectorAll('.tab')) : [];
      const betTabActive = tabs.some(el =>
        (el.textContent || '').trim().toLowerCase() === 'bet'
        && el.classList?.contains('active'));
      // Button styling is not provider phase evidence. In particular, a green
      // enabled Bet button does not prove which round is open or its deadline.
      // Accept phase only from one explicit provider marker that supplies the
      // round identity, target mapping, and deadline in the same DOM record.
      const providerMarkers = Array.from(document.querySelectorAll(
        '[data-aviator-phase][data-provider-round-id][data-canonical-target-round-id][data-betting-deadline]'
      ));
      const marker = providerMarkers.length === 1 ? providerMarkers[0] : null;
      const providerPhase = String(marker?.getAttribute('data-aviator-phase') || '').toUpperCase();
      const providerRoundId = String(marker?.getAttribute('data-provider-round-id') || '').trim();
      const canonicalTargetRoundId = String(marker?.getAttribute('data-canonical-target-round-id') || '').trim();
      const bettingDeadline = String(marker?.getAttribute('data-betting-deadline') || '').trim();
      const proofMethod = String(marker?.getAttribute('data-target-identity-proof-method') || '').trim();
      const targetNotStarted = marker?.getAttribute('data-target-not-started') === 'true';
      const phaseEvidenceValid = Boolean(
        marker && providerRoundId && canonicalTargetRoundId &&
        ['BETTING_OPEN', 'BETTING_CLOSED'].includes(providerPhase) &&
        Number.isFinite(Date.parse(bettingDeadline)) &&
        /(?:Z|[+-]\d\d:\d\d)$/i.test(bettingDeadline) &&
        proofMethod === 'platform_target_id_before_start' && targetNotStarted &&
        ((providerPhase !== 'BETTING_OPEN') || (visible && inputVisible && betTabActive && !disabled))
      );
      const phase = phaseEvidenceValid ? providerPhase : 'UNKNOWN';
      return {
        phase,
        observed_at: new Date().toISOString(),
        provider_round_id: phaseEvidenceValid ? providerRoundId : null,
        canonical_target_round_id: phaseEvidenceValid ? canonicalTargetRoundId : null,
        betting_deadline: phaseEvidenceValid ? bettingDeadline : null,
        target_identity_verified: phaseEvidenceValid,
        target_started: phaseEvidenceValid ? false : null,
        target_identity_proof: phaseEvidenceValid ? {
          provider_round_id: providerRoundId,
          target_round_id: canonicalTargetRoundId,
          target_not_started: true,
          proof_method: proofMethod,
        } : null,
        evidence: {
          source: 'AVIATOR_GAME_PHASE_READ_ONLY',
          provider_phase_source: marker ? 'EXPLICIT_PROVIDER_DATA_ATTRIBUTES' : null,
          provider_phase_selector: marker
            ? '[data-aviator-phase][data-provider-round-id][data-canonical-target-round-id][data-betting-deadline]'
            : null,
          selector: '.bet-block .btn-success.bet',
          button_visible: visible,
          button_enabled: visible && !disabled,
          stake_input_visible: inputVisible,
          bet_tab_active: betTabActive,
          button_text: button ? (button.textContent || '').trim().slice(0, 40) : null,
          button_class: button ? String(button.className || '').slice(0, 160) : null,
          provider_marker_count: providerMarkers.length,
        },
      };
    }

    const first = document.querySelector(sel);
    let block = first?.closest('.payouts-block') || document.querySelector('.payouts-block');
    const target = block?.parentElement || block || document.body;
    if (!first || !block) return { ok: false, error: 'payout target not found' };

    if (window.__aviatorCollector?.observer) {
      window.__aviatorCollector.observer.disconnect();
      window.__aviatorCollector.phaseObserver?.disconnect();
      if (window.__aviatorCollector.phaseHeartbeat) {
        clearInterval(window.__aviatorCollector.phaseHeartbeat);
      }
      window.__aviatorCollector = null;
    }

    const initialEntries = readEntries();
    const c = { queue: [], queueOverflow: false, waiter: null, latestPhaseEvent: null,
      payoutEntries: initialEntries,
      lastSig: sig(initialEntries.map(entry => entry.multiplier).filter(v => v !== null)), observer: null,
      phaseObserver: null, phaseHeartbeat: null, lastPhaseSig: null,
      latestGamePhase: null, notificationBindingName };

    function dispatch(ev) {
      if (c.notificationBindingName
          && typeof window[c.notificationBindingName] === 'function') {
        try {
          window[c.notificationBindingName](JSON.stringify(ev));
          return;
        } catch (_) {
          // Preserve the in-frame queue fallback if CDP notification fails.
        }
      }
      if (c.waiter) {
        const w = c.waiter;
        c.waiter = null;
        if (ev.phaseOnly && c.latestPhaseEvent === ev) c.latestPhaseEvent = null;
        w(ev);
        return;
      }
      // Round observations are durable work candidates; retain each payout
      // event until the collector drains it. If a prolonged Node stall fills
      // this compatibility queue, force a full snapshot recovery instead of
      // silently evicting a completed round.
      if (!ev.phaseOnly) {
        if (c.queue.length >= maxQueuedRoundEvents) c.queueOverflow = true;
        else c.queue.push(ev);
      }
    }

    function publish(reason, mutationObservedAt = new Date().toISOString(), mutations = null,
      mutationObservedPerformanceMs = null) {
      const extractionStartedAt = new Date().toISOString();
      const extractionStartedPerformanceMs = performance.now();
      let entries = c.payoutEntries || [];
      let updatedOnlyChangedPayouts = Array.isArray(mutations) && mutations.length > 0;
      if (updatedOnlyChangedPayouts) {
        for (const mutation of mutations) {
          const element = nodeElement(mutation.target);
          const payout = element?.closest?.(sel);
          const insertedPayout = mutation.type === 'childList'
            && [...(mutation.addedNodes || []), ...(mutation.removedNodes || [])]
              .some(node => subtreeMatches(node, sel));
          const cached = payout ? entries.find(entry => entry.element === payout) : null;
          if (!payout || !cached || insertedPayout || mutation.type === 'attributes') {
            updatedOnlyChangedPayouts = false;
            break;
          }
          cached.multiplier = parse(payout.textContent);
        }
      }
      if (!updatedOnlyChangedPayouts) entries = readEntries();
      c.payoutEntries = entries;
      const mults = entries.map(entry => entry.multiplier).filter(v => v !== null);
      const extractedAt = new Date().toISOString();
      const extractedPerformanceMs = performance.now();
      if (!mults.length) return;
      const nextSig = sig(mults);
      if (nextSig === c.lastSig) return;
      c.lastSig = nextSig;
      const ev = { reason, ts: mutationObservedAt, mutation_observed_at: mutationObservedAt,
        extraction_started_at: extractionStartedAt, extracted_at: extractedAt,
        page_mutation_to_extraction_ms: Number.isFinite(mutationObservedPerformanceMs)
          ? Math.max(0, extractedPerformanceMs - mutationObservedPerformanceMs) : null,
        page_extraction_ms: Math.max(0, extractedPerformanceMs - extractionStartedPerformanceMs),
        multipliers: mults, sig: nextSig, gamePhase: c.latestGamePhase };
      dispatch(ev);
    }

    function nodeElement(node) {
      return node?.nodeType === 1 ? node : node?.parentElement || null;
    }

    function subtreeMatches(node, selector) {
      const element = nodeElement(node);
      if (!element) return false;
      return Boolean(element.matches?.(selector) || element.querySelector?.(selector));
    }

    function touchesPayouts(mutation) {
      const element = nodeElement(mutation.target);
      const payout = element?.closest?.(sel);
      if (mutation.type === 'characterData' || mutation.type === 'childList') {
        if (payout) return true;
        if (mutation.type !== 'childList') return false;
        for (const node of [...(mutation.addedNodes || []), ...(mutation.removedNodes || [])]) {
          const changed = nodeElement(node);
          const changedBlock = changed?.matches?.('.payouts-block')
            ? changed : changed?.querySelector?.('.payouts-block');
          if (changedBlock) block = document.querySelector('.payouts-block') || changedBlock;
          if (changed === block || subtreeMatches(node, sel)) return true;
        }
        return false;
      }
      if (mutation.type === 'attributes' && mutation.attributeName === 'class') {
        return Boolean(element && (element.matches?.(sel)
          || c.payoutEntries.some(entry => entry.element === element)));
      }
      return false;
    }

    const providerMarkerSelector = '[data-aviator-phase][data-provider-round-id][data-canonical-target-round-id][data-betting-deadline]';
    let observedBetBlock = document.querySelector('.bet-block');
    function touchesGameState(mutation) {
      const element = nodeElement(mutation.target);
      // Result-history churn is handled by the payout observer and cannot
      // alter betting controls or provider target identity.
      if (element?.closest?.('.payouts-block')) return false;
      const providerMarker = element && (element.closest?.(providerMarkerSelector)
        || element.matches?.(providerMarkerSelector));
      if (providerMarker || element?.closest?.('.bet-block')) return true;
      // Ancestor visibility/class changes can affect the Bet panel; changes
      // inside that panel and replacement of it are also relevant.
      if (element && observedBetBlock && (element === observedBetBlock
          || element.contains?.(observedBetBlock) || observedBetBlock.contains?.(element))) return true;
      if (mutation.type === 'childList') {
        for (const node of [...(mutation.addedNodes || []), ...(mutation.removedNodes || [])]) {
          const changed = nodeElement(node);
          if (changed?.matches?.('.payouts-block') || changed?.closest?.('.payouts-block')) continue;
          const replacesBetBlock = subtreeMatches(node, '.bet-block');
          if (replacesBetBlock || subtreeMatches(node, providerMarkerSelector)) {
            if (replacesBetBlock) observedBetBlock = document.querySelector('.bet-block');
            return true;
          }
        }
      }
      return false;
    }

    function publishPhase(reason) {
      const phase = readGamePhase();
      c.latestGamePhase = phase;
      const phaseSig = JSON.stringify({
        phase: phase.phase,
        provider_round_id: phase.provider_round_id,
        canonical_target_round_id: phase.canonical_target_round_id,
        betting_deadline: phase.betting_deadline,
        target_identity_verified: phase.target_identity_verified,
        target_started: phase.target_started,
        target_identity_proof: phase.target_identity_proof,
        evidence: phase.evidence,
      });
      const heartbeat = reason === 'provider-phase-heartbeat';
      if (phaseSig === c.lastPhaseSig && !heartbeat) return;
      c.lastPhaseSig = phaseSig;
      const ev = { phaseOnly: true, reason, ts: phase.observed_at, gamePhase: phase };
      // Coalesce phase-only refreshes separately so a periodic heartbeat can
      // never evict completed-round payout observations.
      c.latestPhaseEvent = ev;
      dispatch(ev);
    }

    const obs = new MutationObserver(mutations => {
      const mutationObservedAt = new Date().toISOString();
      const mutationObservedPerformanceMs = performance.now();
      const relevantMutations = mutations.filter(m =>
        (m.type === 'childList' || m.type === 'characterData'
          || (m.type === 'attributes' && m.attributeName === 'class'))
        && touchesPayouts(m));
      if (relevantMutations.length) {
        publish('mutation', mutationObservedAt, relevantMutations, mutationObservedPerformanceMs);
      }
    });
    obs.observe(target, {
      childList: true, subtree: true, characterData: true,
    attributes: true, attributeFilter: ['class'],
    });
    c.observer = obs;
    const phaseObserver = new MutationObserver(mutations => {
      if (mutations.some(touchesGameState)) publishPhase('betting-panel-mutation');
    });
    phaseObserver.observe(document.body, {
      childList: true, subtree: true, characterData: true,
      attributes: true, attributeFilter: ['class', 'style', 'hidden', 'disabled', 'aria-disabled',
        'data-aviator-phase', 'data-provider-round-id', 'data-canonical-target-round-id',
        'data-betting-deadline', 'data-target-identity-proof-method', 'data-target-not-started'],
    });
    c.phaseObserver = phaseObserver;
    c.phaseHeartbeat = setInterval(
      () => publishPhase('provider-phase-heartbeat'), phaseHeartbeatMs);
    window.__aviatorCollector = c;

    publishPhase('observer-installed');

    const mults = readAll();
    return { ok: true, count: mults.length, multipliers: mults, sig: sig(mults) };
  }, {
    sel: selector,
    notificationBindingName: frame.collectorBindingName || null,
    maxQueuedRoundEvents: Math.max(1, Number(frame.maxQueuedEvents) || MAX_BUFFERED_ROUND_EVENTS),
  });
}

async function _waitForMutation(frame, idleMs) {
  if (typeof frame.waitForCollectorMutation === 'function') {
    return frame.waitForCollectorMutation(idleMs);
  }
  return frame.evaluate((ms) => {
    const c = window.__aviatorCollector;
    if (!c) return { error: 'collector_not_installed' };
    if (c.queue.length > 0) return c.queue.shift();
    if (c.queueOverflow) return { error: 'collector_page_event_queue_overflow' };
    if (c.latestPhaseEvent) {
      const phaseEvent = c.latestPhaseEvent;
      c.latestPhaseEvent = null;
      return phaseEvent;
    }
    return new Promise(resolve => {
      let settled = false;
      const done = payload => {
        if (settled) return; settled = true;
        clearTimeout(timer);
        if (c.waiter === done) c.waiter = null;
        resolve(payload);
      };
      const timer = setTimeout(() => done({ timeout: true }), ms);
      c.waiter = done;
    });
  }, idleMs);
}

// ── Collector class ───────────────────────────────────────────────────────────

export class Collector {
  async install(frame) {
    const result = await _installObserver(frame, PAYOUT_SELECTOR);
    if (!result?.ok) {
      throw new Error(result?.error || 'MutationObserver install failed');
    }
    const multipliers = result.multipliers.map(normalizeMultiplier).filter(v => v !== null);
    log.info(`Collector: observer installed — ${multipliers.length} rounds visible`);
    return { multipliers, signature: snapshotSignature(multipliers) };
  }

  async waitForMutation(frame, idleMs = MUTATION_IDLE_MS) {
    const raw = await _waitForMutation(frame, idleMs);
    if (raw?.error) return { error: raw.error };
    if (raw?.timeout) return { timeout: true };
    const nodeReceivedAt = new Date().toISOString();
    const nodeReceivedMonotonicMs = performance.now();
    const multipliers = (raw?.multipliers ?? []).map(normalizeMultiplier).filter(v => v !== null);
    const normalizedAt = new Date().toISOString();
    return {
      multipliers,
      signature: snapshotSignature(multipliers),
      timestamp: raw?.ts || null,
      mutationObservedAt: raw?.mutation_observed_at || raw?.ts || null,
      extractionStartedAt: raw?.extraction_started_at || null,
      extractedAt: raw?.extracted_at || null,
      pageMutationToExtractionMs: Number.isFinite(raw?.page_mutation_to_extraction_ms)
        ? raw.page_mutation_to_extraction_ms : null,
      pageExtractionMs: Number.isFinite(raw?.page_extraction_ms) ? raw.page_extraction_ms : null,
      nodeReceivedAt,
      nodeReceivedMonotonicMs,
      normalizedAt,
      phaseOnly: raw?.phaseOnly === true,
      gamePhase: raw?.gamePhase || null,
    };
  }

  async isInstalled(frame) {
    try {
      return await frame.evaluate(() => !!window.__aviatorCollector?.observer);
    } catch (error) {
      log.debug(`Collector observer status could not be read from the current frame: ${error?.message || error}`);
      return false;
    }
  }
}
