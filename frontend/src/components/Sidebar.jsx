import { NavLink, useNavigate } from 'react-router-dom';
import { LayoutDashboard, Bell, Settings, LogOut } from 'lucide-react';
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

  const handleLogout = async () => {
    try {
      await logout();
      navigate('/login');
    } catch {
      addToast('Logout failed', 'error');
    }
  };

  const initials = user?.email ? user.email.slice(0, 2).toUpperCase() : 'CA';
  const recentAccounts = accounts.slice(0, 5);

  return (
    <aside className="sidebar">
      {/* Brand */}
      <div className="sidebar-brand">
        <Logo size={36} />
        <div>
          <div className="name">BudgetBuddy</div>
          <div className="sub">Google Ads pacing</div>
        </div>
      </div>

      {/* Workspace nav */}
      <div className="nav-section-label">Workspace</div>

      <NavLink to="/" end className={({ isActive }) => `nav-item${isActive ? ' active' : ''}`}>
        <LayoutDashboard size={15} />
        Dashboard
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

      {/* Recent accounts */}
      {recentAccounts.length > 0 && (
        <>
          <div className="nav-section-label" style={{ marginTop: '10px' }}>Recent Accounts</div>
          {recentAccounts.map(a => (
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
        </>
      )}

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
