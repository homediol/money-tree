import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { Brain, Eye, EyeOff, LogIn, AlertCircle } from 'lucide-react';
import { login } from '../auth.js';

export default function Login() {
  const navigate = useNavigate();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [show,     setShow]     = useState(false);
  const [error,    setError]    = useState('');
  const [loading,  setLoading]  = useState(false);

  function handleSubmit(e) {
    e.preventDefault();
    setError('');
    if (!username.trim() || !password) {
      setError('Username and password are required.');
      return;
    }
    setLoading(true);
    // Slight delay for UX feel
    setTimeout(() => {
      if (login(username, password)) {
        navigate('/', { replace: true });
      } else {
        setError('Invalid username or password.');
        setLoading(false);
      }
    }, 400);
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-ink px-4">
      {/* Background glows */}
      <div className="pointer-events-none fixed inset-0 overflow-hidden">
        <div className="absolute -top-32 left-1/2 h-96 w-96 -translate-x-1/2 rounded-full bg-cyan/10 blur-3xl" />
        <div className="absolute bottom-0 right-0 h-80 w-80 rounded-full bg-acid/8 blur-3xl" />
      </div>

      <div className="relative w-full max-w-sm">
        {/* Logo */}
        <div className="mb-8 flex flex-col items-center gap-3">
          <div className="flex h-14 w-14 items-center justify-center rounded-2xl bg-gradient-to-br from-cyan to-acid shadow-glow">
            <Brain className="h-7 w-7 text-ink" />
          </div>
          <div className="text-center">
            <h1 className="text-2xl font-black text-white tracking-tight">Winner Predict</h1>
            <p className="mt-0.5 text-xs font-semibold uppercase tracking-[0.2em] text-cyan">
              Risk Console
            </p>
          </div>
        </div>

        {/* Card */}
        <div className="rounded-2xl border border-line bg-panel/90 p-8 shadow-2xl backdrop-blur">
          <h2 className="mb-1 text-lg font-bold text-white">Sign in</h2>
          <p className="mb-6 text-sm text-slate-400">Enter your credentials to continue.</p>

          <form onSubmit={handleSubmit} className="space-y-4">
            {/* Username */}
            <div>
              <label className="mb-1.5 block text-xs font-semibold uppercase tracking-[0.1em] text-slate-400">
                Username
              </label>
              <input
                type="text"
                autoComplete="username"
                value={username}
                onChange={e => setUsername(e.target.value)}
                disabled={loading}
                placeholder="admin"
                className="w-full rounded-lg border border-line bg-ink/70 px-4 py-3 text-sm text-white placeholder-slate-600 outline-none transition focus:border-cyan/50 focus:ring-1 focus:ring-cyan/20 disabled:opacity-50"
              />
            </div>

            {/* Password */}
            <div>
              <label className="mb-1.5 block text-xs font-semibold uppercase tracking-[0.1em] text-slate-400">
                Password
              </label>
              <div className="relative">
                <input
                  type={show ? 'text' : 'password'}
                  autoComplete="current-password"
                  value={password}
                  onChange={e => setPassword(e.target.value)}
                  disabled={loading}
                  placeholder="••••••••"
                  className="w-full rounded-lg border border-line bg-ink/70 px-4 py-3 pr-10 text-sm text-white placeholder-slate-600 outline-none transition focus:border-cyan/50 focus:ring-1 focus:ring-cyan/20 disabled:opacity-50"
                />
                <button
                  type="button"
                  onClick={() => setShow(s => !s)}
                  tabIndex={-1}
                  className="absolute right-3 top-1/2 -translate-y-1/2 text-slate-500 hover:text-white transition"
                >
                  {show ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
            </div>

            {/* Error */}
            {error && (
              <div className="flex items-center gap-2 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2.5 text-sm text-rose-200">
                <AlertCircle className="h-4 w-4 shrink-0 text-danger" />
                {error}
              </div>
            )}

            {/* Submit */}
            <button
              type="submit"
              disabled={loading}
              className="flex w-full items-center justify-center gap-2 rounded-lg bg-gradient-to-r from-cyan to-acid py-3 text-sm font-black text-ink transition hover:brightness-110 disabled:opacity-50"
            >
              {loading ? (
                <span className="h-4 w-4 animate-spin rounded-full border-2 border-ink/40 border-t-ink" />
              ) : (
                <LogIn className="h-4 w-4" />
              )}
              {loading ? 'Signing in…' : 'Sign In'}
            </button>
          </form>
        </div>

        <p className="mt-6 text-center text-xs text-slate-600">
          Winner Predict · Private Access Only
        </p>
      </div>
    </div>
  );
}
