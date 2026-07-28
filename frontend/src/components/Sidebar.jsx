import { Activity, BarChart3, PieChart, TrendingUp, History, RefreshCw, Brain, Bot, ExternalLink, LogOut, ClipboardList, Cpu, LayoutDashboard } from 'lucide-react';
import { useLocation, useNavigate } from 'react-router-dom';
import { logout, getSession } from '../auth.js';

const navItems = [
  { id: 'overview',     label: 'Overview',      icon: Activity    },
  { id: 'performance',  label: 'Performance',   icon: TrendingUp  },
  { id: 'distribution', label: 'Distribution',  icon: PieChart    },
  { id: 'multipliers',  label: 'Multipliers',   icon: BarChart3   },
  { id: 'logs',         label: 'History Log',   icon: History     },
  { id: 'intelligence', label: 'Intelligence',  icon: Cpu         },
];

const PREDICTIONS_PATH = '/predictions';
const ML_DASH_PATH     = '/ml-dashboard';

export default function Sidebar({ activeTab, onTabChange, onRefresh, loading, onTrain, training }) {
  const location = useLocation();
  const navigate = useNavigate();
  const isBotPage         = location.pathname === '/bot';
  const isPredictionsPage = location.pathname === PREDICTIONS_PATH;
  const isMLDashPage      = location.pathname === ML_DASH_PATH;

  return (
    <aside className="hidden w-64 shrink-0 border-r border-line bg-panel/50 backdrop-blur lg:flex lg:flex-col">
      {/* Logo */}
      <div
        className="flex cursor-pointer items-center gap-3 border-b border-line px-6 py-5 transition hover:opacity-80"
        onClick={() => navigate('/')}
      >
        <div className="flex h-9 w-9 items-center justify-center rounded-lg bg-gradient-to-br from-cyan to-acid">
          <Brain className="h-5 w-5 text-ink" />
        </div>
        <div>
          <div className="text-sm font-black text-white">Aviator ML</div>
          <div className="text-[10px] font-semibold uppercase tracking-[0.2em] text-cyan">Console</div>
        </div>
      </div>

      {/* Nav items */}
      <nav className="flex-1 space-y-1 overflow-y-auto px-3 py-4">
        {navItems.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            onClick={() => {
              if (isBotPage || isPredictionsPage || isMLDashPage) navigate('/');
              onTabChange(id);
            }}
            className={`flex w-full items-center gap-3 rounded-lg px-4 py-2.5 text-sm font-semibold transition-all duration-200 ${
              !isBotPage && !isPredictionsPage && !isMLDashPage && activeTab === id
                ? id === 'intelligence'
                  ? 'bg-violet-500/10 text-violet-400 shadow-[inset_0_0_0_1px_rgba(139,92,246,0.25)]'
                  : 'bg-cyan/10 text-cyan shadow-[inset_0_0_0_1px_rgba(53,212,255,0.25)]'
                : 'text-slate-400 hover:bg-slate-800/50 hover:text-white'
            }`}
          >
            <Icon className="h-4 w-4" />
            {label}
          </button>
        ))}

        {/* Separator */}
        <div className="my-3 border-t border-line" />

        {/* Predictions page nav */}
        <button
          onClick={() => navigate(PREDICTIONS_PATH)}
          className={`flex w-full items-center gap-3 rounded-lg px-4 py-2.5 text-sm font-semibold transition-all duration-200 ${
            isPredictionsPage
              ? 'bg-cyan/10 text-cyan shadow-[inset_0_0_0_1px_rgba(53,212,255,0.25)]'
              : 'text-slate-400 hover:bg-slate-800/50 hover:text-white'
          }`}
        >
          <ClipboardList className="h-4 w-4" />
          Predictions
        </button>

        {/* ML Performance Dashboard */}
        <button
          onClick={() => navigate(ML_DASH_PATH)}
          className={`flex w-full items-center gap-3 rounded-lg px-4 py-2.5 text-sm font-semibold transition-all duration-200 ${
            isMLDashPage
              ? 'bg-violet-500/10 text-violet-400 shadow-[inset_0_0_0_1px_rgba(139,92,246,0.25)]'
              : 'text-slate-400 hover:bg-slate-800/50 hover:text-white'
          }`}
        >
          <LayoutDashboard className="h-4 w-4" />
          ML Dashboard
        </button>

        {/* Separator */}
        <div className="my-3 border-t border-line" />

        {/* Bot Control nav */}
        <button
          onClick={() => navigate('/bot')}
          className={`flex w-full items-center gap-3 rounded-lg px-4 py-2.5 text-sm font-semibold transition-all duration-200 ${
            isBotPage
              ? 'bg-acid/10 text-acid shadow-[inset_0_0_0_1px_rgba(156,255,69,0.25)]'
              : 'text-slate-400 hover:bg-slate-800/50 hover:text-white'
          }`}
        >
          <Bot className="h-4 w-4" />
          Bot Control
          <ExternalLink className="ml-auto h-3 w-3 opacity-50" />
        </button>
      </nav>

      {/* Bottom actions */}
      <div className="border-t border-line p-4 space-y-2">
        {/* User badge */}
        <div className="flex items-center gap-2 px-1 py-1 mb-1">
          <div className="flex h-7 w-7 shrink-0 items-center justify-center rounded-full bg-cyan/10 text-xs font-black text-cyan">
            {(getSession()?.username?.[0] ?? 'U').toUpperCase()}
          </div>
          <span className="text-xs font-semibold text-slate-300 truncate">{getSession()?.username ?? 'user'}</span>
        </div>

        <button
          onClick={onRefresh}
          disabled={loading}
          className="flex w-full items-center justify-center gap-2 rounded-lg border border-cyan/40 px-4 py-2.5 text-sm font-bold text-cyan transition hover:bg-cyan/10 disabled:opacity-50"
        >
          <RefreshCw className={`h-4 w-4 ${loading ? 'animate-spin' : ''}`} />
          {loading ? 'Refreshing' : 'Refresh'}
        </button>
        <button
          onClick={onTrain}
          disabled={training}
          className="flex w-full items-center justify-center gap-2 rounded-lg bg-acid px-4 py-2.5 text-sm font-black text-ink transition hover:brightness-110 disabled:opacity-50"
        >
          <Brain className="h-4 w-4" />
          {training ? 'Training...' : 'Train Model'}
        </button>
        <button
          onClick={() => { logout(); navigate('/login'); }}
          className="flex w-full items-center justify-center gap-2 rounded-lg border border-line px-4 py-2.5 text-sm font-semibold text-slate-400 transition hover:border-danger/40 hover:bg-danger/10 hover:text-danger"
        >
          <LogOut className="h-4 w-4" />
          Sign Out
        </button>
      </div>
    </aside>
  );
}
