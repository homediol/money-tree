/**
 * StateMachine.js — Collector state machine with transition validation.
 *
 * States:
 *   STARTING → LOGIN | HOME
 *   LOGIN → HOME | RELOGIN | RESTART_BROWSER
 *   HOME → GAME_LOADING | RELOGIN | RECOVERING
 *   GAME_LOADING → WAITING_IFRAME | RECOVERING | RELOGIN
 *   WAITING_IFRAME → COLLECTING | RECOVERING | RELOGIN
 *   COLLECTING → RECOVERING | RELOGIN | STOPPED
 *   RECOVERING → LOGIN | HOME | GAME_LOADING | WAITING_IFRAME | RESTART_BROWSER | RELOGIN
 *   RELOGIN → HOME | GAME_LOADING | RESTART_BROWSER | STOPPED
 *   RESTART_BROWSER → STARTING | STOPPED
 *   STOPPED → (terminal)
 */

import { log } from './Logger.js';

export const State = Object.freeze({
  STARTING:       'STARTING',
  LOGIN:          'LOGIN',
  HOME:           'HOME',
  GAME_LOADING:   'GAME_LOADING',
  OFFLINE:        'OFFLINE',
  CHECKING_SESSION: 'CHECKING_SESSION',
  AUTH_REQUIRED:  'AUTH_REQUIRED',
  NAVIGATING_AVIATOR: 'NAVIGATING_AVIATOR',
  WAITING_IFRAME: 'WAITING_IFRAME',
  WAITING_HISTORY: 'WAITING_HISTORY',
  COLLECTING:     'COLLECTING',
  RECOVERING:     'RECOVERING',
  SITE_UNAVAILABLE: 'SITE_UNAVAILABLE',
  STORAGE_UNAVAILABLE: 'STORAGE_UNAVAILABLE',
  STOPPING:       'STOPPING',
  RELOGIN:        'RELOGIN',
  RESTART_BROWSER:'RESTART_BROWSER',
  STOPPED:        'STOPPED',
});

const RECOVERY_STATES = [
  State.OFFLINE, State.CHECKING_SESSION, State.AUTH_REQUIRED,
  State.NAVIGATING_AVIATOR, State.WAITING_IFRAME, State.WAITING_HISTORY,
  State.COLLECTING, State.RECOVERING, State.SITE_UNAVAILABLE,
  State.STORAGE_UNAVAILABLE, State.RELOGIN, State.RESTART_BROWSER,
];

const VALID_TRANSITIONS = {
  [State.STARTING]:        [State.LOGIN, State.HOME, State.RESTART_BROWSER, ...RECOVERY_STATES, State.STOPPING, State.STOPPED],
  [State.LOGIN]:           [State.HOME, State.RELOGIN, State.RESTART_BROWSER, ...RECOVERY_STATES, State.STOPPING, State.STOPPED],
  [State.HOME]:            [State.GAME_LOADING, State.RELOGIN, State.RECOVERING, ...RECOVERY_STATES, State.STOPPING, State.STOPPED],
  [State.GAME_LOADING]:    [State.WAITING_IFRAME, State.RECOVERING, State.RELOGIN, ...RECOVERY_STATES, State.STOPPING, State.STOPPED],
  [State.OFFLINE]:         [State.CHECKING_SESSION, State.SITE_UNAVAILABLE, State.STORAGE_UNAVAILABLE, ...RECOVERY_STATES, State.STOPPING, State.STOPPED],
  [State.CHECKING_SESSION]:[State.LOGIN, State.HOME, State.AUTH_REQUIRED, State.NAVIGATING_AVIATOR, ...RECOVERY_STATES, State.STOPPING, State.STOPPED],
  [State.AUTH_REQUIRED]:   [State.CHECKING_SESSION, State.LOGIN, State.HOME, State.NAVIGATING_AVIATOR, ...RECOVERY_STATES, State.STOPPING, State.STOPPED],
  [State.NAVIGATING_AVIATOR]: [State.WAITING_IFRAME, State.SITE_UNAVAILABLE, ...RECOVERY_STATES, State.STOPPING, State.STOPPED],
  [State.WAITING_IFRAME]:  [State.WAITING_HISTORY, State.COLLECTING, State.RECOVERING, State.AUTH_REQUIRED, State.RELOGIN, ...RECOVERY_STATES, State.STOPPING, State.STOPPED],
  [State.WAITING_HISTORY]: [State.COLLECTING, State.WAITING_IFRAME, State.RECOVERING, State.AUTH_REQUIRED, ...RECOVERY_STATES, State.STOPPING, State.STOPPED],
  [State.COLLECTING]:      [State.RECOVERING, State.RELOGIN, State.AUTH_REQUIRED, State.WAITING_IFRAME, State.OFFLINE, State.SITE_UNAVAILABLE, State.STORAGE_UNAVAILABLE, State.STOPPING, State.STOPPED],
  [State.RECOVERING]:      [State.LOGIN, State.HOME, State.GAME_LOADING, State.WAITING_IFRAME, State.WAITING_HISTORY, State.NAVIGATING_AVIATOR, State.CHECKING_SESSION, State.RESTART_BROWSER, State.RELOGIN, State.AUTH_REQUIRED, State.OFFLINE, State.SITE_UNAVAILABLE, State.STORAGE_UNAVAILABLE, State.COLLECTING, State.STOPPING, State.STOPPED],
  [State.SITE_UNAVAILABLE]: [State.OFFLINE, State.CHECKING_SESSION, State.NAVIGATING_AVIATOR, State.WAITING_IFRAME, State.RECOVERING, State.STORAGE_UNAVAILABLE, ...RECOVERY_STATES, State.STOPPING, State.STOPPED],
  [State.STORAGE_UNAVAILABLE]: [State.CHECKING_SESSION, State.WAITING_IFRAME, State.COLLECTING, State.RECOVERING, ...RECOVERY_STATES, State.STOPPING, State.STOPPED],
  [State.RELOGIN]:         [State.AUTH_REQUIRED, State.CHECKING_SESSION, State.HOME, State.GAME_LOADING, State.RESTART_BROWSER, ...RECOVERY_STATES, State.STOPPING, State.STOPPED],
  [State.RESTART_BROWSER]: [State.STARTING, State.CHECKING_SESSION, State.STOPPING, State.STOPPED],
  [State.STOPPING]:        [State.STOPPED],
  [State.STOPPED]:         [],
};

export class StateMachine {
  constructor() {
    this._state   = State.STARTING;
    this._history = [{ state: State.STARTING, ts: Date.now() }];
    this._listeners = [];
  }

  get current() { return this._state; }

  is(s) { return this._state === s; }

  transition(next, reason = '') {
    const allowed = VALID_TRANSITIONS[this._state] ?? [];
    if (this._state === next && RECOVERY_STATES.includes(next)) {
      return; // silent no-op, don't log or notify
    }
    if (!allowed.includes(next)) {
      log.warn(`Invalid transition ${this._state} → ${next} (reason: ${reason}) — forcing RECOVERING`);
      next = State.RECOVERING;
    }
    const prev = this._state;
    this._state = next;
    const entry = { from: prev, to: next, reason, ts: Date.now() };
    this._history.push(entry);
    if (this._history.length > 100) this._history = this._history.slice(-100);
    log.info(`State: ${prev} → ${next}${reason ? ' (' + reason + ')' : ''}`);
    for (const fn of this._listeners) {
      try { fn(next, prev, reason); }
      catch (error) { log.warn(`State listener failed during ${prev} → ${next}: ${error?.message || error}`); }
    }
  }

  onTransition(fn) {
    this._listeners.push(fn);
    return () => { this._listeners = this._listeners.filter(f => f !== fn); };
  }

  recentHistory(n = 10) {
    return this._history.slice(-n);
  }
}
