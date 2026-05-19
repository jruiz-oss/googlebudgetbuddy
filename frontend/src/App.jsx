import { useState, useEffect, createContext, useContext } from 'react';
import { BrowserRouter, Routes, Route, Navigate, Link, useLocation } from 'react-router-dom';
import axios from 'axios';
import { ToastProvider, useToast } from './components/Toast';
import Sidebar from './components/Sidebar';
import Home from './pages/Home';
import AccountDashboard from './pages/AccountDashboard';
import CampaignDetail from './pages/CampaignDetail';
import AccountSettings from './pages/Settings';
import GlobalSettings from './pages/GlobalSettings';
import Notifications from './pages/Notifications';
import History from './pages/History';
import Leads from './pages/Leads';
import Login from './pages/Login';
import Register from './pages/Register';

// ── Axios global config ────────────────────────────────────────────────────
axios.defaults.baseURL = import.meta.env.VITE_API_URL || 'http://localhost:5000';
axios.defaults.withCredentials = true;
axios.defaults.timeout = 60_000;
axios.get('/api/health').catch(() => {});

// ── Auth context ───────────────────────────────────────────────────────────
const AuthContext = createContext(null);
export const useAuth = () => useContext(AuthContext);

function AuthProvider({ children }) {
  const [user, setUser]                       = useState(undefined);
  const [googleConnected, setGoogleConnected] = useState(false);

  useEffect(() => {
    axios.get('/api/auth/me')
      .then(r => { setUser(r.data.user); setGoogleConnected(r.data.user.has_google_token); })
      .catch(() => setUser(null));
  }, []);

  useEffect(() => {
    const p = new URLSearchParams(window.location.search);
    if (p.get('oauth_success')) { setGoogleConnected(true); window.history.replaceState({}, '', '/'); }
    if (p.get('oauth_error'))   { window.history.replaceState({}, '', '/'); }
  }, []);

  const login  = (u) => { setUser(u); setGoogleConnected(u.has_google_token); };
  const logout = async () => { await axios.post('/api/auth/logout'); setUser(null); setGoogleConnected(false); };

  return (
    <AuthContext.Provider value={{ user, googleConnected, setGoogleConnected, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

function ProtectedRoute({ children }) {
  const { user } = useAuth();
  if (user === undefined) return (
    <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100vh', color: 'var(--muted)' }}>Loading…</div>
  );
  if (!user) return <Navigate to="/login" replace />;
  return children;
}

// ── Day label helper ───────────────────────────────────────────────────────
function DayLabel() {
  const today       = new Date();
  const day         = today.getDate();
  const daysInMonth = new Date(today.getFullYear(), today.getMonth() + 1, 0).getDate();
  return `Day ${day} / ${daysInMonth} · ${daysInMonth - day} left`;
}

// ── Topbar ─────────────────────────────────────────────────────────────────
function Topbar({ onSync, syncing, accounts }) {
  const location    = useLocation();
  const accountMatch = location.pathname.match(/^\/accounts\/(\d+)/);
  const accountId   = accountMatch?.[1];
  const currentAcct = accountId ? accounts.find(a => String(a.id) === accountId) : null;

  const isNoti     = location.pathname === '/notifications';
  const isSettings = location.pathname === '/settings';

  return (
    <div className="topbar">
      <div>
        {currentAcct ? (
          <div className="breadcrumb">
            <Link to="/">Dashboard</Link>
            <span className="sep">/</span>
            <span className="current">{currentAcct.account_name}</span>
          </div>
        ) : isNoti ? (
          <span className="page-title">Notifications</span>
        ) : isSettings ? (
          <span className="page-title">Settings</span>
        ) : (
          <span className="page-title">Dashboard</span>
        )}
      </div>
      <div className="topbar-right">
        <span className="day-label">{DayLabel()}</span>
        <button className="btn" onClick={onSync} disabled={syncing} style={{ gap: '6px' }}>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21.5 2v6h-6M21.34 15.57a10 10 0 1 1-.57-8.38"/>
          </svg>
          {syncing ? 'Syncing…' : 'Sync'}
        </button>
      </div>
    </div>
  );
}

// ── Layout wrapper ─────────────────────────────────────────────────────────
function AppLayout({ children, accounts, onSync, syncing }) {
  return (
    <div className="shell">
      <Sidebar accounts={accounts} unreadCount={3} />
      <div className="main-col">
        <Topbar onSync={onSync} syncing={syncing} accounts={accounts} />
        <div className="page-content">{children}</div>
      </div>
    </div>
  );
}

// ── Inner routes (need ToastProvider in scope) ─────────────────────────────
function AppRoutes() {
  const [accounts, setAccounts] = useState([]);
  const [syncing, setSyncing]   = useState(false);
  const toast = useToast();

  const loadAccounts = async () => {
    try {
      const r = await axios.get('/api/campaigns/all');
      setAccounts(r.data.accounts || []);
    } catch { /* silent */ }
  };

  useEffect(() => { loadAccounts(); }, []);

  const handleSync = async () => {
    setSyncing(true);
    try {
      const r = await axios.post('/api/accounts/sync-from-mcc', {});
      if (r.status === 202) {
        toast.info('Sync started — refreshing in 75 seconds…');
        setTimeout(() => { loadAccounts(); setSyncing(false); }, 75000);
      } else {
        loadAccounts(); setSyncing(false);
      }
    } catch (e) {
      const status = e.response?.status;
      if (status === 409) toast.info(e.response?.data?.message || 'Sync already running');
      else toast.error(e.response?.data?.error || 'Sync failed');
      setSyncing(false);
    }
  };

  const wrap = (el) => (
    <AppLayout accounts={accounts} onSync={handleSync} syncing={syncing}>
      {el}
    </AppLayout>
  );

  return (
    <Routes>
      <Route path="/login"    element={<Login />} />
      <Route path="/register" element={<Register />} />

      <Route path="/" element={
        <ProtectedRoute>{wrap(<Home onAccountsChange={loadAccounts} accounts={accounts} />)}</ProtectedRoute>
      } />

      <Route path="/notifications" element={
        <ProtectedRoute>{wrap(<Notifications accounts={accounts} />)}</ProtectedRoute>
      } />

      <Route path="/settings" element={
        <ProtectedRoute>{wrap(<GlobalSettings />)}</ProtectedRoute>
      } />

      <Route path="/accounts/:id" element={
        <ProtectedRoute>{wrap(<AccountDashboard onPacingComplete={loadAccounts} />)}</ProtectedRoute>
      } />

      <Route path="/campaigns/:id" element={
        <ProtectedRoute>{wrap(<CampaignDetail />)}</ProtectedRoute>
      } />

      <Route path="/accounts/:id/settings" element={
        <ProtectedRoute>{wrap(<AccountSettings />)}</ProtectedRoute>
      } />

      <Route path="/accounts/:id/history" element={
        <ProtectedRoute>{wrap(<History />)}</ProtectedRoute>
      } />

      <Route path="/accounts/:id/leads" element={
        <ProtectedRoute>{wrap(<Leads />)}</ProtectedRoute>
      } />

      <Route path="*" element={<Navigate to="/" replace />} />
    </Routes>
  );
}

export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <ToastProvider>
          <AppRoutes />
        </ToastProvider>
      </AuthProvider>
    </BrowserRouter>
  );
}
