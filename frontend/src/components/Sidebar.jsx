import { useState } from 'react';
import { NavLink, useNavigate } from 'react-router-dom';
import { Home as HomeIcon, Bell, Settings, LogOut, Search } from 'lucide-react';
import { useAuth } from '../App';
import { useToast } from './Toast';
import Logo from './Logo';

function accountPaceStatus(account) {
  const s = account.pacing_status;
  if (s === 'over_pacing')  return 'over';
  if (s === 'under_pacing') return 'under';
  if (s === 'on_track')     return 'ok';
  return 'none';
}

export default function Sidebar({ accounts = [], unreadCount = 0 }) {
  const { user, logout } = useAuth();
  const navigate = useNavigate();
  const { addToast } = useToast();
  const [filter, setFilter] = useState('');

  const handleLogout = async () => {
    try {
      await logout();
      navigate('/login');
    } catch {
      addToast('Logout failed', 'error');
    }
  };

  const initials = user?.email ? user.email.slice(0, 2).toUpperCase() : 'CA';
  const q = filter.trim().toLowerCase();
  const shownAccounts = q
    ? accounts.filter(a => (a.account_name || '').toLowerCase().includes(q))
    : accounts;

  return (
    <aside className="sidebar">
      {/* Brand */}
      <div className="sidebar-brand" onClick={() => navigate('/')} style={{ cursor: 'pointer' }}>
        <Logo size={144} />
      </div>

      {/* Accounts */}
      <div className="nav-section-label">Accounts</div>
      <div className="sidebar-filter">
        <Search size={13} style={{ color: 'var(--muted)', flexShrink: 0 }} />
        <input
          placeholder="Filter accounts…"
          value={filter}
          onChange={e => setFilter(e.target.value)}
        />
      </div>
      <div className="sidebar-accounts">
        {shownAccounts.map(a => (
          <div
            key={a.id}
            className="recent-account-item"
            onClick={() => navigate(`/accounts/${a.id}`)}
          >
            <span className={`status-dot ${accountPaceStatus(a)}`} />
            <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {a.account_name}
            </span>
          </div>
        ))}
        {accounts.length > 0 && shownAccounts.length === 0 && (
          <div className="sidebar-empty">No matches</div>
        )}
      </div>

      {/* Navigation */}
      <div className="nav-section-label" style={{ marginTop: '12px' }}>Navigation</div>

      <NavLink to="/" end className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}>
        <HomeIcon size={15} />
        Home
      </NavLink>

      <NavLink to="/notifications" className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}>
        <Bell size={15} />
        Notifications
        {unreadCount > 0 && <span className="nav-badge">{unreadCount}</span>}
      </NavLink>

      <NavLink to="/settings" className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}>
        <Settings size={15} />
        Settings
      </NavLink>

      {/* User block */}
      <div className="sidebar-user">
        <div className="avatar">{initials}</div>
        <div className="info" style={{ flex: 1, minWidth: 0 }}>
          <div className="uname">Commit Agency</div>
          <div className="uemail" style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {user?.email || ''}
          </div>
        </div>
        <button
          onClick={handleLogout}
          style={{ background: 'none', border: 'none', cursor: 'pointer', color: 'var(--muted)', padding: '2px', display: 'flex', flexShrink: 0 }}
          title="Logout"
        >
          <LogOut size={14} />
        </button>
      </div>
    </aside>
  );
}
