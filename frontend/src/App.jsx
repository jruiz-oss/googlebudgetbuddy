import { useState, useEffect, createContext, useContext } from 'react';
import { BrowserRouter, Routes, Route, Navigate } from 'react-router-dom';
import axios from 'axios';
import { ToastProvider } from './components/Toast';
import Sidebar from './components/Sidebar';
import Home from './pages/Home';
import AccountDashboard from './pages/AccountDashboard';
import CampaignDetail from './pages/CampaignDetail';
import Settings from './pages/Settings';
import History from './pages/History';
import Leads from './pages/Leads';
import Login from './pages/Login';
import Register from './pages/Register';

// ── Axios global config ────────────────────────────────────────────────────
axios.defaults.baseURL = import.meta.env.VITE_API_URL || 'http://localhost:5000';
axios.defaults.withCredentials = true;
axios.defaults.timeout = 60_000;

// Warm up the backend on load (Railway dyno wakes up while React mounts)
axios.get('/api/health').catch(() => {});

// ── Auth context ───────────────────────────────────────────────────────────
const AuthContext = createContext(null);
export const useAuth = () => useContext(AuthContext);

function AuthProvider({ children }) {
  const [user, setUser] = useState(undefined); // undefined = loading
  const [googleConnected, setGoogleConnected] = useState(false);

  useEffect(() => {
    axios.get('/api/auth/me')
      .then(r => {
        setUser(r.data.user);
        setGoogleConnected(r.data.user.has_google_token);
      })
      .catch(() => setUser(null));
  }, []);

  // Check for OAuth success/error from Google redirect
  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    if (params.get('oauth_success')) {
      setGoogleConnected(true);
      window.history.replaceState({}, '', '/');
    }
    if (params.get('oauth_error')) {
      console.error('OAuth error:', params.get('oauth_error'));
      window.history.replaceState({}, '', '/');
    }
  }, []);

  const login = (userData) => {
    setUser(userData);
    setGoogleConnected(userData.has_google_token);
  };

  const logout = async () => {
    await axios.post('/api/auth/logout');
    setUser(null);
    setGoogleConnected(false);
  };

  return (
    <AuthContext.Provider value={{ user, googleConnected, setGoogleConnected, login, logout }}>
      {children}
    </AuthContext.Provider>
  );
}

// ── Protected route wrapper ────────────────────────────────────────────────
function ProtectedRoute({ children }) {
  const { user } = useAuth();
  if (user === undefined) return (
    <div className="bb-app" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <p className="bb-muted">Loading…</p>
    </div>
  );
  if (!user) return <Navigate to="/login" replace />;
  return children;
}

// ── Layout with sidebar ────────────────────────────────────────────────────
function AppLayout({ children }) {
  return (
    <div className="bb-app">
      <Sidebar />
      <main className="bb-main">{children}</main>
    </div>
  );
}

// ── Root app ───────────────────────────────────────────────────────────────
export default function App() {
  return (
    <BrowserRouter>
      <AuthProvider>
        <ToastProvider>
          <Routes>
            <Route path="/login" element={<Login />} />
            <Route path="/register" element={<Register />} />
            <Route path="/" element={
              <ProtectedRoute>
                <AppLayout><Home /></AppLayout>
              </ProtectedRoute>
            } />
            <Route path="/accounts/:id" element={
              <ProtectedRoute>
                <AppLayout><AccountDashboard /></AppLayout>
              </ProtectedRoute>
            } />
            <Route path="/campaigns/:id" element={
              <ProtectedRoute>
                <AppLayout><CampaignDetail /></AppLayout>
              </ProtectedRoute>
            } />
            <Route path="/accounts/:id/settings" element={
              <ProtectedRoute>
                <AppLayout><Settings /></AppLayout>
              </ProtectedRoute>
            } />
            <Route path="/accounts/:id/history" element={
              <ProtectedRoute>
                <AppLayout><History /></AppLayout>
              </ProtectedRoute>
            } />
            <Route path="/accounts/:id/leads" element={
              <ProtectedRoute>
                <AppLayout><Leads /></AppLayout>
              </ProtectedRoute>
            } />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Routes>
        </ToastProvider>
      </AuthProvider>
    </BrowserRouter>
  );
}
