import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { Plus, RefreshCw, TrendingUp, TrendingDown, Minus, Settings, History, Download } from 'lucide-react';
import axios from 'axios';
import { useToast } from '../components/Toast';
import { SkeletonAccountBlock } from '../components/Skeleton';
import EmptyState from '../components/EmptyState';

function StatusPill({ status }) {
  const map = {
    over_pacing:  { cls: 'bb-pill-down', icon: <TrendingDown size={12} />, label: 'Over Pacing' },
    under_pacing: { cls: 'bb-pill-up',   icon: <TrendingUp size={12} />,   label: 'Under Pacing' },
    on_track:     { cls: 'bb-pill-on',   icon: <Minus size={12} />,        label: 'On Track' },
  };
  const { cls, icon, label } = map[status] || map.on_track;
  return <span className={`bb-pill ${cls}`} style={{ display: 'inline-flex', alignItems: 'center', gap: '4px' }}>{icon}{label}</span>;
}

function AddAccountModal({ onClose, onAdded }) {
  const [name, setName] = useState('');
  const [customerId, setCustomerId] = useState('');
  const [mccId, setMccId] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');

  const submit = async (e) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      await axios.post('/api/accounts', {
        account_name: name,
        google_customer_id: customerId.replace(/-/g, ''),
        mcc_customer_id: mccId.replace(/-/g, '') || null,
      });
      onAdded();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to add account');
      setLoading(false);
    }
  };

  return (
    <div className="bb-modal-overlay" onClick={onClose}>
      <div className="bb-modal" onClick={e => e.stopPropagation()}>
        <div className="bb-modal-header">
          <h2 className="bb-section-title">Add Google Ads Account</h2>
          <button className="bb-btn bb-btn-ghost" onClick={onClose}>✕</button>
        </div>
        {error && <div className="bb-alert bb-alert-error">{error}</div>}
        <form onSubmit={submit}>
          <div className="bb-form-group">
            <label className="bb-form-label">Account Name</label>
            <input className="bb-input" value={name} onChange={e => setName(e.target.value)} placeholder="e.g. Harrah's Oklahoma" required />
          </div>
          <div className="bb-form-group">
            <label className="bb-form-label">Google Customer ID</label>
            <input className="bb-input" value={customerId} onChange={e => setCustomerId(e.target.value)} placeholder="123-456-7890" required />
            <p className="bb-form-help">Found in Google Ads top-right corner. Dashes OK.</p>
          </div>
          <div className="bb-form-group">
            <label className="bb-form-label">MCC / Manager Account ID (optional)</label>
            <input className="bb-input" value={mccId} onChange={e => setMccId(e.target.value)} placeholder="Leave blank if this is a standalone account" />
          </div>
          <div className="bb-row" style={{ justifyContent: 'flex-end', gap: '8px', marginTop: '16px' }}>
            <button type="button" className="bb-btn bb-btn-secondary" onClick={onClose}>Cancel</button>
            <button type="submit" className="bb-btn bb-btn-primary" disabled={loading}>
              {loading ? 'Adding…' : 'Add Account'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default function Home() {
  const [accounts, setAccounts] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showAdd, setShowAdd] = useState(false);
  const navigate = useNavigate();
  const { addToast } = useToast();

  const load = async () => {
    setLoading(true);
    try {
      const r = await axios.get('/api/campaigns/all');
      setAccounts(r.data.accounts || []);
    } catch {
      addToast('Failed to load accounts', 'error');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => { load(); }, []);

  // Aggregate stats across all accounts
  const allCampaigns = accounts.flatMap(a => a.campaigns || []);
  const totalBudget = allCampaigns.reduce((s, c) => s + (c.monthly_budget || 0), 0);
  const totalSpend = allCampaigns.reduce((s, c) => s + (c.latest_pacing?.actual_spend || 0), 0);
  const paceCount = { increase: 0, decrease: 0, on_pace: 0 };
  allCampaigns.forEach(c => {
    const st = c.latest_pacing?.status;
    if (st === 'INCREASE') paceCount.increase++;
    else if (st === 'DECREASE') paceCount.decrease++;
    else if (st === 'ON_PACE') paceCount.on_pace++;
  });

  return (
    <div>
      {/* Header */}
      <div className="bb-row-between" style={{ marginBottom: '24px' }}>
        <div>
          <h1 className="bb-page-title">Dashboard</h1>
          <p className="bb-section-meta">All Google Ads accounts</p>
        </div>
        <div className="bb-row" style={{ gap: '8px' }}>
          <button className="bb-btn bb-btn-ghost" onClick={load}>
            <RefreshCw size={16} /> Refresh
          </button>
          <button className="bb-btn bb-btn-primary" onClick={() => setShowAdd(true)}>
            <Plus size={16} /> Add Account
          </button>
        </div>
      </div>

      {/* Top stats */}
      {!loading && accounts.length > 0 && (
        <div className="bb-grid" style={{ gridTemplateColumns: 'repeat(4, 1fr)', marginBottom: '32px' }}>
          <div className="bb-card">
            <p className="bb-section-meta">Total Monthly Budget</p>
            <p className="bb-stat-value">${totalBudget.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</p>
          </div>
          <div className="bb-card">
            <p className="bb-section-meta">MTD Spend</p>
            <p className="bb-stat-value">${totalSpend.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</p>
          </div>
          <div className="bb-card">
            <p className="bb-section-meta">Campaigns Tracked</p>
            <p className="bb-stat-value">{allCampaigns.length}</p>
          </div>
          <div className="bb-card">
            <p className="bb-section-meta">Need Attention</p>
            <p className="bb-stat-value" style={{ color: 'var(--color-danger)' }}>{paceCount.increase + paceCount.decrease}</p>
          </div>
        </div>
      )}

      {/* Account list */}
      {loading ? (
        <div>{[1,2,3].map(i => <SkeletonAccountBlock key={i} />)}</div>
      ) : accounts.length === 0 ? (
        <EmptyState
          icon={<Plus size={32} />}
          title="No accounts yet"
          body="Add your first Google Ads account to start tracking pacing."
          action={{ label: 'Add Account', onClick: () => setShowAdd(true) }}
        />
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: '16px' }}>
          {accounts.map(account => {
            const activeCampaigns = (account.campaigns || []).filter(c => c.is_active !== false);
            const acctBudget = activeCampaigns.reduce((s, c) => s + (c.monthly_budget || 0), 0);
            const acctSpend = activeCampaigns.reduce((s, c) => s + (c.latest_pacing?.actual_spend || 0), 0);
            const spendPct = acctBudget > 0 ? Math.min((acctSpend / acctBudget) * 100, 100) : 0;

            return (
              <div key={account.id} className="bb-card" style={{ cursor: 'pointer' }} onClick={() => navigate(`/accounts/${account.id}`)}>
                <div className="bb-row-between" style={{ marginBottom: '12px' }}>
                  <div>
                    <h2 className="bb-section-title" style={{ marginBottom: '2px' }}>{account.account_name}</h2>
                    <p className="bb-muted" style={{ fontSize: '13px' }}>ID: {account.google_customer_id}</p>
                  </div>
                  <div className="bb-row" style={{ gap: '8px' }} onClick={e => e.stopPropagation()}>
                    <button className="bb-btn bb-btn-ghost" style={{ padding: '6px 10px' }} onClick={() => navigate(`/accounts/${account.id}/history`)}>
                      <History size={15} />
                    </button>
                    <button className="bb-btn bb-btn-ghost" style={{ padding: '6px 10px' }} onClick={() => navigate(`/accounts/${account.id}/settings`)}>
                      <Settings size={15} />
                    </button>
                    <button className="bb-btn bb-btn-primary" style={{ padding: '6px 14px', fontSize: '13px' }} onClick={() => navigate(`/accounts/${account.id}`)}>
                      View
                    </button>
                  </div>
                </div>

                {/* Budget progress bar */}
                <div style={{ marginBottom: '12px' }}>
                  <div className="bb-row-between" style={{ marginBottom: '4px' }}>
                    <span className="bb-muted" style={{ fontSize: '13px' }}>
                      ${acctSpend.toLocaleString('en-US', { maximumFractionDigits: 0 })} spent of ${acctBudget.toLocaleString('en-US', { maximumFractionDigits: 0 })}
                    </span>
                    <span className="bb-muted" style={{ fontSize: '13px' }}>{spendPct.toFixed(1)}%</span>
                  </div>
                  <div style={{ background: 'var(--color-border)', borderRadius: '4px', height: '6px' }}>
                    <div style={{
                      background: spendPct >= 95 ? 'var(--color-danger)' : spendPct >= 80 ? 'var(--color-warning)' : 'var(--color-primary)',
                      width: `${spendPct}%`,
                      height: '100%',
                      borderRadius: '4px',
                      transition: 'width 0.3s',
                    }} />
                  </div>
                </div>

                {/* Campaign pacing pills */}
                {activeCampaigns.length > 0 && (
                  <div className="bb-row" style={{ gap: '8px', flexWrap: 'wrap' }}>
                    {activeCampaigns.slice(0, 6).map(c => (
                      <div key={c.id} className="bb-row" style={{ gap: '6px', alignItems: 'center', fontSize: '13px' }}>
                        <span style={{
                          width: '8px', height: '8px', borderRadius: '50%',
                          background: c.latest_pacing?.status === 'DECREASE' ? 'var(--color-danger)'
                            : c.latest_pacing?.status === 'INCREASE' ? 'var(--color-warning)'
                            : 'var(--color-success)',
                        }} />
                        <span className="bb-muted">{c.campaign_name}</span>
                      </div>
                    ))}
                    {activeCampaigns.length > 6 && (
                      <span className="bb-muted" style={{ fontSize: '13px' }}>+{activeCampaigns.length - 6} more</span>
                    )}
                  </div>
                )}
              </div>
            );
          })}
        </div>
      )}

      {showAdd && (
        <AddAccountModal
          onClose={() => setShowAdd(false)}
          onAdded={() => { setShowAdd(false); load(); addToast('Account added', 'success'); }}
        />
      )}
    </div>
  );
}
