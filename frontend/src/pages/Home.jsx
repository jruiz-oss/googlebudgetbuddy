import { useState, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import { Plus, RefreshCw, TrendingUp, TrendingDown, Minus, Settings, History, Download, Trash2, CloudDownload, Pencil, Check, X, RotateCcw } from 'lucide-react';
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

function ImportMccModal({ onClose, onImported, existingIds }) {
  const [accounts, setAccounts] = useState([]);
  // editedNames lets users fix account names before importing
  const [editedNames, setEditedNames] = useState({});
  const [selected, setSelected] = useState(new Set());
  const [mccId, setMccId] = useState('');
  const [loading, setLoading] = useState(false);
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState('');

  const fetchAccounts = async () => {
    setLoading(true);
    setError('');
    try {
      const r = await axios.get('/api/accounts/mcc/list', { params: { mcc_id: mccId.replace(/-/g, '') } });
      const all = r.data.accounts || [];
      // Pre-select accounts not already in the app
      const newOnes = new Set(all.filter(a => !existingIds.has(a.customer_id)).map(a => a.customer_id));
      setAccounts(all);
      setSelected(newOnes);
      // Reset any previous edits
      setEditedNames({});
    } catch (e) {
      setError(e.response?.data?.error || 'Failed to load accounts from MCC');
    } finally {
      setLoading(false);
    }
  };

  const toggle = (id) => setSelected(s => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const toggleAll = () => setSelected(selected.size === accounts.length ? new Set() : new Set(accounts.map(a => a.customer_id)));

  const getDisplayName = (a) => editedNames[a.customer_id] ?? a.name;

  const handleImport = async () => {
    if (!selected.size) return;
    setImporting(true);
    const toImport = accounts.filter(a => selected.has(a.customer_id));
    let added = 0;
    for (const acct of toImport) {
      try {
        await axios.post('/api/accounts', {
          account_name: getDisplayName(acct).trim() || acct.customer_id,
          google_customer_id: acct.customer_id,
          mcc_customer_id: mccId.replace(/-/g, '') || null,
        });
        added++;
      } catch { /* skip duplicates */ }
    }
    setImporting(false);
    onImported(added);
  };

  return (
    <div className="bb-modal-overlay" onClick={onClose}>
      <div className="bb-modal" style={{ maxWidth: '620px' }} onClick={e => e.stopPropagation()}>
        <div className="bb-modal-header">
          <h2 className="bb-section-title">Import All Accounts from MCC</h2>
          <button className="bb-btn bb-btn-ghost" onClick={onClose}>✕</button>
        </div>

        {error && <div className="bb-alert bb-alert-error">{error}</div>}

        <div className="bb-row" style={{ gap: '8px', marginBottom: '16px' }}>
          <input
            className="bb-input"
            value={mccId}
            onChange={e => setMccId(e.target.value)}
            placeholder="MCC ID (optional — leave blank to load all accessible accounts)"
            style={{ flex: 1 }}
          />
          <button className="bb-btn bb-btn-primary" onClick={fetchAccounts} disabled={loading}>
            {loading ? 'Loading…' : 'Load Accounts'}
          </button>
        </div>

        {accounts.length > 0 && (
          <>
            <div className="bb-row-between" style={{ marginBottom: '8px' }}>
              <span className="bb-muted" style={{ fontSize: '13px' }}>{accounts.length} account(s) found · {selected.size} selected</span>
              <button className="bb-btn bb-btn-ghost" style={{ fontSize: '13px', padding: '4px 8px' }} onClick={toggleAll}>
                {selected.size === accounts.length ? 'Deselect all' : 'Select all'}
              </button>
            </div>
            <p className="bb-muted" style={{ fontSize: '12px', marginBottom: '8px' }}>
              You can edit any account name before importing — just click the name field.
            </p>
            <div style={{ maxHeight: '340px', overflowY: 'auto', border: '1px solid var(--color-border)', borderRadius: '8px', marginBottom: '16px' }}>
              {accounts.map(a => {
                const alreadyExists = existingIds.has(a.customer_id);
                return (
                  <div
                    key={a.customer_id}
                    style={{
                      display: 'flex', alignItems: 'center', gap: '12px',
                      padding: '10px 14px', borderBottom: '1px solid var(--color-border)',
                      opacity: alreadyExists ? 0.5 : 1,
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={selected.has(a.customer_id)}
                      onChange={() => !alreadyExists && toggle(a.customer_id)}
                      disabled={alreadyExists}
                      style={{ flexShrink: 0, cursor: alreadyExists ? 'default' : 'pointer' }}
                    />
                    {/* Editable name field */}
                    <input
                      className="bb-input"
                      value={getDisplayName(a)}
                      onChange={e => setEditedNames(n => ({ ...n, [a.customer_id]: e.target.value }))}
                      disabled={alreadyExists}
                      style={{
                        flex: 1, fontWeight: 500, fontSize: '14px',
                        padding: '4px 8px', height: '32px',
                        background: alreadyExists ? 'transparent' : undefined,
                        border: alreadyExists ? 'none' : undefined,
                      }}
                    />
                    <span className="bb-muted" style={{ fontSize: '12px', whiteSpace: 'nowrap' }}>
                      ID: {a.customer_id}
                    </span>
                    {alreadyExists && <span className="bb-pill bb-pill-on" style={{ fontSize: '11px' }}>Already added</span>}
                  </div>
                );
              })}
            </div>
            <div className="bb-row" style={{ justifyContent: 'flex-end', gap: '8px' }}>
              <button className="bb-btn bb-btn-secondary" onClick={onClose}>Cancel</button>
              <button className="bb-btn bb-btn-primary" onClick={handleImport} disabled={!selected.size || importing}>
                {importing ? 'Importing…' : `Import ${selected.size} account(s)`}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
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
  const [syncing, setSyncing] = useState(false);
  const [showAdd, setShowAdd] = useState(false);
  const [showMcc, setShowMcc] = useState(false);
  // Inline rename state: { accountId: { editing: bool, value: string, saving: bool } }
  const [renameState, setRenameState] = useState({});
  const navigate = useNavigate();
  const { addToast } = useToast();

  const syncFromMcc = async () => {
    setSyncing(true);
    try {
      const r = await axios.post('/api/accounts/sync-from-mcc', {});
      const { updated, deleted, campaigns_added, campaigns_updated, message } = r.data;
      addToast(message, 'success');
      if (deleted?.length) {
        const noName = deleted.filter(d => d.reason === 'no_real_name');
        const notInMcc = deleted.filter(d => d.reason === 'not_in_mcc');
        noName.forEach(d => addToast(`Removed nameless account: ${d.customer_id}`, 'info'));
        notInMcc.forEach(d => addToast(`Removed unknown account: ${d.name || d.customer_id}`, 'info'));
      }
      load();
    } catch (e) {
      addToast(e.response?.data?.error || 'Sync failed', 'error');
    } finally {
      setSyncing(false);
    }
  };

  const startRename = (account, e) => {
    e.stopPropagation();
    setRenameState(s => ({ ...s, [account.id]: { editing: true, value: account.account_name, saving: false } }));
  };

  const cancelRename = (id, e) => {
    e && e.stopPropagation();
    setRenameState(s => { const n = { ...s }; delete n[id]; return n; });
  };

  const saveRename = async (account, e) => {
    e && e.stopPropagation();
    const rs = renameState[account.id];
    if (!rs) return;
    const newName = rs.value.trim();
    if (!newName || newName === account.account_name) { cancelRename(account.id); return; }
    setRenameState(s => ({ ...s, [account.id]: { ...s[account.id], saving: true } }));
    try {
      await axios.put(`/api/accounts/${account.id}`, { account_name: newName });
      addToast('Account renamed', 'success');
      cancelRename(account.id);
      load();
    } catch {
      addToast('Rename failed', 'error');
      setRenameState(s => ({ ...s, [account.id]: { ...s[account.id], saving: false } }));
    }
  };

  const load = async () => {
    setLoading(true);
    try {
      const r = await axios.get('/api/campaigns/all');
      const loaded = r.data.accounts || [];
      setAccounts(loaded);

      // Silently refresh any accounts whose name looks like a placeholder ID
      const hasPlaceholders = loaded.some(a => {
        const n = (a.account_name || '').trim();
        return (
          /^\d+$/.test(n) ||
          n.toLowerCase().startsWith('account ') ||
          n.replace(/-/g, '') === (a.google_customer_id || '').replace(/-/g, '')
        );
      });
      if (hasPlaceholders) {
        axios.post('/api/accounts/refresh-names').then(res => {
          if (res.data.refreshed > 0) load(); // reload with real names
        }).catch(() => {}); // silent — don't block the UI
      }
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
          <p className="bb-section-meta">All Google Ads accounts — Commit Agency</p>
        </div>
        <div className="bb-row" style={{ gap: '8px' }}>
          <button className="bb-btn bb-btn-secondary" onClick={syncFromMcc} disabled={syncing} title="Fix names + remove unknown accounts">
            <RotateCcw size={16} /> {syncing ? 'Syncing…' : 'Sync Accounts'}
          </button>
          <button className="bb-btn bb-btn-ghost" onClick={() => setShowMcc(true)}>
            <CloudDownload size={16} /> Import from MCC
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
              <div key={account.id} className="bb-card" style={{ cursor: 'pointer' }} onClick={() => !renameState[account.id]?.editing && navigate(`/accounts/${account.id}`)}>
                <div className="bb-row-between" style={{ marginBottom: '12px' }}>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    {renameState[account.id]?.editing ? (
                      <div className="bb-row" style={{ gap: '6px', alignItems: 'center' }} onClick={e => e.stopPropagation()}>
                        <input
                          className="bb-input"
                          value={renameState[account.id].value}
                          onChange={e => setRenameState(s => ({ ...s, [account.id]: { ...s[account.id], value: e.target.value } }))}
                          onKeyDown={e => { if (e.key === 'Enter') saveRename(account, e); if (e.key === 'Escape') cancelRename(account.id, e); }}
                          autoFocus
                          style={{ fontSize: '15px', fontWeight: 600, padding: '4px 8px', height: '34px' }}
                        />
                        <button className="bb-btn bb-btn-primary" style={{ padding: '4px 10px', height: '34px' }} onClick={e => saveRename(account, e)} disabled={renameState[account.id].saving}>
                          <Check size={14} />
                        </button>
                        <button className="bb-btn bb-btn-ghost" style={{ padding: '4px 10px', height: '34px' }} onClick={e => cancelRename(account.id, e)}>
                          <X size={14} />
                        </button>
                      </div>
                    ) : (
                      <div className="bb-row" style={{ gap: '6px', alignItems: 'center' }}>
                        <h2 className="bb-section-title" style={{ marginBottom: '2px' }}>{account.account_name}</h2>
                        <button
                          className="bb-btn bb-btn-ghost"
                          style={{ padding: '2px 6px', opacity: 0.5 }}
                          title="Rename account"
                          onClick={e => startRename(account, e)}
                        >
                          <Pencil size={13} />
                        </button>
                      </div>
                    )}
                    <p className="bb-muted" style={{ fontSize: '13px' }}>ID: {account.google_customer_id}</p>
                  </div>
                  <div className="bb-row" style={{ gap: '8px' }} onClick={e => e.stopPropagation()}>
                    <button className="bb-btn bb-btn-ghost" style={{ padding: '6px 10px' }} onClick={() => navigate(`/accounts/${account.id}/history`)}>
                      <History size={15} />
                    </button>
                    <button className="bb-btn bb-btn-ghost" style={{ padding: '6px 10px' }} onClick={() => navigate(`/accounts/${account.id}/settings`)}>
                      <Settings size={15} />
                    </button>
                    <button
                      className="bb-btn bb-btn-ghost"
                      style={{ padding: '6px 10px', color: 'var(--color-danger)' }}
                      onClick={async () => {
                        if (!confirm(`Delete "${account.account_name}"? This cannot be undone.`)) return;
                        try {
                          await axios.delete(`/api/accounts/${account.id}`);
                          addToast(`"${account.account_name}" deleted`, 'info');
                          load();
                        } catch {
                          addToast('Delete failed', 'error');
                        }
                      }}
                    >
                      <Trash2 size={15} />
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

      {showMcc && (
        <ImportMccModal
          onClose={() => setShowMcc(false)}
          existingIds={new Set(accounts.map(a => a.google_customer_id))}
          onImported={(count) => { setShowMcc(false); load(); addToast(`Imported ${count} account(s)`, 'success'); }}
        />
      )}
    </div>
  );
}
