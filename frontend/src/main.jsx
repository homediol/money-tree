import React, { useEffect, useState } from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter, NavLink, Navigate, Route, Routes } from 'react-router-dom';
import { ErrorBoundary } from 'react-error-boundary';
import { Toaster } from 'sonner';
import { Activity, BarChart3, Bot, Brain, FlaskConical, GitBranch, History, LayoutDashboard, ReceiptText, Settings, ShieldCheck, Signal } from 'lucide-react';
import './styles/index.css';
import Dashboard from './pages/Dashboard.jsx';
import SystemDashboard from './pages/SystemDashboard.jsx';
import LiveHistory from './pages/LiveHistory.jsx';
import PatternExplorer from './pages/PatternExplorer.jsx';
import SignalHistory from './pages/SignalHistory.jsx';
import ModelPerformance from './pages/ModelPerformance.jsx';
import Betting from './pages/Betting.jsx';
import BetPanelSimulation from './components/BetPanelSimulation.jsx';
import SystemSettings from './pages/SystemSettings.jsx';
import SystemHealth from './components/SystemHealth.jsx';
import AppErrorFallback from './components/AppErrorFallback.jsx';
import EvidenceExplorer from './pages/EvidenceExplorer.jsx';
import DecisionEngine from './pages/DecisionEngine.jsx';
import BetResults from './pages/BetResults.jsx';
import Backtesting from './pages/Backtesting.jsx';
import StrategyResearch from './pages/StrategyResearch.jsx';
import ShadowMode from './pages/ShadowMode.jsx';
import LiveActivation from './pages/LiveActivation.jsx';
import Recovery from './pages/Recovery.jsx';
import { BACKEND_RESTORED_EVENT } from './services/backendConnection.js';

const nav = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/history', label: 'Round History', icon: History },
  { to: '/patterns', label: 'Patterns', icon: BarChart3 },
  { to: '/signals', label: 'Signals', icon: Signal },
  { to: '/models', label: 'Models', icon: Brain },
  { to: '/evidence', label: 'Evidence', icon: ShieldCheck },
  { to: '/decisions', label: 'Decisions', icon: GitBranch },
  { to: '/betting', label: 'Betting', icon: Bot },
  { to: '/panel-test', label: 'Panel Test', icon: FlaskConical },
  { to: '/results', label: 'Bet Results', icon: ReceiptText },
  { to: '/backtesting', label: 'Backtesting', icon: FlaskConical },
  { to: '/research', label: 'Strategy Research', icon: FlaskConical },
  { to: '/shadow', label: 'Shadow Mode', icon: FlaskConical },
  { to: '/live', label: 'Live Activation', icon: ShieldCheck },
  { to: '/recovery', label: 'Recovery', icon: ShieldCheck },
  { to: '/settings', label: 'Settings', icon: Settings },
];

function Shell() {
  const [backendEpoch, setBackendEpoch] = useState(0);

  useEffect(() => {
    const restore = () => setBackendEpoch((value) => value + 1);
    window.addEventListener(BACKEND_RESTORED_EVENT, restore);
    return () => window.removeEventListener(BACKEND_RESTORED_EVENT, restore);
  }, []);

  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100">
      <SystemHealth />
      <aside className="fixed inset-y-0 left-0 hidden w-64 border-r border-zinc-800 bg-zinc-950/95 p-5 lg:block">
        <div className="flex items-center gap-3 border-b border-zinc-800 pb-5">
          <div className="grid h-10 w-10 place-items-center rounded bg-emerald-500 text-zinc-950">
            <Activity size={22} />
          </div>
          <div>
            <div className="text-lg font-semibold">Winner Predict</div>
            <div className="text-[10px] font-bold uppercase tracking-[0.18em] text-emerald-300">Operations Console</div>
          </div>
        </div>
        <nav className="mt-8 space-y-1">
          {nav.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} className={({ isActive }) => `group flex items-center gap-3 rounded-lg border px-3 py-2.5 text-sm font-medium transition ${isActive ? 'border-emerald-400/20 bg-emerald-400/10 text-emerald-200' : 'border-transparent text-zinc-400 hover:border-zinc-800 hover:bg-zinc-900 hover:text-zinc-100'}`}>
              <Icon size={18} className="transition-transform group-hover:scale-110" /> {label}
            </NavLink>
          ))}
        </nav>
      </aside>
      <header className="sticky top-0 z-10 border-b border-zinc-800 bg-zinc-950/90 px-4 py-3 backdrop-blur lg:hidden">
        <div className="font-semibold">Winner Predict</div>
        <div className="mt-3 flex gap-2 overflow-x-auto pb-1">
          {nav.map(({ to, label }) => (
            <NavLink key={to} to={to} className={({ isActive }) => `whitespace-nowrap rounded px-3 py-1.5 text-xs ${isActive ? 'bg-emerald-500 text-zinc-950' : 'bg-zinc-900 text-zinc-300'}`}>
              {label}
            </NavLink>
          ))}
        </div>
      </header>
      <main className="lg:pl-64">
        <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
          <Routes key={backendEpoch}>
            <Route path="/" element={<SystemDashboard />} />
            <Route path="/analytics" element={<Dashboard />} />
            <Route path="/history" element={<LiveHistory />} />
            <Route path="/patterns" element={<PatternExplorer />} />
            <Route path="/signals" element={<SignalHistory />} />
            <Route path="/models" element={<ModelPerformance />} />
            <Route path="/evidence" element={<EvidenceExplorer />} />
            <Route path="/decisions" element={<DecisionEngine />} />
            <Route path="/betting" element={<Betting />} />
            <Route path="/panel-test" element={<BetPanelSimulation />} />
            <Route path="/results" element={<BetResults />} />
            <Route path="/backtesting" element={<Backtesting />} />
            <Route path="/research" element={<StrategyResearch />} />
            <Route path="/shadow" element={<ShadowMode />} />
            <Route path="/live" element={<LiveActivation />} />
            <Route path="/recovery" element={<Recovery />} />
            <Route path="/settings" element={<SystemSettings />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ErrorBoundary FallbackComponent={AppErrorFallback} onReset={() => window.location.assign('/')}>
      <BrowserRouter>
        <Shell />
        <Toaster richColors position="top-right" theme="dark" />
      </BrowserRouter>
    </ErrorBoundary>
  </React.StrictMode>,
);
