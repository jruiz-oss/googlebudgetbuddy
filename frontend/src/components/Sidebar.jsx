import { NavLink, useNavigate } from 'react-router-dom';
import { Activity, LayoutDashboard, LogOut, Settings, History, Users, Download } from 'lucide-react';
import axios from 'axios';
import { useAuth } from '../App';
import { useToast } from './Toast';

export default function Sidebar() {
  const { user, googleConnected, logout } = useAuth();
  const navigate = useNavigate();
  const { addToast } = useToast();

  const handleLogout = async () => {
    try {
      await logout();
      navigate('/login');
    } catch {
      addToast('Logout failed', 'error');
    }
  };

  return (
    <aside className="bb-sidebar">
      {/* Brand */}
      <div className="bb-sidebar-brand">
        <span className="bb-brand-pill">
          <Activity size={16} />
        </span>
        <span className="bb-brand-name">Google BudgetBuddy</span>
      </div>

      {/* Nav */}
      <nav className="bb-nav">
        <NavLink to="/" end className={({ isActive }) => `bb-nav-item${isActive ? ' is-active' : ''}`}>
          <LayoutDashboard size={18} />
          <span>Dashboard</span>
        </NavLink>
      </nav>

      {/* Google connection status */}
      <div className="bb-sidebar-section">
        <p className="bb-sidebar-section-label">Google Ads</p>
        <div className={`bb-pill ${googleConnected ? 'bb-pill-on' : 'bb-pill-muted'}`} style={{ fontSize: '12px', padding: '4px 10px' }}>
          {googleConnected ? '● Connected' : '○ Not connected'}
        </div>
        {!googleConnected && (
          <p className="bb-muted" style={{ fontSize: '12px', marginTop: '6px' }}>
            Connect via an account's Settings page.
          </p>
        )}
      </div>

      {/* Footer */}
      <div className="bb-sidebar-footer">
        {user && (
          <p className="bb-muted" style={{ fontSize: '12px', marginBottom: '8px', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {user.email}
          </p>
        )}
        <button className="bb-btn bb-btn-ghost" onClick={handleLogout} style={{ width: '100%', justifyContent: 'flex-start', gap: '8px' }}>
          <LogOut size={16} />
          Logout
        </button>
      </div>
    </aside>
  );
}
