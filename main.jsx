import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createRoot } from 'react-dom/client';
import './style.css';

const API = 'http://127.0.0.1:8000';
const REFRESH_MS = 60_000;

function fmtTime(value) {
  if (!value) return '—';
  const d = new Date(value);
  if (Number.isNaN(d.getTime())) return String(value);
  return d.toLocaleString('en-NG', {
    hour: '2-digit',
    minute: '2-digit',
    day: '2-digit',
    month: 'short',
  });
}

function scoreClass(score) {
  if (score >= 80) return 'score-high';
  if (score >= 60) return 'score-mid';
  return 'score-low';
}

function directionClass(direction) {
  return String(direction || 'WAIT').toLowerCase();
}

function safeNumber(value) {
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
}

async function apiFetch(path, options = {}) {
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: {
      ...(options.body ? { 'Content-Type': 'application/json' } : {}),
      ...(options.headers || {}),
    },
  });

  let data = null;
  try {
    data = await response.json();
  } catch {
    data = null;
  }

  if (!response.ok) {
    throw new Error(data?.detail || data?.message || `Request failed (${response.status})`);
  }

  return data;
}

function App() {
  const [mode, setMode] = useState('signin');
  const [logged, setLogged] = useState(localStorage.getItem('collins_logged') === 'true');
  const [email, setEmail] = useState(localStorage.getItem('collins_email') || '');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');

  const [signals, setSignals] = useState([]);
  const [news, setNews] = useState([]);
  const [trades, setTrades] = useState([]);
  const [activeTrades, setActiveTrades] = useState([]);

  const [tab, setTab] = useState('signals');
  const [selectedSymbol, setSelectedSymbol] = useState('ALL');
  const [newsFilter, setNewsFilter] = useState('ALL');
  const [newsWeek, setNewsWeek] = useState('this');
  const [search, setSearch] = useState('');

  const [loading, setLoading] = useState(false);
  const [theme, setTheme] = useState(localStorage.getItem('collins_theme') || 'dark');
  const [calendarView, setCalendarView] = useState('today');
  const [calendarOffset, setCalendarOffset] = useState(0);
  const [calendarDate, setCalendarDate] = useState('');
  const [calendarMeta, setCalendarMeta] = useState({ title: 'Today', start: '', end: '', source: '' });
  const [signalsLoading, setSignalsLoading] = useState(false);
  const previousSignalsRef = useRef([]);
  const watchedSymbolsRef = useRef(new Set(JSON.parse(localStorage.getItem('collins_watched_symbols') || '[]')));

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem('collins_theme', theme);
  }, [theme]);
  const [error, setError] = useState('');
  const [message, setMessage] = useState('');
  const [apiOnline, setApiOnline] = useState(false);
  const [lastUpdated, setLastUpdated] = useState(null);
  const [notificationsEnabled, setNotificationsEnabled] = useState(
    localStorage.getItem('collins_notifications') !== 'false'
  );

  const showMessage = useCallback((text) => {
    setMessage(text);
    setError('');
  }, []);

  const showError = useCallback((text) => {
    setError(text);
    setMessage('');
  }, []);

  useEffect(() => {
    if (!message) return undefined;
    const timer = setTimeout(() => setMessage(''), 4000);
    return () => clearTimeout(timer);
  }, [message]);

  useEffect(() => {
    localStorage.setItem('collins_notifications', notificationsEnabled ? 'true' : 'false');
  }, [notificationsEnabled]);

  // Email verification: backend redirects the user to /?verify=TOKEN.
  useEffect(() => {
    const token = new URLSearchParams(window.location.search).get('verify');
    if (!token) return;

    let cancelled = false;
    setLoading(true);
    apiFetch(`/api/verify?token=${encodeURIComponent(token)}`)
      .then((data) => {
        if (cancelled) return;
        setMode('signin');
        setLogged(false);
        setEmail(data?.email || '');
        setPassword('');
        setConfirmPassword('');
        showMessage('Email verified successful. You can now sign in.');
        window.history.replaceState({}, document.title, window.location.pathname);
      })
      .catch((err) => {
        if (!cancelled) showError(err.message || 'Verification failed.');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
    };
  }, [showError, showMessage]);

  const ensureNotificationPermission = useCallback(async () => {
    if (!('Notification' in window)) {
      showError('This browser does not support notifications.');
      return false;
    }
    if (Notification.permission === 'granted') return true;
    if (Notification.permission === 'denied') {
      showError('Browser notifications are blocked. Allow notifications for localhost in browser site settings.');
      return false;
    }
    try {
      return (await Notification.requestPermission()) === 'granted';
    } catch {
      return false;
    }
  }, [showError]);

  const pushSignalNotification = useCallback(async (signal, reason = 'ready', force = false) => {
    if ((!notificationsEnabled && !force) || !signal) return;
    const ok = await ensureNotificationPermission();
    if (!ok) return;
    const direction = signal.direction === 'BUY' ? 'BUY' : signal.direction === 'SELL' ? 'SELL' : 'READY';
    new Notification(`CollinsAI • ${signal.symbol} ${direction}`, {
      body: `${signal.symbol} is now ${direction} at ${Number(signal.score || 0)}/100 (${signal.tier || 'STRONG'}). ${reason}.`,
      tag: `collins-${signal.symbol}`
    });
  }, [notificationsEnabled, ensureNotificationPermission]);

  const setGlobalNotifications = useCallback(async () => {
    if (notificationsEnabled) {
      setNotificationsEnabled(false);
      return;
    }
    const ok = await ensureNotificationPermission();
    if (ok) setNotificationsEnabled(true);
  }, [notificationsEnabled, ensureNotificationPermission]);

  const toggleWatchedSymbol = useCallback(async (symbol) => {
    const ok = await ensureNotificationPermission();
    if (!ok) return;
    const next = new Set(watchedSymbolsRef.current);
    if (next.has(symbol)) next.delete(symbol); else next.add(symbol);
    watchedSymbolsRef.current = next;
    localStorage.setItem('collins_watched_symbols', JSON.stringify([...next]));
    showMessage(next.has(symbol) ? `Watching ${symbol}. I will alert you when it becomes an actionable 70+ BUY/SELL signal.` : `Stopped watching ${symbol}.`);
  }, [ensureNotificationPermission, showMessage]);

  const loadDashboard = useCallback(async (silent = false) => {
    if (!silent) setSignalsLoading(true);
    try {
      const [signalData, newsData, tradeData, activeData] = await Promise.all([
        apiFetch('/api/signals'),
        apiFetch(`/api/news?week=${newsWeek}`),
        apiFetch('/api/trades'),
        apiFetch('/api/trades/active'),
      ]);

      const nextSignals = signalData?.signals || [];
      const previousSignals = previousSignalsRef.current;
      if (previousSignals.length) {
        for (const next of nextSignals) {
          const prev = previousSignals.find((x) => x.symbol === next.symbol);
          const nextScore = Number(next.score || 0);
          const prevScore = Number(prev?.score || 0);
          const nextActionable = next.direction === 'BUY' || next.direction === 'SELL';
          const prevActionable = prev?.direction === 'BUY' || prev?.direction === 'SELL';
          const becameReady = nextActionable && nextScore >= 70 && (!prevActionable || prevScore < 70 || prev?.direction !== next.direction);
          const watched = watchedSymbolsRef.current.has(next.symbol);
          if (becameReady && (notificationsEnabled || watched)) {
            pushSignalNotification(next, watched ? 'Your watched signal is ready' : 'Global alerts are enabled', watched && !notificationsEnabled);
          }
        }
      }
      previousSignalsRef.current = nextSignals;
      setSignals(nextSignals);
      setNews(newsData?.news || []);
      setTrades(tradeData?.trades || []);
      setActiveTrades(activeData?.trades || []);
      setLastUpdated(signalData?.generated_at || new Date().toISOString());
      setApiOnline(true);
      if (!silent) setError('');
    } catch (err) {
      setApiOnline(false);
      if (!silent) showError(err.message || 'Could not load live market data.');
    } finally {
      if (!silent) setSignalsLoading(false);
    }
  }, [showError, newsWeek]);

  const loadCalendar = useCallback(async (view = calendarView, offset = calendarOffset, date = calendarDate) => {
    try {
      const params = new URLSearchParams();
      if (view === 'date' && date) params.set('date', date);
      else { params.set('view', view); params.set('offset', String(offset)); }
      const data = await apiFetch(`/api/news?${params.toString()}`);
      setNews(data?.news || data?.items || []);
      setCalendarMeta({ title: data?.title || (view === 'today' ? 'Today' : view === 'next' ? 'Next Week' : view === 'previous' ? 'Previous Week' : 'This Week'), start: data?.range_start || '', end: data?.range_end || '', source: data?.source || '' });
      setApiOnline(true);
    } catch (err) {
      showError(err.message || 'Could not load economic calendar.');
    }
  }, [calendarView, calendarOffset, calendarDate, showError]);

  useEffect(() => {
    if (!logged) return undefined;
    loadDashboard();
    const timer = setInterval(() => loadDashboard(true), REFRESH_MS);
    return () => clearInterval(timer);
  }, [logged, loadDashboard]);

  useEffect(() => {
    if (logged && tab === 'news') loadCalendar();
  }, [logged, tab, calendarView, calendarOffset, calendarDate, loadCalendar]);

  const handleRegister = async () => {
    setMessage('');
    setError('');

    const cleanEmail = email.trim().toLowerCase();
    if (!cleanEmail || !password || !confirmPassword) {
      showError('Please fill in all fields.');
      return;
    }
    if (password.length < 8) {
      showError('Password must be at least 8 characters.');
      return;
    }
    if (password !== confirmPassword) {
      showError('Passwords do not match.');
      return;
    }

    setLoading(true);
    try {
      const data = await apiFetch('/api/register', {
        method: 'POST',
        body: JSON.stringify({ email: cleanEmail, password }),
      });
      setPassword('');
      setConfirmPassword('');
      showMessage(data?.message || 'Account created. Check your email for verification.');
      setMode('signin');
    } catch (err) {
      showError(err.message || 'Registration failed.');
    } finally {
      setLoading(false);
    }
  };

  const handleLogin = async () => {
    setMessage('');
    setError('');
    const cleanEmail = email.trim().toLowerCase();
    if (!cleanEmail || !password) {
      showError('Please enter your email and password.');
      return;
    }

    setLoading(true);
    try {
      const data = await apiFetch('/api/login', {
        method: 'POST',
        body: JSON.stringify({ email: cleanEmail, password }),
      });
      localStorage.setItem('collins_logged', 'true');
      localStorage.setItem('collins_email', cleanEmail);
      localStorage.setItem('collins_token', data?.token || '');
      setLogged(true);
      setPassword('');
    } catch (err) {
      showError(err.message || 'Login failed.');
    } finally {
      setLoading(false);
    }
  };

  const logout = () => {
    localStorage.removeItem('collins_logged');
    localStorage.removeItem('collins_email');
    localStorage.removeItem('collins_token');
    setLogged(false);
    setSignals([]);
    setNews([]);
    setTrades([]);
    setActiveTrades([]);
    setPassword('');
    setConfirmPassword('');
    setMessage('');
    setError('');
  };

  const symbols = useMemo(
    () => ['ALL', ...Array.from(new Set(signals.map((s) => s.symbol).filter(Boolean)))],
    [signals]
  );

  const filteredSignals = useMemo(() => {
    return signals.filter((s) => {
      const matchesSymbol = selectedSymbol === 'ALL' || s.symbol === selectedSymbol;
      const q = search.trim().toLowerCase();
      const matchesSearch = !q || [s.symbol, s.direction, s.tier, s.analysis]
        .join(' ')
        .toLowerCase()
        .includes(q);
      return matchesSymbol && matchesSearch;
    });
  }, [signals, selectedSymbol, search]);

  const filteredNews = useMemo(() => {
    return news.filter((item) => newsFilter === 'ALL' || String(item.impact).toUpperCase() === newsFilter);
  }, [news, newsFilter]);

  const calendarGroups = useMemo(() => {
    const groups = {};
    filteredNews.forEach((item) => {
      const raw = item.wat_time || item.utc_time || item.date;
      const d = raw ? new Date(raw) : null;
      const key = d && !Number.isNaN(d.getTime()) ? d.toLocaleDateString('en-NG', { weekday: 'long', day: '2-digit', month: 'short', year: 'numeric' }) : 'Other';
      (groups[key] ||= []).push(item);
    });
    return Object.entries(groups);
  }, [filteredNews]);

  const newsWeekLabel = newsWeek === 'next' ? 'Next Week' : 'This Week';

  const stats = useMemo(() => {
    const actionable = signals.filter((s) => s.direction === 'BUY' || s.direction === 'SELL');
    const strong = actionable.filter((s) => Number(s.score) >= 80);
    const avg = signals.length
      ? Math.round(signals.reduce((sum, s) => sum + (Number(s.score) || 0), 0) / signals.length)
      : 0;
    return {
      total: signals.length,
      actionable: actionable.length,
      strong: strong.length,
      avg,
      active: activeTrades.length,
    };
  }, [signals, activeTrades]);

  const sendBrowserNotification = (signal) => toggleWatchedSymbol(signal.symbol);

  const renderSignalCard = (s) => {
    const score = Number(s.score) || 0;
    const rsi = safeNumber(s.rsi);
    const newsRisk = Number(s.news_risk) || 0;
    return (
      <article className="signal-card" key={s.symbol}>
        <div className="card-topline">
          <div>
            <div className="symbol-title">{s.symbol}</div>
            <div className="mini-muted">HTF: {s.htf_trend || 'UNKNOWN'}</div>
          </div>
          <span className={`direction ${directionClass(s.direction)}`}>{s.direction}</span>
        </div>

        <div className="score-row">
          <div className={`score-ring ${scoreClass(score)}`} style={{ '--score': `${score * 3.6}deg` }}>
            <div><strong>{score}</strong><small>/100</small></div>
          </div>
          <div className="score-copy">
            <span className="tier">{s.tier || 'WAIT'}</span>
            <span className="mini-muted">Bull {s.bullish_score ?? 0} · Bear {s.bearish_score ?? 0}</span>
            <div className="progress"><span style={{ width: `${Math.max(0, Math.min(100, score))}%` }} /></div>
          </div>
        </div>

        <div className="price-grid">
          <div><span>Entry</span><b>{s.entry ?? '—'}</b></div>
          <div><span>Stop Loss</span><b>{s.sl ?? '—'}</b></div>
          <div><span>TP1</span><b>{s.tp1 ?? '—'}</b></div>
          <div><span>TP2</span><b>{s.tp2 ?? s.tp ?? '—'}</b></div>
          <div><span>TP3</span><b>{s.tp3 ?? '—'}</b></div>
          <div><span>R:R</span><b>{s.rr ?? '—'}</b></div>
        </div>

        <div className="signal-metrics">
          <span>RSI <b>{rsi ?? '—'}</b></span>
          <span>News risk <b>{newsRisk}/100</b></span>
          <span className={`status-dot ${s.status === 'DATA ERROR' ? 'bad' : 'good'}`} />
          <span>{s.status || 'WAITING'}</span>
        </div>

        <p className="analysis">{s.analysis || 'No analysis text returned.'}</p>

        <div className="strategy-list">
          {(s.strategies || []).map((strategy) => <span key={strategy}>{strategy}</span>)}
        </div>

        {s.news_warning && <div className="news-warning">{s.news_warning}</div>}

        <button className={`outline-btn full ${watchedSymbolsRef.current.has(s.symbol) ? 'watching-btn' : ''}`} onClick={() => sendBrowserNotification(s)}>
          {watchedSymbolsRef.current.has(s.symbol) ? `🔔 Watching ${s.symbol}` : `Notify me when ${s.symbol} is ready`}
        </button>
      </article>
    );
  };

  if (!logged) {
    return (
      <div className="auth-shell">
        <div className="auth-glow glow-one" />
        <div className="auth-glow glow-two" />
        <section className="auth-card">
          <div className="brand-mark">C<span>AI</span></div>
          <div className="brand-caption">COLLINS AI SIGNALS</div>
          <h1>{mode === 'signin' ? 'Welcome back' : 'Build your trading edge'}</h1>
          <p className="auth-subtitle">Live market intelligence powered by multi-strategy technical analysis.</p>

          <div className="auth-tabs">
            <button className={mode === 'signin' ? 'active' : ''} onClick={() => { setMode('signin'); setError(''); setMessage(''); }}>Sign In</button>
            <button className={mode === 'signup' ? 'active' : ''} onClick={() => { setMode('signup'); setError(''); setMessage(''); }}>Create Account</button>
          </div>

          <label>Email address</label>
          <input value={email} onChange={(e) => setEmail(e.target.value)} type="email" placeholder="you@example.com" autoComplete="email" />

          <label>Password</label>
          <input value={password} onChange={(e) => setPassword(e.target.value)} type="password" placeholder="Minimum 8 characters" autoComplete={mode === 'signin' ? 'current-password' : 'new-password'} />

          {mode === 'signup' && (
            <>
              <label>Confirm password</label>
              <input value={confirmPassword} onChange={(e) => setConfirmPassword(e.target.value)} type="password" placeholder="Repeat your password" autoComplete="new-password" />
            </>
          )}

          <button className="primary-btn" disabled={loading} onClick={mode === 'signin' ? handleLogin : handleRegister}>
            {loading ? 'Please wait…' : mode === 'signin' ? 'Sign In to CollinsAI' : 'Create CollinsAI Account'}
          </button>

          <div className="security-note"><span>●</span> Demo authentication · No broker credentials required</div>

          {message && <div className="toast success"><b>✓</b>{message}</div>}
          {error && <div className="toast error"><b>!</b>{error}</div>}
        </section>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar-brand"><div className="brand-mark small">C<span>AI</span></div><div><b>COLLINS AI</b><small>Signal Intelligence</small></div></div>

        <nav>
          <button className={tab === 'signals' ? 'nav-active' : ''} onClick={() => setTab('signals')}><span>◈</span> Signal Center</button>
          <button className={tab === 'news' ? 'nav-active' : ''} onClick={() => setTab('news')}><span>◉</span> Economic News</button>
          <button className={tab === 'trades' ? 'nav-active' : ''} onClick={() => setTab('trades')}><span>↗</span> Trade Monitor</button>
        </nav>

        <div className="sidebar-bottom">
          <div className="connection-box"><span className={`status-dot ${apiOnline ? 'good' : 'bad'}`} /><div><b>{apiOnline ? 'API Connected' : 'API Offline'}</b><small>FastAPI · Twelve Data</small></div></div>
          <div className="user-box"><span>{(email || 'C').charAt(0).toUpperCase()}</span><div><b>{email || 'User'}</b><small>CollinsAI account</small></div></div>
          <button className="logout-btn" onClick={logout}>Log out</button>
        </div>
      </aside>

      <main className="main-content">
        <header className="topbar">
          <div><div className="eyebrow">LIVE MARKET INTELLIGENCE</div><h1>{tab === 'signals' ? 'Signal Center' : tab === 'news' ? 'Economic Calendar' : 'Trade Monitor'}</h1></div>
          <div className="top-actions">
            <button className="theme-toggle top-theme" onClick={() => setTheme((v) => v === 'dark' ? 'light' : 'dark')} title="Toggle light/dark mode">{theme === 'dark' ? '☀' : '☾'}</button>
            <button className="notification-toggle" onClick={setGlobalNotifications} title="Global signal notifications">
              {notificationsEnabled ? '🔔' : '🔕'}
            </button>
            <button className="refresh-btn" onClick={() => loadDashboard()} disabled={signalsLoading}>↻ {signalsLoading ? 'Updating…' : 'Refresh'}</button>
          </div>
        </header>

        {message && <div className="toast success fixed-toast"><b>✓</b>{message}</div>}
        {error && <div className="toast error fixed-toast"><b>!</b>{error}</div>}

        <section className="hero-strip">
          <div><span className="live-pill"><i /> LIVE</span><b>Multi-strategy market analysis</b><span>EMA · RSI · MACD · BB · SMC · BOS · FVG · CRT · HTF</span></div>
          <small>Last update: {lastUpdated ? fmtTime(lastUpdated) : '—'}</small>
        </section>

        <section className="stats-grid">
          <div className="stat-card"><span>Total markets</span><strong>{stats.total}</strong><small>Tracked by engine</small></div>
          <div className="stat-card"><span>Actionable</span><strong>{stats.actionable}</strong><small>BUY / SELL signals</small></div>
          <div className="stat-card"><span>Strong setups</span><strong>{stats.strong}</strong><small>Score ≥ 80</small></div>
          <div className="stat-card"><span>Active trades</span><strong>{stats.active}</strong><small>Engine monitored</small></div>
          <div className="stat-card"><span>Average score</span><strong>{stats.avg}</strong><small>Across returned markets</small></div>
        </section>

        {tab === 'signals' && (
          <>
            <section className="toolbar">
              <div className="symbol-filters">
                {symbols.map((symbol) => <button key={symbol} className={selectedSymbol === symbol ? 'selected' : ''} onClick={() => setSelectedSymbol(symbol)}>{symbol === 'ALL' ? 'All Markets' : symbol}</button>)}
              </div>
              <input className="search" value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search signals…" />
            </section>

            <div className="section-heading"><div><h2>Live Signals</h2><p>Signals are generated by your FastAPI backend from live market candles. They are informational, not guaranteed trade outcomes.</p></div><span>{filteredSignals.length} shown</span></div>

            {signalsLoading && signals.length === 0 ? (
              <div className="loading-panel"><div className="spinner" /> Loading live market signals…</div>
            ) : filteredSignals.length ? (
              <section className="signal-grid">{filteredSignals.map(renderSignalCard)}</section>
            ) : (
              <div className="empty-panel">No signals returned for this filter.</div>
            )}
          </>
        )}

        {tab === 'news' && (
          <section className="calendar-page">
            <div className="calendar-toolbar">
              <div className="calendar-nav">
                <button onClick={() => { setCalendarView('previous'); setCalendarOffset((v) => v - 1); setCalendarDate(''); }}>‹ Previous Week</button>
                <button className={calendarView === 'today' ? 'active' : ''} onClick={() => { setCalendarView('today'); setCalendarOffset(0); setCalendarDate(''); }}>Today</button>
                <button className={calendarView === 'week' && calendarOffset === 0 ? 'active' : ''} onClick={() => { setCalendarView('week'); setCalendarOffset(0); setCalendarDate(''); }}>This Week</button>
                <button onClick={() => { setCalendarView('next'); setCalendarOffset(1); setCalendarDate(''); }}>Next Week ›</button>
              </div>
              <div className="calendar-date-picker">
                <label>Choose date</label>
                <input type="date" value={calendarDate} onChange={(e) => { setCalendarDate(e.target.value); if (e.target.value) setCalendarView('date'); }} />
              </div>
              <button className="theme-toggle" onClick={() => setTheme((v) => v === 'dark' ? 'light' : 'dark')}>{theme === 'dark' ? '☀ Light' : '☾ Dark'}</button>
            </div>

            <div className="calendar-titlebar">
              <div><span className="calendar-kicker">ECONOMIC CALENDAR · WAT</span><h2>{calendarMeta.title || 'Today'}</h2><p>{calendarMeta.start && calendarMeta.end ? `${calendarMeta.start} — ${calendarMeta.end}` : 'Recent and upcoming market-moving events'}</p></div>
              <div className="calendar-count"><b>{filteredNews.length}</b><span>events</span></div>
            </div>

            <div className="calendar-controls">
              <div className="calendar-impact">
                {['ALL', 'HIGH', 'MEDIUM', 'LOW'].map((impact) => <button key={impact} className={newsFilter === impact ? 'selected' : ''} onClick={() => setNewsFilter(impact)}>{impact === 'ALL' ? 'All Impact' : impact}</button>)}
              </div>
              <span className="timezone-note">Nigeria time · WAT (UTC+1)</span>
            </div>

            <div className="calendar-table-wrap">
              <div className="calendar-header"><span>TIME</span><span>CURRENCY</span><span>IMPACT</span><span>EVENT</span><span>ACTUAL</span><span>FORECAST</span><span>PREVIOUS</span></div>
              <div className="calendar-body">
                {calendarGroups.length ? calendarGroups.map(([day, items]) => (
                  <div className="calendar-day-group" key={day}>
                    <div className="calendar-day-heading"><b>{day}</b><span>{items.length} event{items.length === 1 ? '' : 's'}</span></div>
                    {items.map((item, index) => (
                      <article className={`calendar-event ${String(item.impact).toLowerCase()}`} key={`${item.event}-${item.utc_time || item.date}-${index}`}>
                        <div className="calendar-time"><b>{item.tentative ? 'Tentative' : (item.wat_time ? new Date(item.wat_time).toLocaleTimeString('en-NG', {hour:'2-digit', minute:'2-digit'}) : '—')}</b><small>WAT</small></div>
                        <div className="calendar-currency"><b>{item.currency || item.country || '—'}</b></div>
                        <div><span className={`impact-dot ${String(item.impact).toLowerCase()}`}></span><b>{item.impact || 'LOW'}</b></div>
                        <div className="calendar-event-name"><b>{item.event || 'Economic Event'}</b><small>{item.country || item.currency || 'Global'}</small></div>
                        <div className="calendar-number actual">{item.actual ?? '—'}</div>
                        <div className="calendar-number forecast">{item.forecast ?? '—'}</div>
                        <div className="calendar-number previous">{item.previous ?? '—'}</div>
                      </article>
                    ))}
                  </div>
                )) : <div className="empty-panel calendar-empty">No economic events are available for this period.</div>}
              </div>
            </div>
            <div className="calendar-footer-note"><b>Actual</b> = released result · <b>Forecast</b> = market expectation · <b>Previous</b> = prior result. Values are shown in WAT and can change when releases are revised.</div>
          </section>
        )}

        {tab === 'trades' && (
          <section>
            <div className="section-heading"><div><h2>Trade Monitor</h2><p>Your backend creates and monitors qualifying signals. No real broker orders are placed by this dashboard.</p></div><span>{trades.length} total</span></div>
            <div className="trade-summary"><div><span>Active</span><b>{activeTrades.length}</b></div><div><span>Closed</span><b>{Math.max(0, trades.length - activeTrades.length)}</b></div><div><span>Profit results</span><b>{trades.filter((t) => t.result === 'PROFIT').length}</b></div><div><span>Loss results</span><b>{trades.filter((t) => t.result === 'LOSS').length}</b></div></div>
            <div className="trade-table-wrap">
              <table className="trade-table"><thead><tr><th>Symbol</th><th>Direction</th><th>Entry</th><th>SL</th><th>TP1</th><th>TP2</th><th>TP3</th><th>Status</th><th>Score</th><th>Created</th></tr></thead>
                <tbody>{trades.map((t) => <tr key={t.id}><td><b>{t.symbol}</b></td><td><span className={`direction ${directionClass(t.direction)}`}>{t.direction}</span></td><td>{t.entry}</td><td>{t.sl}</td><td>{t.tp1}</td><td>{t.tp2}</td><td>{t.tp3}</td><td><span className="status-chip">{t.status}</span></td><td>{t.score}</td><td>{fmtTime(t.created_at)}</td></tr>)}</tbody>
              </table>
              {!trades.length && <div className="empty-panel">No trades have been created yet. Strong BUY/SELL signals will be monitored by the backend.</div>}
            </div>
          </section>
        )}

        <footer>COLLINS AI SIGNALS · Live data dashboard · For educational and informational use only.</footer>
      </main>
    </div>
  );
}

createRoot(document.getElementById('root')).render(<App />);
