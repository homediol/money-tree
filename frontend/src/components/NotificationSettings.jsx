import { useEffect, useMemo, useRef, useState } from 'react';
import { BellRing, Check, CircleStop, Play, RotateCcw, Save, Volume2 } from 'lucide-react';
import {
  getDefaultNotificationSettings, getNotificationAudioStatus, getNotificationSettings, listVoices, NOTIFICATION_EVENT_DEFINITIONS, previewSound, stopSound,
  publishNotification, saveNotificationSettings, SOUND_PROFILES, speakText, stopVoice,
  subscribeNotifications, enableNotificationAudio, enableNotificationVoice, recordAudibilityResult, soundVerificationKey, voiceVerificationKey,
} from '../services/notifications.js';

const btn = 'rounded-lg border border-zinc-700 bg-zinc-900 px-3 py-2 text-sm text-zinc-200 hover:border-emerald-400/50';
const field = 'rounded-lg border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm text-zinc-100';

export default function NotificationSettings() {
  const [saved, setSaved] = useState(getNotificationSettings);
  const [draft, setDraft] = useState(saved);
  const [audioStatus, setAudioStatus] = useState(() => {
    const status = getNotificationAudioStatus();
    return { status, message: status === 'running' ? 'Browser audio is ready in this tab.' : 'Audio requires one user click in each browser tab.' };
  });
  const [playback, setPlayback] = useState('Idle');
  const [selectedSound, setSelectedSound] = useState(null);
  const [voices, setVoices] = useState(listVoices());
  const [previewText, setPreviewText] = useState('High dominance detected.');
  const [testEvent, setTestEvent] = useState('shadow.win');
  const [testModule, setTestModule] = useState('SHADOW');
  const [testSound, setTestSound] = useState('success');
  const [testMessage, setTestMessage] = useState('Synthetic test notification. No prediction or performance data will change.');
  const [testVoice, setTestVoice] = useState(false);
  const [audibility, setAudibility] = useState('');
  const [voiceAudibility, setVoiceAudibility] = useState('');
  const [completedSound, setCompletedSound] = useState(null);
  const [activeVoiceVerificationKey, setActiveVoiceVerificationKey] = useState('');
  const [completedVoiceVerificationKey, setCompletedVoiceVerificationKey] = useState('');
  const previewSequence = useRef(0);
  const activeVoiceVerificationKeyRef = useRef('');
  const [saveError, setSaveError] = useState('');
  const [dirty, setDirty] = useState(false);
  const eventIds = useMemo(() => Object.keys(NOTIFICATION_EVENT_DEFINITIONS), []);
  const update = (transform) => { setDraft((current) => transform(current)); setDirty(true); };

  useEffect(() => {
    const refresh = () => setVoices(listVoices());
    const speech = window.speechSynthesis;
    const previousHandler = speech?.onvoiceschanged;
    if (speech?.addEventListener) speech.addEventListener('voiceschanged', refresh);
    else if (speech) speech.onvoiceschanged = refresh;
    const unsubscribe = subscribeNotifications((item) => {
      if (item.kind === 'playback') setPlayback(item.status === 'started' ? `Playing ${item.profile}` : item.status);
      if (item.kind === 'playback-result') { setPlayback(`${item.result.status}: ${item.result.message}`); }
      if (item.kind === 'audio-state') setAudioStatus({ status: item.status, message: item.message });
      if (item.kind === 'voice') {
        setPlayback(`Voice ${item.status}${item.message ? `: ${item.message}` : ''}`);
        if (item.status === 'ended' && item.verificationKey === activeVoiceVerificationKeyRef.current) {
          setCompletedVoiceVerificationKey(item.verificationKey);
          activeVoiceVerificationKeyRef.current = '';
        }
      }
    });
    return () => { if (speech?.removeEventListener) speech.removeEventListener('voiceschanged', refresh); else if (speech?.onvoiceschanged === refresh) speech.onvoiceschanged = previousHandler || null; unsubscribe(); };
  }, []);

  const chooseEvent = (eventType, key, value) => update((current) => ({ ...current, events: { ...current.events, [eventType]: { ...current.events[eventType], [key]: value } } }));
  const test = async () => {
    const previewId = ++previewSequence.current;
    setAudibility('');
    setCompletedSound(null);
    if (testSound && draft.mode !== 'voice-only') await enableNotificationAudio();
    if (testVoice) {
      const key = voiceVerificationKey({ ...draft, ...previewVoiceOptions() });
      activeVoiceVerificationKeyRef.current = key;
      setActiveVoiceVerificationKey(key);
      setCompletedVoiceVerificationKey('');
    }
    setPlayback('Synthetic test notification is processing.');
    const result = await publishNotification({ eventType: testEvent, source: `${testModule} · TEST`, message: testMessage,
      id: `test:${Date.now()}`, deduplicationKey: `test:${Date.now()}`, soundProfile: testSound, synthetic: true, test: true,
      soundEnabled: draft.mode !== 'voice-only', voiceEnabled: testVoice, volume: draft.volume,
      voiceMessage: testMessage, voiceOptions: previewVoiceOptions() });
    if (result.status !== 'delivered') setPlayback(`Test event ${result.status}.`);
    else if (previewSequence.current === previewId && result.event?.playback === 'ended')
      setCompletedSound({ profileId: testSound, volume: Number(draft.volume), previewId, source: 'test' });
  };
  const save = () => {
    try {
      const next = saveNotificationSettings(draft);
      setSaved(next); setDraft(next); setDirty(false); setSaveError('');
      if (next.enabled && !next.muted && next.voiceEnabled && next.mode !== 'sound-only') enableNotificationVoice();
      if (next.enabled && !next.muted && next.mode !== 'voice-only') void enableNotificationAudio().then(setAudioStatus);
    } catch (error) { setSaveError(error.message); }
  };
  const cancel = () => { setDraft(saved); setDirty(false); setSelectedSound(null); };
  const reset = () => { setDraft(getDefaultNotificationSettings()); setDirty(true); };
  const categories = [...new Set(eventIds.map((id) => NOTIFICATION_EVENT_DEFINITIONS[id][0]))];
  const hasSoundConfirmation = (profile, volume) => {
    try { return JSON.parse(localStorage.getItem('winner_predict_audio_audibility_tests') || '[]')
      .find((item) => item.kind === 'sound' && item.verificationKey === soundVerificationKey(profile, volume))?.result === 'YES'; }
    catch { return false; }
  };
  const previewVoiceOptions = () => {
    const voice = voices.find((item) => item.voiceURI === draft.voiceURI)
      || (!draft.voiceURI ? voices.find((item) => item.default) : null);
    return { voiceURI: draft.voiceURI, language: draft.language || voice?.lang || navigator.language,
      rate: draft.rate, pitch: draft.pitch, volume: draft.voiceVolume };
  };
  const audioGateMessage = !draft.enabled ? 'Master audio is OFF.'
    : draft.muted ? 'Audio is MUTED.'
      : draft.mode === 'voice-only' ? 'Mode is VOICE ONLY; sound tones are disabled.'
        : draft.priority === 'critical' ? 'Priority is CRITICAL ONLY.'
          : draft.quietStart && draft.quietEnd ? `Quiet hours are set for ${draft.quietStart}–${draft.quietEnd}.` : 'Master sound is enabled.';
  const previewVoice = (replay = false) => {
    setVoiceAudibility('');
    const options = previewVoiceOptions();
    const key = voiceVerificationKey({ ...draft, ...options });
    activeVoiceVerificationKeyRef.current = key;
    setActiveVoiceVerificationKey(key);
    setCompletedVoiceVerificationKey('');
    const result = speakText(previewText, options);
    setPlayback(`Voice${replay ? ' replay' : ''} ${result.status}${result.message ? `: ${result.message}` : ''}`);
  };
  const previewTone = async (profileId, volume, replay = false) => {
    const previewId = ++previewSequence.current;
    const level = Number(volume);
    setAudibility('');
    setCompletedSound(null);
    setPlayback(replay ? 'Replaying generated tone…' : 'Tone generated; requesting browser playback…');
    const result = await previewSound(profileId, level);
    if (previewSequence.current !== previewId) return result;
    setAudioStatus(result.status === 'ended'
      ? { status: 'ready', message: 'Audio engine is ready; confirm you can hear the tone.' }
      : { status: result.status, message: result.message });
    setPlayback(`${replay ? 'Replay ' : ''}${result.status}: ${result.message}`);
    if (result.status === 'ended') setCompletedSound({ profileId, volume: level, previewId, source: 'library' });
    return result;
  };

  return <section className="space-y-5" aria-label="Notification settings">
    <header className="rounded-xl border border-cyan-400/20 bg-gradient-to-r from-cyan-950/30 to-zinc-900 p-5">
      <div className="flex flex-wrap items-center justify-between gap-3"><div><p className="text-xs font-bold uppercase tracking-widest text-cyan-200">Sound & Voice Notification System</p><h2 className="mt-1 text-xl font-semibold">Notification Test Center</h2><p className="mt-1 text-sm text-zinc-400">Preview, compare and confirm sounds before saving them for live events.</p></div>
        <button className="rounded-lg bg-cyan-300 px-4 py-2 text-sm font-bold text-zinc-950" onClick={async () => { enableNotificationVoice(); setAudioStatus(await enableNotificationAudio()); }}><Volume2 size={16} className="mr-2 inline" />Enable sound + voice in this tab</button></div>
      <p role="status" className={`mt-3 text-sm ${audioStatus.status === 'running' || audioStatus.status === 'ready' ? 'text-emerald-300' : 'text-amber-200'}`}>Audio engine: {audioStatus.status} · {audioStatus.message}</p>
      <p className={`mt-1 text-xs ${draft.enabled && !draft.muted && draft.mode !== 'voice-only' ? 'text-emerald-200' : 'text-amber-200'}`}>Live sound gate: {audioGateMessage}</p>
      <p className="mt-1 text-xs text-zinc-500">A running audio engine and completed playback do not confirm speaker output. Confirm audibility below.</p>
    </header>

    <section className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4 sm:p-5">
      <div className="flex items-center gap-2"><BellRing size={18} className="text-emerald-300"/><h3 className="font-semibold">Sound library</h3></div>
      <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-3">{SOUND_PROFILES.map((profile) => <article key={profile.id} className={`rounded-lg border p-3 ${selectedSound === profile.id ? 'border-emerald-400/50 bg-emerald-950/20' : 'border-zinc-800 bg-zinc-950/60'}`}>
        <div className="flex items-start justify-between gap-2"><div><h4 className="text-sm font-medium">{profile.name}</h4><p className="mt-1 text-xs text-zinc-500">{profile.event}</p></div><span className="text-[10px] uppercase text-zinc-500">Generated tone</span></div>
        <div className="mt-3 flex flex-wrap gap-2"><button type="button" className={btn} onClick={() => { void previewTone(profile.id, draft.volume); }}><Play size={14} className="mr-1 inline"/>Play</button><button type="button" className={btn} onClick={() => { stopSound(); setPlayback('Playback stopped.'); }}><CircleStop size={14} className="mr-1 inline"/>Stop</button><button type="button" className={btn} onClick={() => { void previewTone(profile.id, draft.volume, true); }}><RotateCcw size={14} className="mr-1 inline"/>Replay</button><button type="button" disabled={!hasSoundConfirmation(profile.id, draft.volume)} className="rounded-lg border border-emerald-500/30 px-3 py-2 text-sm text-emerald-200 disabled:opacity-40" onClick={() => { chooseEvent(testEvent, 'sound', profile.id); chooseEvent(testEvent, 'volume', Number(draft.volume)); setSelectedSound(profile.id); setPlayback(`${profile.name} selected at ${Math.round(draft.volume * 100)}% for ${NOTIFICATION_EVENT_DEFINITIONS[testEvent][1]}. Save settings to activate.`); }}><Check size={14} className="mr-1 inline"/>Use This Sound</button></div>
      </article>)}</div>
      <label className="mt-4 block text-sm text-zinc-300">Master sound volume <span className="text-zinc-500">{Math.round(draft.volume * 100)}%</span><input className="mt-2 block w-full accent-emerald-400" type="range" min="0" max="1" step="0.01" value={draft.volume} onChange={(e) => update((s) => ({ ...s, volume: Number(e.target.value) }))}/></label>
      <p aria-live="polite" className="mt-3 rounded-lg bg-zinc-950 p-3 text-sm text-zinc-300">Playback status: {playback}</p>
      {completedSound?.source === 'library' && playback.includes('Playback ended') && <div className="mt-3 flex flex-wrap items-center gap-2 text-sm"><span>Did you hear {SOUND_PROFILES.find((s) => s.id === completedSound.profileId)?.name || 'the sound'} at {Math.round(completedSound.volume * 100)}%?</span><button className={btn} onClick={() => recordConfirmation('sound', 'YES')}>Yes, I heard it</button><button className={btn} onClick={() => recordConfirmation('sound', 'NO')}>No, I did not hear it</button>{audibility && <b className="text-cyan-200">Test result: {audibility}</b>}</div>}
    </section>

    <section className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4 sm:p-5">
      <h3 className="font-semibold">Voice preview</h3><p className="mt-1 text-xs text-zinc-500">Voices come from this browser and operating system. Unsupported languages are not substituted or relabeled.</p>
      {!('speechSynthesis' in window) && <p className="mt-3 text-sm text-amber-300">Speech synthesis is unavailable in this browser.</p>}
      <div className="mt-3 grid gap-3 md:grid-cols-2">
        <label className="text-sm text-zinc-300">Available browser voice<select className={`${field} mt-1 block w-full`} value={draft.voiceURI} onChange={(e) => update((s) => ({ ...s, voiceURI: e.target.value }))}><option value="">Browser default ({navigator.language})</option>{voices.map((voice) => <option key={voice.voiceURI} value={voice.voiceURI}>{voice.name} · {voice.lang}{voice.default ? ' · default' : ''}</option>)}</select></label>
        <label className="text-sm text-zinc-300">Language<select className={`${field} mt-1 block w-full`} value={draft.language} onChange={(e) => update((s) => ({ ...s, language: e.target.value }))}><option value="">Voice default</option>{[...new Set(voices.map((voice) => voice.lang))].map((language) => <option key={language} value={language}>{language}</option>)}</select></label>
      </div>
      <label className="mt-3 block text-sm text-zinc-300">Preview announcement<input className={`${field} mt-1 block w-full`} value={previewText} onChange={(e) => setPreviewText(e.target.value)}/></label>
      <div className="mt-3 flex flex-wrap gap-2">{['High dominance detected.', 'Low dominance detected.', 'High window selected.', 'Low window selected.', 'Prediction won.', 'Prediction lost.', 'Rule performance improved.', 'Behaviour transition detected.'].map((text) => <button key={text} className={btn} onClick={() => setPreviewText(text)}>{text}</button>)}</div>
      <div className="mt-4 grid gap-3 sm:grid-cols-3">{[['rate', 'Speech rate', .5, 2], ['pitch', 'Pitch', 0, 2], ['voiceVolume', 'Voice volume', 0, 1]].map(([key, label, min, max]) => <label key={key} className="text-xs text-zinc-400">{label}: {Number(draft[key]).toFixed(1)}<input className="mt-1 block w-full accent-emerald-400" type="range" min={min} max={max} step="0.1" value={draft[key]} onChange={(e) => update((s) => ({ ...s, [key]: Number(e.target.value) }))}/></label>)}</div>
      <div className="mt-4 flex gap-2"><button className={btn} onClick={() => previewVoice()}>Preview voice</button><button className={btn} onClick={() => { stopVoice(); setPlayback('Voice stopped.'); }}>Stop speaking</button><button className={btn} onClick={() => previewVoice(true)}>Replay</button><label className="flex items-center gap-2 text-sm text-zinc-300"><input type="checkbox" checked={draft.voiceEnabled} onChange={(e) => update((s) => ({ ...s, voiceEnabled: e.target.checked }))}/>Enable live voice</label></div>
      {completedVoiceVerificationKey && completedVoiceVerificationKey === activeVoiceVerificationKey && playback.startsWith('Voice ended') && <div className="mt-3 flex flex-wrap items-center gap-2 text-sm"><span>Did you hear the selected voice announcement?</span><button className={btn} onClick={() => recordConfirmation('voice', 'YES')}>Yes, I heard it</button><button className={btn} onClick={() => recordConfirmation('voice', 'NO')}>No, I did not hear it</button>{voiceAudibility && <b className="text-cyan-200">Voice test result: {voiceAudibility}</b>}</div>}
      {!voices.some((voice) => voice.lang.toLowerCase().startsWith('rw')) && <p className="mt-3 text-sm text-amber-300">No Kinyarwanda voice is available in this browser. Select one of the listed voices and languages.</p>}
    </section>

    <section className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4 sm:p-5">
      <h3 className="font-semibold">Safe test notification</h3><p className="mt-1 text-xs text-zinc-500">Synthetic events go through the notification manager and cannot create or settle predictions, rules, behaviour, or ADIE records.</p>
      <div className="mt-3 grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <label className="text-sm text-zinc-300">Module<select className={`${field} mt-1 block w-full`} value={testModule} onChange={(e) => { const module = e.target.value; setTestModule(module); const next = eventIds.find((id) => NOTIFICATION_EVENT_DEFINITIONS[id][0] === module); if (next) setTestEvent(next); }}>{categories.map((c) => <option key={c}>{c}</option>)}</select></label>
        <label className="text-sm text-zinc-300">Event<select className={`${field} mt-1 block w-full`} value={testEvent} onChange={(e) => { setTestEvent(e.target.value); setTestModule(NOTIFICATION_EVENT_DEFINITIONS[e.target.value][0]); }}>{eventIds.filter((id) => NOTIFICATION_EVENT_DEFINITIONS[id][0] === testModule).map((id) => <option key={id} value={id}>{NOTIFICATION_EVENT_DEFINITIONS[id][1]}</option>)}</select></label>
        <label className="text-sm text-zinc-300">Sound<select className={`${field} mt-1 block w-full`} value={testSound} onChange={(e) => setTestSound(e.target.value)}>{SOUND_PROFILES.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select></label>
        <label className="flex items-center gap-2 self-end pb-2 text-sm text-zinc-300"><input type="checkbox" checked={testVoice} onChange={(e) => setTestVoice(e.target.checked)}/>Voice enabled</label>
      </div>
      <label className="mt-3 block text-sm text-zinc-300">Example message<textarea className={`${field} mt-1 block w-full`} rows="2" value={testMessage} onChange={(e) => setTestMessage(e.target.value)}/></label>
      <button className="mt-3 rounded-lg bg-emerald-400 px-4 py-2 font-semibold text-zinc-950" onClick={test}>TEST NOTIFICATION</button>
      {playback.startsWith('blocked') && <p className="mt-2 text-sm text-amber-300">Playback is blocked. Press Enable Sounds and retry the test.</p>}
      {completedSound?.source === 'test' && playback.includes('Playback ended') && <div className="mt-2 flex gap-2 text-sm">Did you hear {SOUND_PROFILES.find((s) => s.id === completedSound.profileId)?.name || 'the test sound'} at {Math.round(completedSound.volume * 100)}%? <button className={btn} onClick={() => recordConfirmation('sound', 'YES')}>Yes, I heard it</button><button className={btn} onClick={() => recordConfirmation('sound', 'NO')}>No, I did not hear it</button>{audibility && <span>Recorded: {audibility}</span>}</div>}
    </section>

    <section className="rounded-xl border border-zinc-800 bg-zinc-900/60 p-4 sm:p-5">
      <h3 className="font-semibold">Live notification controls</h3>
      <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={draft.visualEnabled !== false} onChange={(e) => update((s) => ({ ...s, visualEnabled: e.target.checked }))}/>Visual pop-ups</label>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={draft.enabled} onChange={(e) => update((s) => ({ ...s, enabled: e.target.checked }))}/>Enable sounds & voice</label>
        <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={draft.muted} onChange={(e) => update((s) => ({ ...s, muted: e.target.checked }))}/>Mute sounds & voice</label>
        <label className="text-sm">Mode<select className={`${field} ml-2`} value={draft.mode} onChange={(e) => update((s) => ({ ...s, mode: e.target.value }))}><option value="sound-voice">Sound + voice</option><option value="sound-only">Sound only</option><option value="voice-only">Voice only</option></select></label>
        <label className="text-sm">Default cooldown <input className={`${field} ml-2 w-20`} type="number" min="0" max="3600" value={draft.cooldownSeconds} onChange={(e) => update((s) => ({ ...s, cooldownSeconds: Number(e.target.value) }))}/> sec</label>
        <label className="text-sm">Priority<select className={`${field} ml-2`} value={draft.priority} onChange={(e) => update((s) => ({ ...s, priority: e.target.value }))}><option value="all">All notifications</option><option value="important">Warnings and critical</option><option value="critical">Critical only</option></select></label>
        <label className="text-sm">Quiet hours start <input className={`${field} ml-2`} type="time" value={draft.quietStart} onChange={(e) => update((s) => ({ ...s, quietStart: e.target.value }))}/></label>
        <label className="text-sm">Quiet hours end <input className={`${field} ml-2`} type="time" value={draft.quietEnd} onChange={(e) => update((s) => ({ ...s, quietEnd: e.target.value }))}/></label>
      </div>
      {categories.map((category) => <details key={category} className="mt-4 rounded-lg border border-zinc-800 p-3" open><summary className="cursor-pointer font-medium">{category}</summary><div className="mt-2 space-y-2">{eventIds.filter((id) => NOTIFICATION_EVENT_DEFINITIONS[id][0] === category).map((id) => <div key={id} className="grid gap-2 border-t border-zinc-800 pt-2 sm:grid-cols-[1fr_145px_165px_100px_82px_80px_60px] sm:items-center"><label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={Boolean(draft.events[id]?.enabled)} onChange={(e) => chooseEvent(id, 'enabled', e.target.checked)}/>{NOTIFICATION_EVENT_DEFINITIONS[id][1]}</label><select aria-label={`${id} sound profile`} className={field} value={draft.events[id]?.sound || NOTIFICATION_EVENT_DEFINITIONS[id][2]} onChange={(e) => chooseEvent(id, 'sound', e.target.value)}>{SOUND_PROFILES.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}</select><input aria-label={`${id} voice message`} className={field} value={draft.events[id]?.message || ''} onChange={(e) => chooseEvent(id, 'message', e.target.value)} placeholder="Voice message"/><label className="text-xs text-zinc-400">Vol {Math.round((draft.events[id]?.volume ?? draft.volume) * 100)}%<input aria-label={`${id} volume`} className="block w-full accent-emerald-400" type="range" min="0" max="1" step="0.01" value={draft.events[id]?.volume ?? draft.volume} onChange={(e) => chooseEvent(id, 'volume', Number(e.target.value))}/></label>{id.startsWith('rules.') ? <label className="text-xs text-zinc-400">|net|<input aria-label={`${id} threshold`} className={`${field} mt-1 w-20`} type="number" min="1" max="100" value={draft.events[id]?.threshold ?? 5} onChange={(e) => chooseEvent(id, 'threshold', Number(e.target.value))}/></label> : <span className="text-[10px] text-zinc-600">—</span>}<input aria-label={`${id} cooldown seconds`} className={field} type="number" min="0" max="3600" value={draft.events[id]?.cooldownSeconds ?? 15} onChange={(e) => chooseEvent(id, 'cooldownSeconds', Number(e.target.value))}/><button type="button" className={btn} aria-label={`Preview ${id} sound`} onClick={() => { const profile = draft.events[id]?.sound || NOTIFICATION_EVENT_DEFINITIONS[id][2]; const volume = Number(draft.events[id]?.volume ?? draft.volume); void previewTone(profile, volume); }}>Play</button></div>)}</div></details>)}
    </section>
    {saveError && <p role="alert" className="rounded-lg border border-amber-400/40 bg-amber-950/30 p-3 text-sm text-amber-200">{saveError}</p>}
    <div className="sticky bottom-2 z-10 flex flex-wrap justify-end gap-2 rounded-xl border border-zinc-800 bg-zinc-950/95 p-3 shadow-xl"><span className="mr-auto self-center text-xs text-zinc-500">{dirty ? 'Unsaved notification settings' : 'Settings saved'}{selectedSound ? ` · ${SOUND_PROFILES.find((s) => s.id === selectedSound)?.name} confirmed for draft` : ''}</span><button className={btn} onClick={reset}>Reset to defaults</button><button className={btn} onClick={cancel}>Cancel</button><button className="rounded-lg bg-emerald-400 px-4 py-2 text-sm font-semibold text-zinc-950" onClick={save}><Save size={14} className="mr-1 inline"/>Save settings</button></div>
  </section>;
  function recordConfirmation(kind, result) {
    if (kind === 'sound') {
      if (!completedSound) return;
      recordAudibilityResult(kind, completedSound.profileId, result, '', completedSound.volume);
    } else {
      if (!completedVoiceVerificationKey || completedVoiceVerificationKey !== activeVoiceVerificationKey) return;
      recordAudibilityResult(kind, '', result, completedVoiceVerificationKey);
    }
    if (kind === 'voice') setVoiceAudibility(result); else setAudibility(result);
  }
}
