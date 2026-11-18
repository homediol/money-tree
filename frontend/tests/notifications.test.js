import test from 'node:test';
import assert from 'node:assert/strict';
import {
  enableNotificationAudio, enableNotificationVoice, getNotificationSettings, normalizeNotification,
  NOTIFICATION_STORAGE_KEY, publishNotification, saveNotificationSettings,
  handleLiveTransportEvent, getNotificationHistory, listVoices, speakText,
  subscribeNotifications, previewSound, getDefaultNotificationSettings,
  notificationSettingsVerificationErrors, recordAudibilityResult, shouldShowNotificationToast, soundVerificationKey, voiceVerificationKey,
} from '../src/services/notifications.js';

test('normalizes supported event identity and preserves verified round IDs', () => {
  const event = normalizeNotification({ eventType: 'shadow.win', id: 'win:42', roundId: 42 });
  assert.equal(event.id, 'win:42');
  assert.equal(event.roundId, '42');
  assert.equal(event.source, 'SHADOW');
  assert.equal(event.soundProfile, 'success');
});

test('persists notification preferences through browser storage', () => {
  const values = new Map();
  globalThis.localStorage = { getItem: (key) => values.get(key) || null, setItem: (key, value) => values.set(key, value) };
  const current = getNotificationSettings();
  const saved = saveNotificationSettings({ ...current, enabled: false, volume: 0.31 });
  assert.equal(saved.enabled, false);
  assert.equal(JSON.parse(values.get(NOTIFICATION_STORAGE_KEY)).volume, 0.31);
});

test('legacy notification settings migrate voice announcements to ON', async () => {
  const values = new Map([[NOTIFICATION_STORAGE_KEY, JSON.stringify({
    schemaVersion: 6, enabled: true, muted: false, voiceEnabled: true, mode: 'voice-only', events: {},
  })]]);
  globalThis.localStorage = { getItem: (key) => values.get(key) || null, setItem: (key, value) => values.set(key, value) };
  const migratedModule = await import(`../src/services/notifications.js?voice-migration=${Date.now()}`);
  const migrated = migratedModule.getNotificationSettings();
  assert.equal(migrated.enabled, true);
  assert.equal(migrated.muted, false);
  assert.equal(migrated.voiceEnabled, true);
  assert.equal(migrated.mode, 'sound-voice');
  assert.ok(Object.values(migrated.events).every((event) => event.enabled));
  assert.equal(JSON.parse(values.get(NOTIFICATION_STORAGE_KEY)).schemaVersion, 7);
});

test('live settings require user-confirmed sound and voice audibility', () => {
  globalThis.window = { speechSynthesis: { getVoices: () => [] } };
  const draft = getDefaultNotificationSettings(); draft.enabled = true;
  draft.mode = 'sound-voice'; draft.voiceEnabled = false;
  assert.match(notificationSettingsVerificationErrors(draft).join(' '), /Hear and confirm each selected sound/);
  for (const config of Object.values(draft.events).filter((item) => item.enabled))
    recordAudibilityResult('sound', config.sound, 'YES', '', config.volume ?? draft.volume);
  assert.deepEqual(notificationSettingsVerificationErrors(draft), []);
  draft.voiceEnabled = true;
  assert.match(notificationSettingsVerificationErrors(draft).join(' '), /Preview and confirm the selected browser voice/);
  recordAudibilityResult('voice', '', 'YES', voiceVerificationKey(draft));
  assert.deepEqual(notificationSettingsVerificationErrors(draft), []);
});

test('sound verification is scoped to the selected profile and configured volume, with the latest answer winning', () => {
  const values = new Map();
  globalThis.localStorage = { getItem: (key) => values.get(key) || null, setItem: (key, value) => values.set(key, value) };
  globalThis.window = { speechSynthesis: { getVoices: () => [] } };
  const draft = getDefaultNotificationSettings();
  draft.enabled = true; draft.voiceEnabled = false; draft.mode = 'sound-voice';
  for (const config of Object.values(draft.events)) config.enabled = false;
  draft.events['shadow.win'] = { ...draft.events['shadow.win'], enabled: true, sound: 'success', volume: 0.55 };
  assert.match(notificationSettingsVerificationErrors(draft).join(' '), /success at 55%/);
  recordAudibilityResult('sound', 'success', 'YES', '', 0.55);
  assert.deepEqual(notificationSettingsVerificationErrors(draft), []);
  draft.events['shadow.win'].volume = 0.5;
  assert.match(notificationSettingsVerificationErrors(draft).join(' '), /success at 50%/);
  recordAudibilityResult('sound', 'success', 'YES', '', 0.55);
  assert.match(notificationSettingsVerificationErrors(draft).join(' '), /success at 50%/);
  recordAudibilityResult('sound', 'success', 'YES', '', 0.5);
  assert.deepEqual(notificationSettingsVerificationErrors(draft), []);
  recordAudibilityResult('sound', 'success', 'NO', '', 0.5);
  assert.match(notificationSettingsVerificationErrors(draft).join(' '), /success at 50%/);
  draft.events['shadow.win'].volume = 0;
  assert.match(notificationSettingsVerificationErrors(draft).join(' '), /success at 0%/);
  recordAudibilityResult('sound', 'success', 'YES', '', 0);
  assert.equal(soundVerificationKey('success', 0), 'success|0.00');
  assert.deepEqual(notificationSettingsVerificationErrors(draft), []);
});

test('visual alerts work independently from the audio master and can be disabled explicitly', () => {
  const preferences = getDefaultNotificationSettings();
  const event = { eventType: 'shadow.win', test: false };
  assert.equal(shouldShowNotificationToast(event, preferences), true);
  preferences.visualEnabled = false;
  assert.equal(shouldShowNotificationToast(event, preferences), false);
  preferences.visualEnabled = true;
  preferences.events['shadow.win'].enabled = false;
  assert.equal(shouldShowNotificationToast(event, preferences), false);
  assert.equal(shouldShowNotificationToast({ ...event, test: true }, preferences), true);
});

test('synthetic notification events bypass duplicate suppression and stay marked test-only', async () => {
  const first = await publishNotification({ eventType: 'shadow.win', id: 'synthetic-1', deduplicationKey: 'same-test', test: true, synthetic: true, soundEnabled: false });
  const second = await publishNotification({ eventType: 'shadow.win', id: 'synthetic-2', deduplicationKey: 'same-test', test: true, synthetic: true, soundEnabled: false });
  assert.equal(first.status, 'delivered');
  assert.equal(second.status, 'delivered');
  assert.equal(first.event.test, true);
});

test('production events suppress a repeated stable identity', async () => {
  const id = `prod-${Date.now()}`;
  const input = { eventType: 'system.error', id, deduplicationKey: id };
  const first = await publishNotification(input);
  const repeated = await publishNotification(input);
  assert.equal(first.status, 'delivered');
  assert.equal(repeated.status, 'suppressed');
});

test('audio unlock reports unsupported browser engines without claiming playback', async () => {
  globalThis.window = {};
  const result = await enableNotificationAudio();
  assert.equal(result.status, 'unsupported');
  assert.match(result.message, /not supported/);
});

test('audio unlock reports autoplay restrictions from the browser', async () => {
  globalThis.window = { AudioContext: class { get state() { return 'suspended'; } resume() { throw new Error('NotAllowedError'); } } };
  const result = await enableNotificationAudio();
  assert.equal(result.status, 'error');
  assert.match(result.message, /NotAllowedError/);
});

test('voice unlock resumes the browser speech engine from a user gesture', async () => {
  let resumed = 0;
  class FakeUtterance {}
  globalThis.SpeechSynthesisUtterance = FakeUtterance;
  globalThis.window = { SpeechSynthesisUtterance: FakeUtterance, speechSynthesis: { resume: () => { resumed += 1; } } };
  assert.equal(enableNotificationVoice().status, 'ready');
  assert.equal(resumed, 1);
});

test('generated sound preview reports unsupported when the browser has no audio engine', async () => {
  globalThis.window = {};
  const result = await previewSound('success');
  assert.equal(result.status, 'unsupported');
});

test('voice availability reflects browser-provided voices and handles unsupported speech', () => {
  globalThis.window = { speechSynthesis: { getVoices: () => [] } };
  assert.deepEqual(listVoices(), []);
  assert.equal(speakText('test').status, 'unsupported');
});

test('a speech error releases the next queued announcement', () => {
  const utterances = [];
  const speechSynthesis = { getVoices: () => [], speak: (utterance) => utterances.push(utterance), cancel: () => {} };
  class FakeUtterance { constructor(text) { this.text = text; } }
  globalThis.SpeechSynthesisUtterance = FakeUtterance;
  globalThis.window = { speechSynthesis, SpeechSynthesisUtterance: FakeUtterance };
  assert.equal(speakText('first').status, 'started');
  assert.equal(speakText('queued').status, 'queued');
  utterances[0].onerror({ error: 'network' });
  assert.equal(utterances.length, 2);
  assert.equal(utterances[1].text, 'queued');
  utterances[1].onend();
});

test('a speech engine that never responds times out and releases queued announcements', () => {
  const utterances = [];
  const speechSynthesis = { getVoices: () => [], speak: (utterance) => utterances.push(utterance), cancel: () => {} };
  class FakeUtterance { constructor(text) { this.text = text; } }
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  let timeoutCallback;
  globalThis.setTimeout = (callback) => { timeoutCallback = callback; return 1; };
  globalThis.clearTimeout = () => {};
  globalThis.SpeechSynthesisUtterance = FakeUtterance;
  globalThis.window = { speechSynthesis, SpeechSynthesisUtterance: FakeUtterance };
  try {
    assert.equal(speakText('stuck').status, 'started');
    assert.equal(speakText('next').status, 'queued');
    timeoutCallback();
    assert.equal(utterances.length, 2);
    assert.equal(utterances[1].text, 'next');
    utterances[1].onend();
  } finally {
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
  }
});

test('existing SHADOW result messages route through the notification manager', async () => {
  const id = `shadow:result:${Date.now()}`;
  handleLiveTransportEvent({ type: 'shadow:result', trade: { execution_id: id, status: 'PAPER_RECONCILED', result: 'LOSS', target_round_id: 'verified-17' } });
  await new Promise((resolve) => setImmediate(resolve));
  assert.ok(getNotificationHistory().some((event) => event.id === `shadow:result:${id}` && event.roundId === 'verified-17' && event.eventType === 'shadow.loss'));
});

test('unsubscribed listeners do not receive future events', async () => {
  let calls = 0;
  const unsubscribe = subscribeNotifications(() => { calls += 1; });
  unsubscribe();
  await publishNotification({ eventType: 'shadow.win', id: `cleanup-${Date.now()}`, test: true, synthetic: true, soundEnabled: false });
  assert.equal(calls, 0);
});

test('normalization uses the selected event sound profile when the publisher omits one', () => {
  const values = new Map();
  globalThis.localStorage = { getItem: (key) => values.get(key) || null, setItem: (key, value) => values.set(key, value) };
  const current = getNotificationSettings();
  const events = { ...current.events, 'behaviour.high': { ...current.events['behaviour.high'], sound: 'critical' } };
  recordAudibilityResult('sound', 'critical', 'YES', '', current.events['behaviour.high'].volume ?? current.volume);
  saveNotificationSettings({ ...current, enabled: false, events });
  assert.equal(normalizeNotification({ eventType: 'behaviour.high' }).soundProfile, 'critical');
});

test('voice announcements use dynamic summaries and still honor a custom event message', async () => {
  const values = new Map();
  globalThis.localStorage = { getItem: (key) => values.get(key) || null, setItem: (key, value) => values.set(key, value) };
  const utterances = [];
  const speechSynthesis = { getVoices: () => [], speak: (utterance) => utterances.push(utterance), cancel: () => {} };
  class FakeUtterance { constructor(text) { this.text = text; } }
  globalThis.SpeechSynthesisUtterance = FakeUtterance;
  globalThis.window = { speechSynthesis, SpeechSynthesisUtterance: FakeUtterance };
  const draft = getDefaultNotificationSettings();
  draft.enabled = true;
  draft.mode = 'voice-only';
  draft.voiceEnabled = true;
  draft.events['behaviour.window_completed'].enabled = true;
  draft.events['behaviour.window_completed'].cooldownSeconds = 0;
  recordAudibilityResult('voice', 'speech-synthesis', 'YES', voiceVerificationKey(draft));
  saveNotificationSettings(draft);
  await publishNotification({ eventType: 'behaviour.window_completed', id: `voice-window-${Date.now()}`,
    voiceAnnouncement: 'Window 4 completed.' });
  assert.equal(utterances[0].text, 'Window 4 completed.');
  utterances[0].onend();
  const updated = getNotificationSettings();
  updated.events['behaviour.window_completed'].message = 'Custom window status.';
  saveNotificationSettings(updated);
  await publishNotification({ eventType: 'behaviour.window_completed', id: `voice-window-custom-${Date.now()}`,
    voiceAnnouncement: 'Window 5 completed.' });
  assert.equal(utterances[1].text, 'Custom window status.');
  utterances[1].onend();
});

test('sound engine initializes once for overlapping unlock clicks', async () => {
  let contextsCreated = 0;
  class FakeAudioContext {
    constructor() { contextsCreated += 1; this.state = 'suspended'; this.currentTime = 1; this.destination = {}; }
    resume() { this.state = 'running'; return Promise.resolve(); }
    createOscillator() { return { connect() {}, start() {}, stop() {} }; }
    createGain() { return { gain: { value: 1 }, connect() {} }; }
  }
  globalThis.localStorage = { getItem: () => null, setItem() {} };
  globalThis.window = { AudioContext: FakeAudioContext, addEventListener() {}, removeEventListener() {} };
  const freshNotifications = await import(`../src/services/notifications.js?audio-unlock=${Date.now()}`);
  const results = await Promise.all([freshNotifications.enableNotificationAudio(), freshNotifications.enableNotificationAudio()]);
  assert.deepEqual(results.map(({ status }) => status), ['ready', 'ready']);
  assert.equal(contextsCreated, 1);
});
