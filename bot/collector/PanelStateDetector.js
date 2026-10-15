const PANEL_ID = /^PANEL_[12]$/;
const ACTION = /^(bet|cancel|cash\s*out)\b/i;

function buttonColorFamily(value) {
  const text = String(value || '').trim().toLowerCase();
  if (['green', 'red', 'amber', 'gold', 'yellow'].includes(text)) {
    return text === 'gold' || text === 'yellow' ? 'AMBER' : text.toUpperCase();
  }
  const rgb = text.match(/rgba?\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})/);
  let red, green, blue;
  if (rgb) [red, green, blue] = rgb.slice(1).map(Number);
  else {
    const hex = text.match(/^#([0-9a-f]{3}|[0-9a-f]{6})$/i)?.[1];
    if (!hex) return null;
    const value = hex.length === 3 ? [...hex].map(char => char + char).join('') : hex;
    [red, green, blue] = [0, 2, 4].map(index => Number.parseInt(value.slice(index, index + 2), 16));
  }
  if (green >= 80 && green >= red * 1.18 && green >= blue * 1.15) return 'GREEN';
  // Cash Out uses amber/golden yellow; a red dominant orange is not accepted.
  if (red >= 175 && green >= 160 && blue <= 125 && red > green && red <= green * 1.5) return 'AMBER';
  if (red >= 100 && red >= green * 1.35 && red >= blue * 1.25) return 'RED';
  return null;
}

function parseAmount(text) {
  const match = String(text || '').match(/(?<![A-Z])([0-9][0-9,]*(?:\.[0-9]+)?)(?![0-9])/i);
  if (!match) return null;
  const amount = Number(match[1].replaceAll(',', ''));
  return Number.isFinite(amount) ? amount : null;
}

function parseCurrency(text, hint) {
  const values = new Set([...String(text || '').matchAll(/(?<![A-Z])([A-Z]{3})(?![A-Z])/g)]
    .map(match => match[1].toUpperCase()).filter(code => !['BET', 'OUT'].includes(code)));
  if (/^[a-z]{3}$/i.test(String(hint || '').trim())) {
    const hinted = String(hint).trim().toUpperCase();
    if (!['BET', 'OUT'].includes(hinted)) values.add(hinted);
  }
  return values.size === 1 ? [...values][0] : null;
}

export function classifyPanelObservation(raw = {}, { now = Date.now(), maxAgeMs = 15000 } = {}) {
  const panelId = String(raw.panel_id || 'UNKNOWN');
  const frameIdentity = String(raw.frame_identity || '').slice(0, 180) || null;
  const source = String(raw.source || 'AVIATOR_BETTING_PANEL_READ_ONLY').slice(0, 80);
  const buttonText = String(raw.button_text || '').replace(/\s+/g, ' ').trim().slice(0, 240);
  const secondaryText = String(raw.secondary_text || '').replace(/\s+/g, ' ').trim().slice(0, 160);
  const combinedText = `${buttonText} ${secondaryText}`.trim();
  const inferred = combinedText.match(ACTION)?.[1] || '';
  const mainLabel = String(raw.main_label || inferred).replace(/\s+/g, ' ').trim();
  const normalizedMain = mainLabel.toLowerCase();
  const color = buttonColorFamily(raw.button_color);
  const expectedColor = ({ bet: 'GREEN', cancel: 'RED', 'cash out': 'AMBER' })[normalizedMain] || null;
  const queuedText = /\bwaiting for (?:the )?next round\b/i.test(combinedText);
  const labels = Array.isArray(raw.visible_action_labels) ? raw.visible_action_labels.join(' ').toLowerCase() : '';
  const hasBet = normalizedMain === 'bet' || /\bbet\b/i.test(labels);
  const hasCancel = normalizedMain === 'cancel' || /\bcancel\b/i.test(labels);
  const hasCashout = normalizedMain === 'cash out' || /\bcash\s*out\b/i.test(labels);
  const observedAt = raw.observed_at || null;
  const timestamp = observedAt == null ? NaN : Date.parse(observedAt);
  const ageMs = now - timestamp;
  let state = 'UNKNOWN';
  let reason = 'PANEL_EVIDENCE_INCOMPLETE';
  let confidence = 0;

  if (!PANEL_ID.test(panelId) || !frameIdentity) reason = 'PANEL_OR_FRAME_IDENTITY_UNKNOWN';
  else if (!Number.isFinite(timestamp)) reason = 'OBSERVATION_TIMESTAMP_INVALID';
  else if (ageMs < -30000 || ageMs > maxAgeMs) reason = 'STALE_OR_FUTURE_OBSERVATION';
  else if (raw.panel_visible !== true) reason = 'PANEL_NOT_VISIBLE';
  else if (raw.button_visible !== true) {
    if (['reset', 'idle', 'clear'].includes(String(raw.data_state || '').toLowerCase())) {
      state = 'BET_RESET'; reason = 'EXPLICIT_PANEL_RESET'; confidence = 0.9;
    } else reason = 'ACTION_CONTROL_NOT_VISIBLE';
  } else if ([hasBet, hasCancel, hasCashout].filter(Boolean).length > 1) reason = 'CONFLICTING_VISIBLE_ACTIONS';
  else if ((raw.pending_indicator === true && normalizedMain === 'bet')
      || (raw.active_indicator === true && ['bet', 'cancel'].includes(normalizedMain))) {
    reason = 'CONTRADICTORY_PENDING_OR_ACTIVE_INDICATOR';
  } else if (expectedColor && color && color !== expectedColor) reason = 'BUTTON_TEXT_COLOR_CONFLICT';
  else if (normalizedMain === 'bet' && raw.button_enabled === true) {
    state = 'BET_AVAILABLE'; reason = 'BET_ACTION_VISIBLE_ENABLED';
  } else if (normalizedMain === 'cancel' && queuedText && raw.button_enabled === true) {
    state = 'BET_QUEUED'; reason = 'CANCEL_AND_NEXT_ROUND_WAIT_VISIBLE';
  } else if (normalizedMain === 'cash out' && raw.button_enabled === true) {
    state = 'BET_ACTIVE'; reason = 'CASHOUT_ACTION_VISIBLE_ENABLED';
  } else reason = 'STATE_LABEL_OR_ENABLED_STATUS_INCOMPLETE';

  if (state !== 'UNKNOWN' && !confidence) confidence = color === expectedColor ? 0.98 : 0.88;
  const stakeSource = raw.displayed_stake_text != null ? String(raw.displayed_stake_text)
    : normalizedMain === 'cash out' ? '' : buttonText;
  const cashoutSource = String(raw.cashout_text || buttonText);
  const isCashout = normalizedMain === 'cash out' || state === 'BET_ACTIVE';
  return {
    panel_id: panelId,
    slot: PANEL_ID.test(panelId) ? Number(panelId.slice(-1)) - 1 : null,
    state,
    button_text: buttonText || null,
    displayed_stake: parseAmount(stakeSource),
    currency: parseCurrency(isCashout ? cashoutSource : stakeSource, raw.currency_hint),
    cashout_amount: isCashout ? parseAmount(cashoutSource) : null,
    observed_at: observedAt,
    source,
    evidence: {
      reason, panel_visible: raw.panel_visible === true,
      button_visible: raw.button_visible === true,
      button_enabled: raw.button_enabled === true,
      main_label: mainLabel || null, secondary_text: secondaryText || null,
      aria_label: raw.aria_label || null, title: raw.title || null,
      button_attributes: raw.button_attributes || {}, panel_attributes: raw.panel_attributes || {},
      button_color: raw.button_color || null, recognized_color: color,
      visible_action_labels: Array.isArray(raw.visible_action_labels) ? raw.visible_action_labels : [],
      pending_indicator: raw.pending_indicator === true,
      active_indicator: raw.active_indicator === true,
    },
    confidence,
    frame_identity: frameIdentity,
    pending_bet_state: state === 'BET_QUEUED' ? 'PENDING'
      : state === 'BET_ACTIVE' ? 'ACTIVE'
        : ['BET_AVAILABLE', 'BET_RESET'].includes(state) ? 'CLEAR' : 'UNKNOWN',
  };
}

export function panelObservationTransition(previous, current) {
  const oldObservation = previous || {};
  const nextObservation = current || {};
  let stale = false;
  if (oldObservation.observed_at && nextObservation.observed_at) {
    const oldTime = Date.parse(oldObservation.observed_at);
    const newTime = Date.parse(nextObservation.observed_at);
    stale = !Number.isFinite(oldTime) || !Number.isFinite(newTime) || newTime < oldTime;
  }
  const frameReloaded = Boolean(oldObservation.frame_identity && nextObservation.frame_identity
    && oldObservation.frame_identity !== nextObservation.frame_identity);
  const duplicate = Boolean(previous && current && !frameReloaded
    && oldObservation.state === nextObservation.state
    && oldObservation.button_text === nextObservation.button_text
    && oldObservation.displayed_stake === nextObservation.displayed_stake
    && oldObservation.currency === nextObservation.currency
    && oldObservation.cashout_amount === nextObservation.cashout_amount
    && oldObservation.evidence?.recognized_color === nextObservation.evidence?.recognized_color);
  return {
    changed: Boolean(current) && !stale && (!duplicate || frameReloaded),
    duplicate,
    stale,
    frame_reloaded: frameReloaded,
    from_state: frameReloaded ? 'UNKNOWN' : previous ? (oldObservation.state || 'UNKNOWN') : null,
    to_state: current ? (nextObservation.state || 'UNKNOWN') : 'UNKNOWN',
    observed_at: nextObservation.observed_at || null,
  };
}
