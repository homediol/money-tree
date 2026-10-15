import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter, NavLink, Navigate, Route, Routes } from 'react-router-dom';
import { ErrorBoundary } from 'react-error-boundary';
import { Toaster } from 'sonner';
import { Activity, KeyRound, ListChecks, ShieldCheck } from 'lucide-react';
import './styles/index.css';
import RulesEngine from './pages/RulesEngine.jsx';
import LiveHistory from './pages/LiveHistory.jsx';
import Betting from './pages/Betting.jsx';
import BetResults from './pages/BetResults.jsx';
import SystemSettings from './pages/SystemSettings.jsx';
import PlatformConnection from './pages/PlatformConnection.jsx';
import LiveActivation from './pages/LiveActivation.jsx';
import Recovery from './pages/Recovery.jsx';
import SystemHealth from './components/SystemHealth.jsx';
import AppErrorFallback from './components/AppErrorFallback.jsx';
import ApiAccessGate from './components/ApiAccessGate.jsx';

const nav = [
  { to: '/platform', label: 'Login & Aviator', icon: KeyRound },
  { to: '/engine', label: 'Collector & Rules Engine', icon: ListChecks },
  { to: '/executor', label: 'Betting Executor', icon: ShieldCheck },
];

function Shell() {
  return <div className="min-h-screen bg-zinc-950 text-zinc-100">
    <SystemHealth />
    <aside className="fixed inset-y-0 left-0 hidden w-64 overflow-y-auto border-r border-zinc-800 bg-zinc-950/95 p-5 lg:block">
      <div className="flex items-center gap-3 border-b border-zinc-800 pb-5">
        <div className="grid h-10 w-10 place-items-center rounded bg-emerald-500 text-zinc-950"><Activity size={22} /></div>
        <div>
          <div className="text-lg font-semibold">Winner Predict</div>
          <div className="text-[10px] font-bold uppercase tracking-[0.18em] text-emerald-300">Operations Console</div>
        </div>
      </div>
      <nav className="mt-8 space-y-1 pb-6">
        {nav.map(({ to, label, icon: Icon }) => <NavLink key={to} to={to}
          className={({ isActive }) => `flex items-center gap-3 rounded-lg border px-3 py-2.5 text-sm font-medium transition ${isActive ? 'border-emerald-400/20 bg-emerald-400/10 text-emerald-200' : 'border-transparent text-zinc-400 hover:border-zinc-800 hover:bg-zinc-900 hover:text-zinc-100'}`}>
          <Icon size={18} />{label}
        </NavLink>)}
      </nav>
    </aside>
    <header className="sticky top-0 z-10 border-b border-zinc-800 bg-zinc-950/90 px-4 py-3 backdrop-blur lg:hidden">
      <div className="font-semibold">Winner Predict</div>
      <nav className="mt-3 flex gap-2 overflow-x-auto pb-1">
        {nav.map(({ to, label }) => <NavLink key={to} to={to}
          className={({ isActive }) => `whitespace-nowrap rounded px-3 py-1.5 text-xs ${isActive ? 'bg-emerald-500 text-zinc-950' : 'bg-zinc-900 text-zinc-300'}`}>
          {label}
        </NavLink>)}
      </nav>
    </header>
    <main className="lg:pl-64">
      <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
        <Routes>
          <Route path="/platform" element={<PlatformConnection />} />
          <Route path="/engine" element={<div className="space-y-8"><RulesEngine /><section><h2 className="mb-4 text-xl font-semibold">Round history</h2><LiveHistory /></section></div>} />
          <Route path="/executor" element={<div className="space-y-8"><LiveActivation /><Betting /><section><h2 className="mb-4 text-xl font-semibold">Execution recovery</h2><Recovery /></section><section><h2 className="mb-4 text-xl font-semibold">Settled results</h2><BetResults /></section></div>} />
          {/* Preserve direct links while keeping the primary workflow to three components. */}
          <Route path="/" element={<Navigate to="/platform" replace />} />
          <Route path="/rules-engine" element={<RulesEngine />} />
          <Route path="/history" element={<LiveHistory />} />
          <Route path="/betting" element={<Betting />} />
          <Route path="/results" element={<BetResults />} />
          <Route path="/operations" element={<div className="space-y-8"><LiveActivation /><Recovery /></div>} />
          <Route path="/settings" element={<SystemSettings />} />
          <Route path="*" element={<Navigate to="/platform" replace />} />
        </Routes>
      </div>
    </main>
  </div>;
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ErrorBoundary FallbackComponent={AppErrorFallback} onReset={() => window.location.assign('/')}>
      <BrowserRouter>
        <ApiAccessGate><Shell /></ApiAccessGate>
        <Toaster richColors position="top-right" theme="dark" />
      </BrowserRouter>
    </ErrorBoundary>
  </React.StrictMode>,
);
