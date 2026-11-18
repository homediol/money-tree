import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter, NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { ErrorBoundary } from 'react-error-boundary';
import { Toaster } from 'sonner';
import { Activity, BellRing, BrainCircuit, Code2, History, ListChecks, Settings } from 'lucide-react';
import './styles/index.css';
import RulesEngine from './pages/RulesEngine.jsx';
import RulePerformancePage from './pages/RulePerformancePage.jsx';
import LiveHistory from './pages/LiveHistory.jsx';
import SystemSettings from './pages/SystemSettings.jsx';
import RulesSignalApi from './pages/RulesSignalApi.jsx';
import SystemHealth from './components/SystemHealth.jsx';
import AppErrorFallback from './components/AppErrorFallback.jsx';
import ApiAccessGate from './components/ApiAccessGate.jsx';
import BehaviourDetection from './pages/BehaviourDetection.jsx';
import DominanceIntelligence from './pages/DominanceIntelligence.jsx';
import NotificationRuntime from './components/NotificationRuntime.jsx';
import NotificationCenter from './pages/NotificationCenter.jsx';

const nav = [
  { to: '/', label: 'Rules Engine', icon: ListChecks },
  { to: '/rule-performance', label: 'Rule Performance', icon: Activity },
  { to: '/dominance-intelligence', label: 'Dominance Intelligence', icon: BrainCircuit },
  { to: '/history', label: 'Round History', icon: History },
  { to: '/notifications', label: 'Notifications', icon: BellRing },
  { to: '/settings', label: 'Settings', icon: Settings },
  { to: '/rules-signal-api', label: 'Rules Signal API', icon: Code2 },
  { to: '/behaviour', label: 'Behaviour Detection', icon: Activity },
];

function Shell({ behaviourOnly = false }) {
  // Keep the same top-level navigation visible while viewing the independently
  // served Behaviour pages. Clicking a main app link switches back to its
  // normal authenticated shell through AppRoutes.
  const shellNav = nav;
  return <div className="min-h-screen bg-zinc-950 text-zinc-100">
    {!behaviourOnly && <SystemHealth />}
    <aside className="fixed inset-y-0 left-0 hidden w-64 overflow-y-auto border-r border-zinc-800 bg-zinc-950/95 p-5 lg:block">
      <div className="flex items-center gap-3 border-b border-zinc-800 pb-5">
        <div className="grid h-10 w-10 place-items-center rounded bg-emerald-500 text-zinc-950"><Activity size={22} /></div>
        <div>
          <div className="text-lg font-semibold">Winner Predict</div>
          <div className="text-[10px] font-bold uppercase tracking-[0.18em] text-emerald-300">{behaviourOnly ? 'Behaviour Detection' : 'Operations Console'}</div>
        </div>
      </div>
      <nav className="mt-8 space-y-1 pb-6">
        {shellNav.map(({ to, label, icon: Icon }) => <NavLink key={to} to={to} end={to === '/'}
          className={({ isActive }) => `flex items-center gap-3 rounded-lg border px-3 py-2.5 text-sm font-medium transition ${isActive ? 'border-emerald-400/20 bg-emerald-400/10 text-emerald-200' : 'border-transparent text-zinc-400 hover:border-zinc-800 hover:bg-zinc-900 hover:text-zinc-100'}`}>
          <Icon size={18} />{label}
        </NavLink>)}
      </nav>
    </aside>
    <header className="sticky top-0 z-10 border-b border-zinc-800 bg-zinc-950/90 px-4 py-3 backdrop-blur lg:hidden">
      <div className="font-semibold">Winner Predict</div>
      <nav className="mt-3 flex gap-2 overflow-x-auto pb-1">
        {shellNav.map(({ to, label }) => <NavLink key={to} to={to} end={to === '/'}
          className={({ isActive }) => `whitespace-nowrap rounded px-3 py-1.5 text-xs ${isActive ? 'bg-emerald-500 text-zinc-950' : 'bg-zinc-900 text-zinc-300'}`}>
          {label}
        </NavLink>)}
      </nav>
    </header>
    <main className="lg:pl-64">
      <NotificationRuntime behaviourOnly={behaviourOnly} />
      <div className="mx-auto max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
        <Routes>
          {behaviourOnly ? <Route path="/behaviour/*" element={<BehaviourDetection />} /> : <>
          <Route path="/" element={<RulesEngine />} />
          <Route path="/rules-engine" element={<RulesEngine />} />
          <Route path="/rule-performance" element={<RulePerformancePage />} />
          <Route path="/dominance-intelligence" element={<DominanceIntelligence />} />
          <Route path="/history" element={<LiveHistory />} />
          <Route path="/notifications" element={<NotificationCenter />} />
          <Route path="/settings" element={<SystemSettings />} />
          <Route path="/rules-signal-api" element={<RulesSignalApi />} />
          <Route path="*" element={<Navigate to="/" replace />} />
          </>}
        </Routes>
      </div>
    </main>
  </div>;
}

function AppRoutes() {
  const location = useLocation();
  const behaviourRoute = location.pathname === '/behaviour' || location.pathname.startsWith('/behaviour/');
  if (behaviourRoute) return <Shell behaviourOnly />;
  return <ApiAccessGate><Shell /></ApiAccessGate>;
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ErrorBoundary FallbackComponent={AppErrorFallback} onReset={() => window.location.assign('/')}>
      <BrowserRouter>
        <AppRoutes />
        <Toaster richColors position="top-right" theme="dark" />
      </BrowserRouter>
    </ErrorBoundary>
  </React.StrictMode>,
);
