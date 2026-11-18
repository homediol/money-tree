import { API_CREDENTIAL_CHANGED_EVENT, API_TOKEN_STORAGE_KEY, getApiToken } from '../auth.js';

export const NOTIFICATION_STORAGE_KEY = 'winner_predict_notification_settings_v1';
export const NOTIFICATION_HISTORY_STORAGE_KEY = 'winner_predict_notification_history_v1';
const NOTIFICATION_HISTORY_OWNER_KEY = `${NOTIFICATION_HISTORY_STORAGE_KEY}_owner`;
export const SOUND_PROFILES = [
  { id: 'success', name: 'Success chime', event: 'Prediction won', notes: [[660, 0, .11], [880, .12, .16]] },
  { id: 'loss', name: 'Loss tone', event: 'Prediction lost', notes: [[440, 0, .13], [330, .15, .2]] },
  { id: 'high', name: 'HIGH rising tone', event: 'HIGH selected', notes: [[440, 0, .12], [587, .13, .12], [784, .26, .18]] },
  { id: 'low', name: 'LOW falling tone', event: 'LOW selected', notes: [[784, 0, .12], [587, .13, .12], [440, .26, .18]] },
  { id: 'discovery', name: 'Behaviour discovery', event: 'New behaviour detected', notes: [[523, 0, .1], [659, .12, .1], [784, .24, .2]] },
  { id: 'transition', name: 'Behaviour transition', event: 'Behaviour state changed', notes: [[523, 0, .1], [392, .13, .2]] },
  { id: 'rule-win', name: 'Rule achievement', event: 'Rule performance improved', notes: [[587, 0, .12], [740, .14, .18]] },
  { id: 'rule-warning', name: 'Rule warning', event: 'Rule performance warning', notes: [[440, 0, .18], [392, .2, .24]] },
  { id: 'complete', name: 'Window completion', event: 'ADIE window completed', notes: [[523, 0, .1], [659, .12, .1], [784, .24, .1], [1047, .36, .22]] },
  { id: 'critical', name: 'Critical system alert', event: 'System health warning', notes: [[880, 0, .15], [660, .2, .15], [880, .4, .15]] },
];

const EVENTS = {
  'shadow.created': ['SHADOW', 'New prediction created', 'discovery'],
  'shadow.activated': ['SHADOW', 'Prediction activated', 'high'],
  'shadow.win': ['SHADOW', 'Prediction won', 'success'],
  'shadow.loss': ['SHADOW', 'Prediction lost', 'loss'],
  'shadow.completed': ['SHADOW', 'Prediction completed', 'complete'],
  'shadow.stopped': ['SHADOW', 'Prediction stopped', 'critical'],
  'shadow.paused': ['SHADOW', 'Prediction paused', 'critical'],
  'shadow.alert': ['SHADOW', 'Prediction limit reached', 'critical'],
  'rules.improved': ['Rules Performance', 'Rule performance improved', 'rule-win'],
  'rules.warning': ['Rules Performance', 'Rule performance warning', 'rule-warning'],
  'behaviour.detected': ['Behaviour Detection', 'New behaviour detected', 'discovery'],
  'behaviour.high': ['Behaviour Detection', 'High dominance detected', 'high'],
  'behaviour.low': ['Behaviour Detection', 'Low dominance detected', 'low'],
  'behaviour.high_rising': ['Behaviour Detection', 'High dominance rising', 'high'],
  'behaviour.low_rising': ['Behaviour Detection', 'Low dominance rising', 'low'],
  'behaviour.balance': ['Behaviour Detection', 'Dominance returned to balance', 'transition'],
  'behaviour.transition': ['Behaviour Detection', 'Behaviour transition detected', 'transition'],
  'behaviour.window_completed': ['Behaviour Detection', 'Observed 100-round window completed', 'complete'],
  'behaviour.possible_missing': ['Behaviour Detection', 'Possible missing rounds; coverage is uncertain', 'critical'],
  'behaviour.high_emergence': ['Behaviour Detection', 'New HIGH emergence', 'high'],
  'behaviour.part1_started': ['Behaviour Detection', 'Part 1 started', 'discovery'],
  'behaviour.part1_completed': ['Behaviour Detection', 'Part 1 completed', 'complete'],
  'behaviour.part2_started': ['Behaviour Detection', 'Part 2 started', 'high'],
  'behaviour.part2_paused': ['Behaviour Detection', 'Part 2 paused', 'transition'],
  'behaviour.part2_resumed': ['Behaviour Detection', 'Part 2 resumed', 'high'],
  'behaviour.part2_stopped': ['Behaviour Detection', 'Part 2 stopped', 'critical'],
  'behaviour.cooldown_completed': ['Behaviour Detection', 'Series cooldown completed', 'complete'],
  'adie.selected': ['ADIE', 'Dominance window selected', 'high'],
  'adie.completed': ['ADIE', 'Dominance window completed', 'complete'],
  'adie.positive': ['ADIE', 'Window completed with positive net', 'success'],
  'adie.negative': ['ADIE', 'Window completed with negative net', 'loss'],
  'adie.exploratory': ['ADIE', 'Exploratory selection activated', 'discovery'],
  'adie.integrity': ['ADIE', 'ADIE data freshness warning', 'rule-warning'],
  'system.disconnected': ['System Health', 'Collector disconnected', 'critical'],
  'system.stale': ['System Health', 'Live data is stale', 'critical'],
  'system.restored': ['System Health', 'Connection restored', 'success'],
  'collector.receiving': ['Round Collector', 'Collector is receiving rounds', 'success'],
  'collector.stale': ['Round Collector', 'Collector has stopped receiving rounds', 'critical'],
  'collector.disconnected': ['Round Collector', 'Collector connection lost', 'critical'],
  'collector.restored': ['Round Collector', 'Round collection resumed', 'success'],
  'system.error': ['System Health', 'Critical notification processing error', 'critical'],
};

const defaultEvents = Object.fromEntries(Object.entries(EVENTS).map(([id, [module, message, sound]]) => [id,
  { enabled: true, sound, message, cooldownSeconds: id.startsWith('system.') || id.startsWith('collector.') ? 60 : 15,
    ...(id.startsWith('rules.') ? { threshold: 5 } : {}) }]));
const COLLECTOR_EVENT_TYPES = ['collector.receiving', 'collector.stale', 'collector.disconnected', 'collector.restored'];
// Freshness of the optional ADIE feed is not a user-facing critical alert.
const DEFAULTS = {
  schemaVersion: 7, enabled: true, visualEnabled: true, mode: 'sound-voice', volume: 0.55, muted: false, voiceEnabled: true, priority: 'all',
  voiceURI: '', language: '', rate: 1, pitch: 1, voiceVolume: 0.8, cooldownSeconds: 8,
  quietStart: '', quietEnd: '', events: defaultEvents,
  behaviourAlerts: { repeatEnabled: false, intervalSeconds: 60, completionDurationSeconds: 5 },
};
const listeners = new Set();
const history = loadHistory();
const seen = new Map();
const collectorStates = new Map();
let audioContext;
let audioStatus = 'not-initialized';
let audioUnlockGranted = false;
let audioUnlockPending = false;
let audioResumePending = false;
let audioEnablePromise = null;
let currentSound = null;
let soundQueue = [];
let soundSequence = 0;
let speaking = false;
let speechQueue = [];
let activeUtterance = null;
let settings = loadSettings();

function loadHistory() {
  try {
    const scope = historyScope();
    const owner = localStorage.getItem(NOTIFICATION_HISTORY_OWNER_KEY);
    if (owner && owner !== scope) {
      localStorage.setItem(NOTIFICATION_HISTORY_OWNER_KEY, scope);
      localStorage.removeItem(NOTIFICATION_HISTORY_STORAGE_KEY);
      return [];
    }
    if (!owner) localStorage.setItem(NOTIFICATION_HISTORY_OWNER_KEY, scope);
    const value = JSON.parse(localStorage.getItem(NOTIFICATION_HISTORY_STORAGE_KEY) || '[]');
    return Array.isArray(value) ? value.slice(0, 100) : [];
  }
  catch { return []; }
}
function historyScope() {
  try {
    const token = getApiToken();
    if (!token) return 'local-session';
    let hash = 14695981039346656037n;
    for (const byte of new TextEncoder().encode(token)) {
      hash ^= BigInt(byte);
      hash = BigInt.asUintN(64, hash * 1099511628211n);
    }
    return hash.toString(36);
  } catch { return 'local-session'; }
}
function persistHistory() {
  try {
    localStorage.setItem(NOTIFICATION_HISTORY_OWNER_KEY, historyScope());
    localStorage.setItem(NOTIFICATION_HISTORY_STORAGE_KEY, JSON.stringify(history.slice(0, 100)));
  }
  catch { /* Notification delivery continues when browser storage is unavailable. */ }
}

function loadSettings() {
  try {
    const stored = JSON.parse(localStorage.getItem(NOTIFICATION_STORAGE_KEY) || '{}');
    const storedSchemaVersion = Number(stored.schemaVersion) || 0;
    const events = { ...DEFAULTS.events, ...(stored.events || {}) };
    // The user asked for every notification event to be enabled. Migrate older
    // saved event toggles to ON once, while leaving master audio controls intact.
    if (storedSchemaVersion < 4) for (const eventType of Object.keys(DEFAULTS.events)) {
      events[eventType] = { ...events[eventType], enabled: true };
    }
    // Turn on collector alerts and visual delivery for existing installs, but
    // keep sound opt-in until the selected audio has been previewed.
    if (storedSchemaVersion < 5) for (const eventType of COLLECTOR_EVENT_TYPES) {
      events[eventType] = { ...events[eventType], enabled: true };
    }
    const migrated = { ...DEFAULTS, ...stored,
      visualEnabled: storedSchemaVersion < 5 ? true : (stored.visualEnabled ?? true), schemaVersion: 7,
      events, behaviourAlerts: { ...DEFAULTS.behaviourAlerts, ...(stored.behaviourAlerts || {}) } };
    // The user explicitly requested every sound and voice alert be switched on.
    // Existing installs had master audio disabled or set to voice-only, so
    // migrate once to Sound + voice and enable all configured event types.
    if (storedSchemaVersion < 7) {
      migrated.enabled = true;
      migrated.muted = false;
      migrated.voiceEnabled = true;
      migrated.mode = 'sound-voice';
      for (const eventType of Object.keys(events)) events[eventType] = { ...events[eventType], enabled: true };
    }
    if (storedSchemaVersion < 7) {
      try { localStorage.setItem(NOTIFICATION_STORAGE_KEY, JSON.stringify(migrated)); }
      catch { /* The migrated settings still apply for this page session. */ }
    }
    return migrated;
  }
  catch { return structuredClone(DEFAULTS); }
}
export function getNotificationSettings() { return structuredClone(settings); }
export function getDefaultNotificationSettings() { return structuredClone(DEFAULTS); }
export function getNotificationAudioStatus() { return audioContext?.state || audioStatus; }
export const AUDIBILITY_TEST_STORAGE_KEY = 'winner_predict_audio_audibility_tests';
export function soundVerificationKey(profile, volume) {
  const level = Number(volume ?? settings.volume);
  return `${profile}|${(Number.isFinite(level) ? level : Number(settings.volume)).toFixed(2)}`;
}
export function voiceVerificationKey(options = {}) {
  const requestedURI = options.voiceURI ?? settings.voiceURI;
  const available = listVoices();
  const selected = available.find((item) => item.voiceURI === requestedURI)
    || (!requestedURI ? available.find((item) => item.default) : null);
  const language = Object.hasOwn(options, 'language')
    ? (options.language || selected?.lang || navigator.language)
    : (settings.language || selected?.lang || navigator.language);
  const rate = Number(options.rate ?? settings.rate).toFixed(2);
  const pitch = Number(options.pitch ?? settings.pitch).toFixed(2);
  const volume = Number(options.voiceVolume ?? options.volume ?? settings.voiceVolume).toFixed(2);
  return `${requestedURI || selected?.voiceURI || 'default'}|${language}|${rate}|${pitch}|${volume}`;
}
export function recordAudibilityResult(kind, profile, result, voiceKey = '', volume = null) {
  const entry = { kind, profile, voiceKey, ...(kind === 'sound' ? { volume: Number(volume ?? settings.volume), verificationKey: soundVerificationKey(profile, volume) } : {}), result, recordedAt: new Date().toISOString(), claim: 'user-confirmed' };
  try { localStorage.setItem(AUDIBILITY_TEST_STORAGE_KEY, JSON.stringify([entry, ...JSON.parse(localStorage.getItem(AUDIBILITY_TEST_STORAGE_KEY) || '[]')].slice(0, 100))); } catch { /* Keep the visible result even if browser storage is unavailable. */ }
  return entry;
}
export function notificationSettingsVerificationErrors(value) {
  let records = [];
  try { records = JSON.parse(localStorage.getItem(AUDIBILITY_TEST_STORAGE_KEY) || '[]'); } catch { records = []; }
  const errors = [];
  if (value.mode !== 'voice-only') {
    const required = [...new Map(Object.entries(value.events || {}).filter(([, config]) => config.sound
      && value.enabled && !value.muted && config.enabled)
      .map(([eventType, config]) => {
        const volume = Number(config.volume ?? value.volume);
        const profile = config.sound;
        return [soundVerificationKey(profile, volume), { profile, volume, eventType }];
      })).values()];
    const missing = required.filter(({ profile, volume }) => records.find((item) => item.kind === 'sound'
      && item.verificationKey === soundVerificationKey(profile, volume))?.result !== 'YES');
    if (missing.length) errors.push(`Hear and confirm each selected sound at its configured volume before enabling live audio: ${missing.map(({ profile, volume }) => `${profile} at ${Math.round(volume * 100)}%`).join(', ')}.`);
  }
  if (value.enabled && !value.muted && value.voiceEnabled && value.mode !== 'sound-only') {
    // `value.volume` is the master sound volume; voice confirmation is keyed
    // to the voice-specific volume instead.
    const key = voiceVerificationKey({
      voiceURI: value.voiceURI,
      language: value.language,
      rate: value.rate,
      pitch: value.pitch,
      voiceVolume: value.voiceVolume,
    });
    if (records.find((item) => item.kind === 'voice' && item.voiceKey === key)?.result !== 'YES') errors.push('Preview and confirm the selected browser voice before enabling live voice announcements.');
  }
  return errors;
}
export function saveNotificationSettings(value) {
  const errors = notificationSettingsVerificationErrors(value);
  if (errors.length) throw new Error(errors.join(' '));
  settings = { ...DEFAULTS, ...value, schemaVersion: 7,
    events: { ...DEFAULTS.events, ...(value.events || {}) }, behaviourAlerts: { ...DEFAULTS.behaviourAlerts, ...(value.behaviourAlerts || {}) } };
  try { localStorage.setItem(NOTIFICATION_STORAGE_KEY, JSON.stringify(settings)); } catch { /* Private browsing can disable storage. */ }
  notifyListeners({ kind: 'settings', settings: getNotificationSettings() });
  return getNotificationSettings();
}
export function subscribeNotifications(listener) { listeners.add(listener); return () => listeners.delete(listener); }
export function getNotificationHistory() { return [...history]; }
function notifyListeners(value) { listeners.forEach((listener) => { try { listener(value); } catch { /* UI listeners are isolated. */ } }); }
function clearNotificationHistory() {
  history.length = 0;
  persistHistory();
  notifyListeners({ kind: 'history-cleared' });
}
if (typeof window !== 'undefined') {
  window.addEventListener(API_CREDENTIAL_CHANGED_EVENT, clearNotificationHistory);
  window.addEventListener('storage', (event) => {
    if (event.key === API_TOKEN_STORAGE_KEY) clearNotificationHistory();
    if (event.key === NOTIFICATION_STORAGE_KEY) {
      settings = loadSettings();
      notifyListeners({ kind: 'settings', settings: getNotificationSettings() });
    }
  });
}
export function shouldShowNotificationToast(event, preferences = settings) {
  return event?.test === true || (preferences.visualEnabled !== false && preferences.events?.[event.eventType]?.enabled === true);
}

export function normalizeNotification(input = {}) {
  const type = EVENTS[input.eventType] ? input.eventType : 'system.error';
  const [source, defaultMessage, sound] = EVENTS[type];
  const timestamp = input.timestamp || new Date().toISOString();
  const id = String(input.id || `${type}:${input.roundId || input.deduplicationKey || timestamp}`);
  const defaultSeverity = ['rules.warning', 'system.stale', 'collector.stale', 'shadow.stopped', 'shadow.paused', 'shadow.alert', 'adie.integrity'].includes(type) ? 'warning'
    : ['system.disconnected', 'system.error', 'collector.disconnected'].includes(type) ? 'critical' : 'normal';
  return { id, eventType: type, source: input.source || source, timestamp,
    roundId: input.roundId == null ? null : String(input.roundId), direction: input.direction || null,
    severity: input.severity || defaultSeverity,
    message: String(input.message || defaultMessage), soundProfile: input.soundProfile || settings.events[type]?.sound || sound,
    voiceAnnouncement: input.voiceAnnouncement || null, deduplicationKey: String(input.deduplicationKey || id),
    synthetic: input.synthetic === true, test: input.test === true };
}

export function enableNotificationAudio() {
  if (audioContext?.state === 'running')
    return Promise.resolve({ status: 'ready', message: 'Browser audio is ready in this tab.' });
  if (audioEnablePromise) return audioEnablePromise;
  audioEnablePromise = initializeNotificationAudio();
  const current = audioEnablePromise;
  return current.finally(() => { if (audioEnablePromise === current) audioEnablePromise = null; });
}

async function initializeNotificationAudio() {
  try {
    const Ctx = window.AudioContext || window.webkitAudioContext;
    if (!Ctx) {
      const result = { status: 'unsupported', message: 'Web Audio is not supported in this browser.' };
      audioStatus = result.status;
      notifyListeners({ kind: 'audio-state', ...result });
      return result;
    }
    if (!audioContext || audioContext.state === 'closed') {
      audioContext = new Ctx();
      audioContext.onstatechange = () => {
        audioStatus = audioContext?.state || 'unavailable';
        notifyListeners({ kind: 'audio-state', status: audioStatus,
        message: audioContext?.state === 'running' ? 'Browser audio is ready in this tab.'
          : `Browser audio is ${audioContext?.state || 'unavailable'}.` });
      };
    }
    if (audioContext.state !== 'running') {
      audioUnlockPending = true;
      await audioContext.resume();
      audioUnlockPending = false;
    }
    if (audioContext.state !== 'running') throw new Error(`Audio engine is ${audioContext.state}.`);
    audioUnlockGranted = true;
    audioStatus = 'running';
    const oscillator = audioContext.createOscillator();
    const gain = audioContext.createGain();
    gain.gain.value = 0;
    oscillator.connect(gain); gain.connect(audioContext.destination);
    oscillator.start(); oscillator.stop(audioContext.currentTime + 0.01);
    const result = { status: 'ready', message: 'Audio engine initialized; browser playback is permitted.' };
    notifyListeners({ kind: 'audio-state', ...result });
    drainSoundQueue();
    return result;
  } catch (error) {
    audioUnlockPending = false;
    const result = { status: 'error', message: error?.message || 'Browser blocked audio playback.' };
    audioStatus = audioContext?.state || 'error';
    notifyListeners({ kind: 'audio-state', ...result });
    soundQueue.splice(0).forEach((queued) => queued.resolve({ status: 'blocked', message: result.message }));
    return result;
  }
}

function playTone(profileId, volume = settings.volume, priority = 'normal') {
  return new Promise((resolve) => {
    const profile = SOUND_PROFILES.find((item) => item.id === profileId);
    if (!profile) return resolve({ status: 'failed', message: 'Sound profile was not found.' });
    const item = { profile, volume, priority: priority === 'critical' ? 2 : priority === 'important' ? 1 : 0, sequence: soundSequence++, resolve, timer: null, oscillators: [] };
    if (soundQueue.length + (currentSound ? 1 : 0) >= 6) {
      const removable = soundQueue.findIndex((queued) => queued.priority < item.priority);
      if (removable < 0) return resolve({ status: 'queue_full', message: 'Notification sound queue is full.' });
      soundQueue.splice(removable, 1)[0].resolve({ status: 'queue_dropped', message: 'A higher priority notification replaced this queued sound.' });
    }
    soundQueue.push(item);
    drainSoundQueue();
  });
}

function drainSoundQueue() {
  if (currentSound || soundQueue.length === 0) return;
  soundQueue.sort((a, b) => b.priority - a.priority || a.sequence - b.sequence);
  const item = soundQueue.shift();
  if (audioUnlockPending) { soundQueue.unshift(item); return; }
  if (!audioContext || audioContext.state !== 'running') {
    if (['suspended', 'interrupted'].includes(audioContext?.state) && audioUnlockGranted) {
      soundQueue.unshift(item);
      if (audioResumePending) return;
      audioResumePending = true;
      audioContext.resume().then(() => {
        audioResumePending = false;
        if (audioContext?.state === 'running') drainSoundQueue();
        else {
          const message = `Browser audio is ${audioContext?.state || 'unavailable'}; click Enable Sounds to resume.`;
          soundQueue.splice(0).forEach((queued) => queued.resolve({ status: 'blocked', message }));
          notifyListeners({ kind: 'audio-state', status: 'blocked', message });
        }
      }).catch((error) => {
        audioResumePending = false;
        const message = error?.message || 'Click Enable Sounds to resume browser audio.';
        soundQueue.splice(0).forEach((queued) => queued.resolve({ status: 'blocked', message }));
        notifyListeners({ kind: 'audio-state', status: 'blocked', message });
      });
      return;
    }
    const message = audioContext
      ? 'Browser audio is locked. Click once anywhere in the app or press Enable Sounds.'
      : 'Audio has not been unlocked in this tab. Click once anywhere in the app or press Enable Sounds.';
    item.resolve({ status: 'blocked', message });
    notifyListeners({ kind: 'audio-state', status: 'blocked', message });
    drainSoundQueue(); return;
  }
  try {
    const now = audioContext.currentTime;
    item.profile.notes.forEach(([frequency, offset, duration]) => {
      const oscillator = audioContext.createOscillator(); const gain = audioContext.createGain();
      oscillator.type = 'sine'; oscillator.frequency.value = frequency;
      const start = now + offset;
      gain.gain.setValueAtTime(0.0001, start);
      gain.gain.linearRampToValueAtTime(Math.max(0.0001, Math.min(1, item.volume)) * 0.22, start + 0.015);
      gain.gain.exponentialRampToValueAtTime(0.0001, start + duration);
      oscillator.connect(gain); gain.connect(audioContext.destination); item.oscillators.push(oscillator);
      oscillator.start(start); oscillator.stop(start + duration + 0.02);
    });
    currentSound = item;
    item.timer = window.setTimeout(() => {
      if (currentSound !== item) return;
      currentSound = null;
      item.resolve({ status: 'ended', message: 'Playback ended. Speaker audibility still needs your confirmation.' });
      notifyListeners({ kind: 'playback', status: 'ended', profile: item.profile.name });
      drainSoundQueue();
    }, Math.max(...item.profile.notes.map((note) => note[1] + note[2])) * 1000 + 50);
    notifyListeners({ kind: 'playback', status: 'started', profile: item.profile.name });
  } catch (error) {
    currentSound = null;
    item.resolve({ status: 'failed', message: error?.message || 'Audio playback failed.' });
    drainSoundQueue();
  }
}

export async function previewSound(profileId, volume = settings.volume) {
  const audio = await enableNotificationAudio();
  if (audio.status !== 'ready') return audio;
  return playTone(profileId, volume);
}
export function playConfiguredSound(eventType, profileId = null) {
  const config = settings.events[eventType] || {};
  if (!settings.enabled || settings.muted || !config.enabled || settings.mode === 'voice-only' || quietNow())
    return Promise.resolve({ status: 'suppressed', message: 'Live sound is disabled, muted, quiet, or not enabled for this event.' });
  return playTone(profileId || config.sound, Number(config.volume ?? settings.volume),
    eventType === 'behaviour.possible_missing' ? 'important' : 'normal').then((result) => {
    if (result.status === 'blocked') notifyListeners({ kind: 'audio-blocked', eventType, result });
    return result;
  });
}
export function stopSound() {
  if (currentSound) {
    const item = currentSound; currentSound = null; window.clearTimeout(item.timer);
    item.oscillators.forEach((oscillator) => { try { oscillator.stop(); } catch { /* Already ended. */ } });
    item.resolve({ status: 'stopped', message: 'Playback stopped by the user.' });
  }
  soundQueue.splice(0).forEach((item) => item.resolve({ status: 'stopped', message: 'Playback stopped by the user.' }));
  notifyListeners({ kind: 'playback', status: 'stopped' });
}
export function stopVoice() {
  speechQueue = [];
  if (activeUtterance) {
    activeUtterance.__notificationSettled = true;
    if (activeUtterance.__notificationWatchdog) clearTimeout(activeUtterance.__notificationWatchdog);
    activeUtterance = null;
  }
  if (window.speechSynthesis) window.speechSynthesis.cancel();
  speaking = false;
}
export function listVoices() { return window.speechSynthesis?.getVoices?.() || []; }
export function enableNotificationVoice() {
  const speech = window.speechSynthesis;
  if (!speech || !window.SpeechSynthesisUtterance)
    return { status: 'unsupported', message: 'Speech synthesis is not supported in this browser.' };
  try {
    // Browsers may suspend speech after startup or when another tab interrupted it.
    // Call this from a user gesture to satisfy autoplay policies before live events.
    speech.resume?.();
    return { status: 'ready', message: 'Browser voice is ready in this tab.' };
  } catch (error) {
    return { status: 'blocked', message: error?.message || 'Browser blocked speech playback.' };
  }
}
export function speakText(text, options = {}) {
  if (!window.speechSynthesis || !window.SpeechSynthesisUtterance) return { status: 'unsupported', message: 'Speech synthesis is not supported in this browser.' };
  const utterance = new SpeechSynthesisUtterance(text);
  const verificationKey = voiceVerificationKey(options);
  utterance.notificationVerificationKey = verificationKey;
  const availableVoices = listVoices();
  const requestedVoiceURI = options.voiceURI ?? settings.voiceURI;
  const voice = availableVoices.find((item) => item.voiceURI === requestedVoiceURI)
    || availableVoices.find((item) => item.default) || availableVoices[0];
  if (voice) utterance.voice = voice;
  utterance.lang = options.language || settings.language || voice?.lang || navigator.language;
  utterance.rate = Number(options.rate ?? settings.rate); utterance.pitch = Number(options.pitch ?? settings.pitch);
  utterance.volume = Number(options.volume ?? settings.voiceVolume);
  utterance.__notificationSettled = false;
  utterance.__notificationStarted = false;
  utterance.__notificationWatchdog = null;
  utterance.onstart = () => {
    utterance.__notificationStarted = true;
    notifyListeners({ kind: 'voice', status: 'started', verificationKey: utterance.notificationVerificationKey });
  };
  utterance.onend = () => finishSpeech(utterance, 'ended');
  utterance.onerror = (event) => finishSpeech(utterance, 'failed', event.error || 'Speech playback failed.');
  if (speaking) { if (options.priority === 'critical') stopVoice(); else { if (speechQueue.length >= 4) speechQueue.shift(); speechQueue.push(utterance); return { status: 'queued' }; } }
  runSpeech(utterance); return { status: 'started' };
}
function finishSpeech(utterance, status, message = '') {
  if (utterance.__notificationSettled) return;
  utterance.__notificationSettled = true;
  if (utterance.__notificationWatchdog) clearTimeout(utterance.__notificationWatchdog);
  if (activeUtterance === utterance) activeUtterance = null;
  speaking = false;
  notifyListeners({ kind: 'voice', status, verificationKey: utterance.notificationVerificationKey,
    ...(message ? { message } : {}) });
  const next = speechQueue.shift();
  if (next) runSpeech(next);
}
function runSpeech(utterance) {
  speaking = true;
  activeUtterance = utterance;
  try {
    const speech = window.speechSynthesis;
    speech.resume?.();
    // Some browser speech engines neither start nor emit an error after sleeping.
    // Release the queue visibly instead of leaving every later announcement stuck.
    utterance.__notificationWatchdog = setTimeout(() => {
      const message = utterance.__notificationStarted
        ? 'Speech playback timed out before it finished.'
        : 'Browser speech did not start. Click the page once, then retry the voice preview.';
      if (!utterance.__notificationStarted) speech.cancel?.();
      finishSpeech(utterance, 'failed', message);
    }, Math.max(12000, String(utterance.text || '').length * 150));
    speech.speak(utterance);
  } catch (error) {
    finishSpeech(utterance, 'failed', error?.message || 'Speech playback failed.');
  }
}

export async function publishNotification(input) {
  const event = normalizeNotification(input);
  const now = Date.now();
  if (!event.test && !event.synthetic) {
    const previous = seen.get(event.deduplicationKey);
    const cooldown = Math.max(0, Number(settings.events[event.eventType]?.cooldownSeconds ?? settings.cooldownSeconds)) * 1000;
    if (previous && now - previous < cooldown) return { status: 'suppressed', reason: 'duplicate_or_cooldown', event };
    seen.set(event.deduplicationKey, now);
    if (seen.size > 1000) seen.delete(seen.keys().next().value);
  }
  const config = settings.events[event.eventType] || {};
  const record = { ...event, receivedAt: new Date().toISOString(), playback: 'not_played' };
  history.unshift(record); if (history.length > 100) history.length = 100;
  persistHistory();
  notifyListeners({ kind: 'event', event: record });
  const testAllowed = event.test === true;
  const priorityAllowed = settings.priority === 'all' || (settings.priority === 'important' && event.severity !== 'normal') || (settings.priority === 'critical' && event.severity === 'critical');
  const soundAllowed = testAllowed ? input.soundEnabled !== false : input.soundEnabled !== false && settings.enabled && !settings.muted && config.enabled && priorityAllowed;
  const voiceAllowed = testAllowed ? input.voiceEnabled === true : input.voiceEnabled === false ? false : settings.enabled && !settings.muted && config.enabled && settings.voiceEnabled && priorityAllowed;
  if (soundAllowed && (testAllowed || settings.mode !== 'voice-only') && (testAllowed || !quietNow())) {
    const result = await playTone(event.soundProfile, testAllowed ? Number(input.volume ?? settings.volume) : Number(config.volume ?? settings.volume), event.severity === 'critical' ? 'critical' : event.severity === 'warning' ? 'important' : 'normal');
    record.playback = result.status; record.playbackMessage = result.message;
    persistHistory();
    notifyListeners({ kind: 'playback-result', event: record, result });
  }
  if (voiceAllowed && (testAllowed || settings.mode !== 'sound-only') && (testAllowed || !quietNow())) {
    const defaultMessage = EVENTS[event.eventType]?.[1];
    const configuredMessage = config.message !== defaultMessage ? config.message : null;
    speakText(input.voiceMessage || configuredMessage || event.voiceAnnouncement || config.message || event.message,
      { priority: event.severity === 'critical' ? 'critical' : 'normal', ...(testAllowed ? input.voiceOptions : {}) });
  }
  return { status: 'delivered', event: record };
}
function quietNow() {
  if (!settings.quietStart || !settings.quietEnd) return false;
  const current = new Date().toTimeString().slice(0, 5);
  return settings.quietStart <= settings.quietEnd ? current >= settings.quietStart && current < settings.quietEnd
    : current >= settings.quietStart || current < settings.quietEnd;
}

export function handleLiveTransportEvent(frame) {
  if (!frame || typeof frame !== 'object') return;
  const type = frame.type || '';
  if (type.startsWith('shadow:')) {
    const eventName = type.slice(7); const trade = frame.trade || {}; const roundId = trade.target_round_id || frame.round_id;
    if (eventName === 'started' || eventName === 'resumed') void publishNotification({ eventType: 'shadow.activated', id: `${type}:${frame.session?.session_id}`, deduplicationKey: `${type}:${frame.session?.session_id}` });
    if (eventName === 'paper_trade') {
      const identity = trade.execution_id || trade.trade_id || roundId;
      void publishNotification({ eventType: 'shadow.created', id: `${type}:${identity}`, roundId, deduplicationKey: `${type}:${identity}`, message: `New SHADOW prediction created for round #${roundId || 'unknown'}.` });
    }
    if (eventName === 'result') {
      const outcome = String(trade.result || trade.status || '').toUpperCase();
      const won = outcome === 'WIN';
      if (['WIN', 'LOSS'].includes(outcome)) void publishNotification({ eventType: won ? 'shadow.win' : 'shadow.loss', id: `${type}:${trade.execution_id || trade.trade_id || roundId}`, roundId, deduplicationKey: `${type}:${trade.execution_id || trade.trade_id || roundId}` });
    }
    if (eventName === 'paused') {
      const id = `${type}:${frame.session?.session_id || frame.reason || Date.now()}`;
      void publishNotification({ eventType: 'shadow.paused', id, deduplicationKey: id, source: 'SHADOW Predictions', message: `SHADOW session paused${frame.reason ? `: ${frame.reason}` : '.'}` });
    }
    if (eventName === 'stopped') {
      const reason = frame.reason || frame.session?.stop_reason || frame.session?.selection_stop_reason || '';
      const id = `${type}:${frame.session?.session_id || reason || Date.now()}:${reason}`;
      void publishNotification({ eventType: 'shadow.stopped', id, deduplicationKey: id, source: 'SHADOW Predictions', message: reason ? `SHADOW session stopped: ${reason}.` : 'SHADOW session stopped.' });
    }
    if (eventName === 'alert') {
      const reason = typeof frame.alert === 'string' ? frame.alert : frame.alert?.message || frame.alert?.code || JSON.stringify(frame.alert || 'SHADOW alert');
      const id = `${type}:${frame.session?.session_id || 'session'}:${reason}`;
      void publishNotification({ eventType: 'shadow.alert', id, deduplicationKey: id, source: 'SHADOW Predictions', message: `SHADOW alert: ${reason}.`, severity: 'warning' });
    }
  }
  if (type === 'rules-engine:error' || type === 'health:alert') {
    const detail = typeof frame.alert === 'string' ? frame.alert : frame.alert?.message || frame.alert?.code || frame.error || frame.message || 'Live service reported an error.';
    const id = `${type}:${frame.correlation_id || frame.event_id || Date.now()}`;
    void publishNotification({ eventType: 'system.error', id, deduplicationKey: id,
      source: type === 'rules-engine:error' ? 'Rules Engine' : 'System Health',
      roundId: frame.round_id || frame.roundId || frame.correlation_id,
      message: `${type === 'rules-engine:error' ? 'Rules Engine error' : 'System alert'}: ${detail}.`, severity: 'critical' });
  }
  if (type === 'health:updated') {
    const components = frame.health?.components || {};
    for (const name of ['history', 'browser']) {
      const collector = components[name];
      if (!collector) continue;
      const state = String(collector.state || collector.metadata?.status || 'UNKNOWN').toUpperCase();
      const previous = collectorStates.get(name);
      const source = collector.name || name;
      if (['ERROR', 'OFFLINE'].includes(state) && previous !== state) {
        const id = `health:down:${name}:${previous || 'UNKNOWN'}:${state}:${Date.now()}`;
        void publishNotification({ eventType: 'system.disconnected', id, deduplicationKey: id, source, message: `${source} data collector is ${state.toLowerCase()}.` });
      } else if (state === 'STALE' && previous !== state) {
        const id = `health:stale:${name}:${Date.now()}`;
        void publishNotification({ eventType: 'system.stale', id, deduplicationKey: id, source, message: `${source} has not received fresh data.` });
      } else if (state === 'HEALTHY' && ['ERROR', 'OFFLINE', 'STALE'].includes(previous)) {
        const id = `health:up:${name}:${collector.last_success || Date.now()}`;
        void publishNotification({ eventType: 'system.restored', id, deduplicationKey: id, source, message: `${source} data connection is healthy again.` });
      }
      collectorStates.set(name, state);
    }
  }
}

export const NOTIFICATION_EVENT_DEFINITIONS = EVENTS;
