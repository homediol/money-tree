import { useReducer } from 'react';
import Card from './Card.jsx';
import { createPanelSimulation, panelSimulationReducer, TEST_MULTIPLIERS } from '../lib/panelSimulation.js';

const buttonClass = 'rounded border border-amber-500/40 bg-amber-500/10 px-3 py-2 text-sm font-semibold text-amber-200 hover:bg-amber-500/20 disabled:cursor-not-allowed disabled:opacity-40';
const inputClass = 'mt-1 w-full rounded border border-zinc-700 bg-zinc-950 px-3 py-2 text-sm disabled:opacity-50';
const statusLabels = { idle: 'Ready', queued: 'Queued', active: 'Active', cashed_out: 'Cashed out', lost: 'Lost' };

export default function BetPanelSimulation() {
  const [state, dispatch] = useReducer(panelSimulationReducer, undefined, createPanelSimulation);
  const editable = state.phase === 'ready';
  const multiplier = TEST_MULTIPLIERS[state.tick];
  return (
    <Card title="Two-panel simulation" action={<span className="rounded border border-amber-400/40 px-2 py-1 text-xs font-bold text-amber-200">TEST CREDITS ONLY</span>}>
      <p className="text-sm text-zinc-400">Test placing, replacing, cancelling and cashing out on both panels. This local simulation uses fictional credits and a scripted 3.00× crash. It never sends a wager to the browser or execution API. Refreshing resets the test.</p>
      <div className="my-4 flex flex-wrap items-center justify-between gap-3 rounded-lg border border-amber-500/20 bg-amber-500/5 p-3">
        <div>
          <div className="text-xs text-zinc-400">Available test credits</div>
          <div className="text-xl font-semibold" data-testid="test-balance">{state.balance.toLocaleString('en-US', { maximumFractionDigits: 2 })}</div>
        </div>
        <div className="text-right" aria-live="polite">
          <div className="text-xs text-zinc-400">Round {state.round} · {state.phase}</div>
          <div className="text-2xl font-semibold text-amber-200">{multiplier.toFixed(2)}×</div>
        </div>
      </div>
      <div className="grid gap-4 md:grid-cols-2">
        {state.panels.map(panel => (
          <section key={panel.slot} aria-label={`Test panel ${panel.slot + 1}`} className="rounded-lg border border-zinc-700 bg-zinc-900 p-4">
            <div className="mb-3 flex items-center justify-between gap-2">
              <h3 className="font-semibold">Panel {panel.slot + 1}</h3>
              <span role="status" className="rounded bg-zinc-800 px-2 py-1 text-xs text-amber-200">{statusLabels[panel.status]}</span>
            </div>
            <div className="grid gap-3 sm:grid-cols-2">
              <label className="text-xs text-zinc-300">Stake (test credits)
                <input type="number" min="1" step="1" value={panel.amount} disabled={!editable} className={inputClass}
                  onChange={event => dispatch({ type: 'CONFIGURE', slot: panel.slot, field: 'amount', value: event.target.value })} />
              </label>
              <label className="text-xs text-zinc-300">Auto cash-out multiplier
                <input type="number" min="1.01" max="100" step="0.01" value={panel.cashout} disabled={!editable || !panel.autoCashout} className={inputClass}
                  onChange={event => dispatch({ type: 'CONFIGURE', slot: panel.slot, field: 'cashout', value: event.target.value })} />
              </label>
            </div>
            <label className="my-3 flex items-center gap-2 text-sm text-zinc-300">
              <input type="checkbox" checked={panel.autoCashout} disabled={!editable}
                onChange={event => dispatch({ type: 'CONFIGURE', slot: panel.slot, field: 'autoCashout', value: event.target.checked })} />
              Enable test auto cash-out
            </label>
            {panel.bet && <p className="mb-3 text-xs text-zinc-400" data-testid={`committed-${panel.slot}`}>
              Committed: {panel.bet.amount} credits · {panel.bet.target == null ? 'manual cash-out' : `${panel.bet.target.toFixed(2)}× auto cash-out`}
              {panel.status === 'queued' && ' · Input changes apply when you replace the queued bet.'}
            </p>}
            <div className="flex flex-wrap gap-2">
              <button type="button" className={buttonClass} disabled={!editable || !['idle', 'queued'].includes(panel.status)}
                onClick={() => dispatch({ type: panel.status === 'queued' ? 'REPLACE' : 'PLACE', slot: panel.slot })}>
                {panel.status === 'queued' ? 'Replace queued test bet' : 'Place test bet'}
              </button>
              <button type="button" className={buttonClass} disabled={!editable || panel.status !== 'queued'}
                onClick={() => dispatch({ type: 'CANCEL', slot: panel.slot })}>Cancel test bet</button>
              <button type="button" className={buttonClass} disabled={state.phase !== 'running' || panel.status !== 'active'}
                onClick={() => dispatch({ type: 'CASH_OUT', slot: panel.slot })}>Cash out test bet</button>
            </div>
            {panel.status === 'cashed_out' && <p className="mt-3 text-sm text-emerald-300">Returned {panel.payout} test credits (includes stake).</p>}
            {panel.status === 'lost' && <p className="mt-3 text-sm text-rose-300">Scripted crash reached. Returned 0 test credits.</p>}
          </section>
        ))}
      </div>
      <div className="mt-4 flex flex-wrap gap-2">
        <button type="button" className={buttonClass} disabled={!editable || !state.panels.some(panel => panel.status === 'queued')}
          onClick={() => dispatch({ type: 'START' })}>Start test round</button>
        <button type="button" className={buttonClass} disabled={state.phase !== 'running'}
          onClick={() => dispatch({ type: 'ADVANCE' })}>
          {state.phase === 'running' ? `Advance to ${TEST_MULTIPLIERS[state.tick + 1].toFixed(2)}×` : 'Advance test round'}
        </button>
        <button type="button" className={buttonClass} disabled={state.phase !== 'crashed'}
          onClick={() => dispatch({ type: 'NEXT_ROUND' })}>Next test round</button>
        <button type="button" className={buttonClass} onClick={() => dispatch({ type: 'RESET' })}>Reset simulation</button>
      </div>
      {state.error && <p role="alert" className="mt-3 text-sm text-rose-300">{state.error}</p>}
      <details className="mt-4" open>
        <summary className="cursor-pointer text-sm font-semibold text-zinc-300">Simulation log ({state.events.length})</summary>
        {state.events.length === 0 ? <p className="mt-2 text-xs text-zinc-500">Queue a test bet on either panel to begin.</p> :
          <ol aria-label="Simulation log" className="mt-2 max-h-56 space-y-1 overflow-auto text-xs text-zinc-400">
            {state.events.map(event => <li key={event.id} className="rounded bg-zinc-950/60 p-2">
              Round {event.round}{event.panel != null ? ` · Panel ${event.panel}` : ''}: {event.message}
            </li>)}
          </ol>}
      </details>
    </Card>
  );
}
