import React from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter, NavLink, Route, Routes } from 'react-router-dom';
import { Activity, BarChart3, Bot, Brain, History, LayoutDashboard, Settings, Signal } from 'lucide-react';
import './styles/index.css';
import Dashboard from './pages/Dashboard.jsx';
import LiveHistory from './pages/LiveHistory.jsx';
import PatternExplorer from './pages/PatternExplorer.jsx';
import SignalHistory from './pages/SignalHistory.jsx';
import ModelPerformance from './pages/ModelPerformance.jsx';
import Betting from './pages/Betting.jsx';
import SystemSettings from './pages/SystemSettings.jsx';

const nav = [
  { to: '/', label: 'Dashboard', icon: LayoutDashboard },
  { to: '/history', label: 'Live History', icon: History },
  { to: '/patterns', label: 'Patterns', icon: BarChart3 },
  { to: '/signals', label: 'Signals', icon: Signal },
  { to: '/models', label: 'Models', icon: Brain },
  { to: '/betting', label: 'Betting', icon: Bot },
  { to: '/settings', label: 'Settings', icon: Settings },
];

function Shell() {
  return (
    <div className="min-h-screen bg-zinc-950 text-zinc-100">
      <aside className="fixed inset-y-0 left-0 hidden w-64 border-r border-zinc-800 bg-zinc-950/95 p-5 lg:block">
        <div className="flex items-center gap-3">
          <div className="grid h-10 w-10 place-items-center rounded bg-emerald-500 text-zinc-950">
            <Activity size={22} />
          </div>
          <div>
            <div className="text-lg font-semibold">Winner Predict</div>
            <div className="text-xs uppercase tracking-wide text-emerald-300">Statistical Analysis</div>
          </div>
        </div>
        <nav className="mt-8 space-y-1">
          {nav.map(({ to, label, icon: Icon }) => (
            <NavLink key={to} to={to} className={({ isActive }) => `flex items-center gap-3 rounded px-3 py-2 text-sm transition ${isActive ? 'bg-zinc-800 text-white' : 'text-zinc-400 hover:bg-zinc-900 hover:text-zinc-100'}`}>
              <Icon size={18} /> {label}
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
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/history" element={<LiveHistory />} />
            <Route path="/patterns" element={<PatternExplorer />} />
            <Route path="/signals" element={<SignalHistory />} />
            <Route path="/models" element={<ModelPerformance />} />
            <Route path="/betting" element={<Betting />} />
            <Route path="/settings" element={<SystemSettings />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <BrowserRouter>
      <Shell />
    </BrowserRouter>
  </React.StrictMode>,
);

