import { useState, useEffect, createContext, useContext } from 'react';
import { BrowserRouter, Routes, Route, Navigate, Link, useLocation } from 'react-router-dom';
import axios from 'axios';
import { ToastProvider } from './components/Toast';
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
  const [user, setUser] = useState(undefined);

  useEffect(() => {
    axios.get('/api/auth/me')
      .then(r => setUser(r.data.user))
      .catch(() => setUser(null));
  }, []);

  const login  = (u) => setUser(u);
  const logout = async () => { await axios.post('/api/auth/logout'); setUser(null); };

  // Service account auth — always connected; expose googleConnected=true for
  // any components that still reference it.
  return (
    <AuthContext.Provider value={{ user, googleConnected: true, setGoogleConnected: () => {}, login, logout }}>
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
function Topbar({ accounts }) {
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
          <span className="page-title">All Campaigns</span>
        )}
      </div>
      <div className="topbar-right">
        <span className="day-label">{DayLabel()}</span>
      </div>
    </div>
  );
}

// ── Layout wrapper ─────────────────────────────────────────────────────────
function AppLayout({ children, accounts }) {
  return (
    <div className="shell">
      <Sidebar accounts={accounts} unreadCount={3} />
      <div className="main-col">
        <Topbar accounts={accounts} />
        <div className="page-content">{children}</div>
      </div>
    </div>
  );
}

// ── Inner routes (need ToastProvider in scope) ─────────────────────────────
function AppRoutes() {
  const [accounts, setAccounts] = useState([]);

  const loadAccounts = async () => {
    try {
      const r = await axios.get('/api/campaigns/all');
      setAccounts(r.data.accounts || []);
    } catch { /* silent */ }
  };

  // Lightweight updater so child pages can patch a single account's data
  // (e.g. settings toggle) without triggering a full reload.
  const updateAccountSettings = (accountId, settingsPatch) => {
    setAccounts(prev => prev.map(a =>
      a.id === accountId
        ? { ...a, settings: { ...(a.settings || {}), ...settingsPatch } }
        : a
    ));
  };

  useEffect(() => { loadAccounts(); }, []);

  const wrap = (el) => (
    <AppLayout accounts={accounts}>
      {el}
    </AppLayout>
  );

  return (
    <Routes>
      <Route path="/login"    element={<Login />} />
      <Route path="/register" element={<Register />} />

      <Route path="/" element={
        <ProtectedRoute>{wrap(<Home onAccountsChange={loadAccounts} onAccountSettingChange={updateAccountSettings} accounts={accounts} />)}</ProtectedRoute>
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
