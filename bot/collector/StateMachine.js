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
 *   RELOGIN → HOME | RESTART_BROWSER | STOPPED
 *   RESTART_BROWSER → STARTING | STOPPED
 *   STOPPED → (terminal)
 */

import { log } from './Logger.js';

export const State = Object.freeze({
  STARTING:       'STARTING',
  LOGIN:          'LOGIN',
  HOME:           'HOME',
  GAME_LOADING:   'GAME_LOADING',
  WAITING_IFRAME: 'WAITING_IFRAME',
  COLLECTING:     'COLLECTING',
  RECOVERING:     'RECOVERING',
  RELOGIN:        'RELOGIN',
  RESTART_BROWSER:'RESTART_BROWSER',
  STOPPED:        'STOPPED',
});

const VALID_TRANSITIONS = {
  [State.STARTING]:        [State.LOGIN, State.HOME, State.RESTART_BROWSER],
  [State.LOGIN]:           [State.HOME, State.RELOGIN, State.RESTART_BROWSER],
  [State.HOME]:            [State.GAME_LOADING, State.RELOGIN, State.RECOVERING],
  [State.GAME_LOADING]:    [State.WAITING_IFRAME, State.RECOVERING, State.RELOGIN],
  [State.WAITING_IFRAME]:  [State.COLLECTING, State.RECOVERING, State.RELOGIN],
  [State.COLLECTING]:      [State.RECOVERING, State.RELOGIN, State.WAITING_IFRAME, State.STOPPED],
  [State.RECOVERING]:      [State.LOGIN, State.HOME, State.GAME_LOADING, State.WAITING_IFRAME, State.RESTART_BROWSER, State.RELOGIN, State.COLLECTING],
  [State.RELOGIN]:         [State.HOME, State.RESTART_BROWSER, State.STOPPED],
  [State.RESTART_BROWSER]: [State.STARTING, State.STOPPED],
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
    // Allow self-transition on WAITING_IFRAME and RECOVERING — they are re-entrant states
    if (this._state === next && (next === State.WAITING_IFRAME || next === State.RECOVERING)) {
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
    for (const fn of this._listeners) { try { fn(next, prev, reason); } catch {} }
  }

  onTransition(fn) {
    this._listeners.push(fn);
    return () => { this._listeners = this._listeners.filter(f => f !== fn); };
  }

  recentHistory(n = 10) {
    return this._history.slice(-n);
  }
}
