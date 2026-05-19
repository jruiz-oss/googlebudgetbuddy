import { useState, useEffect, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { Plus, CloudDownload, Play, Search, ChevronRight, ArrowRight, Trash2 } from 'lucide-react';
import axios from 'axios';
import { useToast } from '../components/Toast';

// ── Pacing math ──────────────────────────────────────────────────────────
function getDaysInfo() {
  const today       = new Date();
  const daysIn      = today.getDate();
  const daysInMonth = new Date(today.getFullYear(), today.getMonth() + 1, 0).getDate();
  return { daysIn, daysInMonth, daysLeft: daysInMonth - daysIn };
}

function computePace(monthly, spend, daysIn, daysInMonth) {
  const idealSpend   = monthly > 0 ? monthly * (daysIn / daysInMonth) : 0;
  const deltaPct     = idealSpend > 0 ? ((spend / idealSpend) - 1) * 100 : 0;
  const status       = deltaPct > 5 ? 'over' : deltaPct < -5 ? 'under' : 'ok';
  const daysLeft     = daysInMonth - daysIn;
  const dailyCurrent = daysIn > 0 ? spend / daysIn : 0;
  // Matches the Google Sheet formula: (Budget - Spend) / days_in_month
  // Dividing by the full month (not just remaining days) stays consistent with
  // the sheet's =(C-D)/$E$2 calculation where $E$2 = total days in month.
  const dailyRec     = daysInMonth > 0 ? Math.max(0, monthly - spend) / daysInMonth : 0;
  const pctOfBudget  = monthly > 0 ? (spend / monthly) * 100 : 0;
  return { idealSpend, deltaPct, status, daysLeft, dailyCurrent, dailyRec, pctOfBudget };
}

function fmt(n) {
  if (n == null || isNaN(n)) return '$0';
  return '$' + Math.round(n).toLocaleString('en-US');
}
function fmtPct(n) {
  if (n == null || isNaN(n)) return '0.0%';
  return (n > 0 ? '+' : '') + n.toFixed(1) + '%';
}

function getSegments(account) {
  const campaigns = account.campaigns || [];
  if (!campaigns.length) return [];
  // Only use the most recent pacing run's data — older entries are stale.
  const mostRecentDate = campaigns.reduce((latest, c) => {
    const d = c.latest_pacing?.date;
    if (!d) return latest;
    return !latest || d > latest ? d : latest;
  }, null);
  const map = {};
  const seenGids = new Set();
  for (const c of campaigns) {
    if (mostRecentDate && c.latest_pacing?.date !== mostRecentDate) continue;
    const label = c.budget_label || 'Primary';
    if (!map[label]) map[label] = { name: label, monthly: 0, spend: 0 };
    map[label].monthly = Math.max(map[label].monthly, c.monthly_budget || 0);
    if (!c.google_campaign_id || !seenGids.has(c.google_campaign_id)) {
      seenGids.add(c.google_campaign_id);
      map[label].spend += c.latest_pacing?.actual_spend || 0;
    }
  }
  return Object.values(map);
}

function accountPacing(account, daysIn, daysInMonth) {
  const campaigns = account.campaigns || [];
  const segBudgets = {};
  for (const c of campaigns) {
    const label = c.budget_label || 'Primary';
    segBudgets[label] = Math.max(segBudgets[label] || 0, c.monthly_budget || 0);
  }
  const monthly = Object.values(segBudgets).reduce((s, b) => s + b, 0);
  // Only sum spend from the most recent pacing run (avoid stale campaigns).
  // Also dedup by google_campaign_id to handle duplicate DB rows.
  const mostRecentDate = campaigns.reduce((latest, c) => {
    const d = c.latest_pacing?.date;
    if (!d) return latest;
    return !latest || d > latest ? d : latest;
  }, null);
  const seenGids = new Set();
  const spend = campaigns.reduce((s, c) => {
    if (mostRecentDate && c.latest_pacing?.date !== mostRecentDate) return s;
    if (c.google_campaign_id && seenGids.has(c.google_campaign_id)) return s;
    seenGids.add(c.google_campaign_id);
    return s + (c.latest_pacing?.actual_spend || 0);
  }, 0);
  const pace    = computePace(monthly, spend, daysIn, daysInMonth);
  const segments = getSegments(account);
  return { monthly, spend, pace, segments };
}

// ── HiFi Switch ──────────────────────────────────────────────────────────
function Switch({ on, onChange }) {
  return (
    <label className="switch" onClick={e => e.stopPropagation()}>
      <input type="checkbox" checked={on} onChange={e => onChange(e.target.checked)} />
      <span className="switch-track" />
      <span className="switch-knob" />
    </label>
  );
}

// ── Pace bar ─────────────────────────────────────────────────────────────
function PaceBar({ spend, monthly, daysIn, daysInMonth, status }) {
  const pct      = monthly > 0 ? Math.min((spend / monthly) * 100, 100) : 0;
  const idealPct = Math.min((daysIn / daysInMonth) * 100, 99);
  const fillColor = status === 'over' ? 'var(--red)' : status === 'under' ? 'var(--amber)' : 'var(--green)';
  return (
    <div className="pace-bar-wrap">
      <div className="pace-bar-fill" style={{ width: `${pct}%`, background: fillColor }} />
      <div className="pace-bar-tick" style={{ left: `${idealPct}%` }} />
    </div>
  );
}

// ── Apply Modal ──────────────────────────────────────────────────────────
function ApplyModal({ item, onClose, onConfirm }) {
  if (!item) return null;
  const { daysIn, daysInMonth, daysLeft } = getDaysInfo();
  const pace = computePace(item.monthly, item.spend, daysIn, daysInMonth);
  const current   = pace.dailyCurrent;
  const rec       = pace.dailyRec;
  const diff      = Math.abs(rec - current);
  const direction = rec > current ? 'increase' : 'decrease';

  if (item.bulk) {
    return (
      <div className="modal-backdrop" onClick={onClose}>
        <div className="modal modal-wide" onClick={e => e.stopPropagation()}>
          <h3>Apply all recommended daily budgets</h3>
          <div className="subtle">{item.name} · {item.segments.length} segments</div>
          <table className="modal-seg-table">
            <thead><tr><th>Segment</th><th>Now</th><th>New daily</th></tr></thead>
            <tbody>
              {item.segments.map(s => {
                const sp = computePace(s.monthly, s.spend, daysIn, daysInMonth);
                return (
                  <tr key={s.name}>
                    <td>{s.name}</td>
                    <td>{fmt(sp.dailyCurrent)}</td>
                    <td className="new-daily">{fmt(sp.dailyRec)}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          <div className="mcopy">New daily budgets are calculated to spend each segment's remaining budget evenly across the {daysLeft} days left this month.</div>
          <div className="footer-row">
            <button className="btn ghost" onClick={onClose}>Cancel</button>
            <button className="btn primary" onClick={() => onConfirm(item)}>Push {item.segments.length} updates to Google Ads</button>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <h3>Apply recommended daily budget</h3>
        <div className="subtle">{item.name}</div>
        <div className="diff-card">
          <div className="dcol from">
            <div className="dk">Current daily</div>
            <div className="dv">{fmt(current)}</div>
          </div>
          <div className="darrow"><ArrowRight size={14} /></div>
          <div className="dcol">
            <div className="dk">New daily</div>
            <div className="dv" style={{ color: 'var(--green)' }}>{fmt(rec)}</div>
          </div>
        </div>
        <div className="mcopy">
          {direction === 'increase'
            ? `An increase of ${fmt(diff)}/day to catch up to the monthly target.`
            : `A decrease of ${fmt(diff)}/day to stay within the monthly target.`}{' '}
          Calculated over the remaining {daysLeft} days of the month.
        </div>
        <div className="footer-row">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn primary" onClick={() => onConfirm(item)}>Push to Google Ads</button>
        </div>
      </div>
    </div>
  );
}

// ── Account Card ─────────────────────────────────────────────────────────
function AccountCard({ account, daysIn, daysInMonth, capStates, setCap, onApply, navigate }) {
  const { monthly, spend, pace, segments } = accountPacing(account, daysIn, daysInMonth);
  const isSegmented = segments.length > 1;

  const handleCTA = (e) => {
    e.stopPropagation();
    if (isSegmented) {
      navigate(`/accounts/${account.id}`);
    } else {
      // Pass accountId and campaigns so the confirm handler can build adjustments
      onApply({ name: account.account_name, accountId: account.id, campaigns: account.campaigns || [], monthly, spend });
    }
  };

  return (
    <div className={`account-card ${pace.status}`} onClick={() => navigate(`/accounts/${account.id}`)}>
      <div className="card-inner">
        <div className="card-head">
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="card-name" title={account.account_name}>{account.account_name}</div>
            <div className="card-meta">{isSegmented ? `${segments.length} segments` : 'single budget'}</div>
          </div>
          <span className={`pill ${pace.status}`}>{fmtPct(pace.deltaPct)}</span>
        </div>

        <PaceBar spend={spend} monthly={monthly} daysIn={daysIn} daysInMonth={daysInMonth} status={pace.status} />

        <div className="card-stats">
          <span className="sk">MTD spend</span>
          <span className="sv">{fmt(spend)} <span className="smuted">/ {fmt(monthly)}</span></span>
          <span className="sk">Daily now</span>
          <span className="sv">{fmt(pace.dailyCurrent)}</span>
          <span className="sk">Daily rec</span>
          <span className="sv rec">{fmt(pace.dailyRec)}</span>
        </div>

        <button className={`card-cta ${isSegmented ? 'outline' : 'primary'}`} onClick={handleCTA}>
          {isSegmented
            ? <><span>review {segments.length} segments</span><ChevronRight size={13} /></>
            : <span>set daily to {fmt(pace.dailyRec)}</span>}
        </button>
      </div>

      <div className="card-foot" onClick={e => e.stopPropagation()}>
        <span className="label-sm">cap at 100%</span>
        <Switch
          on={capStates[account.id] ?? Boolean(account.settings?.auto_pause_enabled)}
          onChange={(v) => setCap(account.id, v)}
        />
      </div>
    </div>
  );
}

// ── Summary Bar ──────────────────────────────────────────────────────────
function SummaryBar({ accounts, daysIn, daysInMonth }) {
  const stats = useMemo(() => {
    let totalMonthly = 0, totalSpend = 0, totalIdeal = 0, over = 0, under = 0;
    for (const a of accounts) {
      const { monthly, spend, pace } = accountPacing(a, daysIn, daysInMonth);
      totalMonthly += monthly;
      totalSpend   += spend;
      totalIdeal   += pace.idealSpend;
      if (pace.status === 'over')  over++;
      if (pace.status === 'under') under++;
    }
    const portfolioDelta = totalIdeal > 0 ? ((totalSpend / totalIdeal) - 1) * 100 : 0;
    const pStatus = portfolioDelta > 5 ? 'over' : portfolioDelta < -5 ? 'under' : 'green';
    return { totalMonthly, totalSpend, totalIdeal, portfolioDelta, pStatus, over, under };
  }, [accounts, daysIn, daysInMonth]);

  return (
    <div className="summary">
      <div className="cell">
        <div className="k">Accounts</div>
        <div className="v">{accounts.length}</div>
        <div className="sub">day {daysIn} / {daysInMonth} · {daysInMonth - daysIn} days left</div>
      </div>
      <div className="cell">
        <div className="k">Portfolio pace</div>
        <div className={`v ${stats.pStatus}`}>{fmtPct(stats.portfolioDelta)}</div>
        <div className="sub">{fmt(stats.totalSpend)} vs ideal {fmt(stats.totalIdeal)}</div>
      </div>
      <div className="cell">
        <div className="k">Monthly committed</div>
        <div className="v">{fmt(stats.totalMonthly)}</div>
        <div className="sub">{stats.totalMonthly > 0 ? ((stats.totalSpend / stats.totalMonthly) * 100).toFixed(0) : 0}% used</div>
      </div>
      <div className="cell">
        <div className="k">Over pace</div>
        <div className="v over">{stats.over}</div>
        <div className="sub">crossed +5% threshold</div>
      </div>
      <div className="cell">
        <div className="k">Under pace</div>
        <div className="v under">{stats.under}</div>
        <div className="sub">crossed −5% threshold</div>
      </div>
    </div>
  );
}

// ── Import MCC Modal ──────────────────────────────────────────────────────
function ImportMccModal({ onClose, onImported, existingIds }) {
  const [accounts, setAccounts] = useState([]);
  const [selected, setSelected] = useState(new Set());
  const [mccId, setMccId]       = useState('');
  const [loading, setLoading]   = useState(false);
  const [importing, setImporting] = useState(false);
  const [error, setError]       = useState('');

  const fetchAccounts = async () => {
    setLoading(true); setError('');
    try {
      const r = await axios.get('/api/accounts/mcc/list', { params: { mcc_id: mccId.replace(/-/g, '') } });
      const all = r.data.accounts || [];
      setAccounts(all);
      setSelected(new Set(all.filter(a => !existingIds.has(a.customer_id)).map(a => a.customer_id)));
    } catch (e) { setError(e.response?.data?.error || 'Failed to load accounts from MCC'); }
    finally { setLoading(false); }
  };

  const toggle    = (id) => setSelected(s => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const toggleAll = () => setSelected(selected.size === accounts.length ? new Set() : new Set(accounts.map(a => a.customer_id)));

  const handleImport = async () => {
    if (!selected.size) return;
    setImporting(true);
    let added = 0;
    for (const acct of accounts.filter(a => selected.has(a.customer_id))) {
      try {
        await axios.post('/api/accounts', { account_name: acct.name.trim() || acct.customer_id, google_customer_id: acct.customer_id, mcc_customer_id: mccId.replace(/-/g, '') || null });
        added++;
      } catch { /* skip duplicates */ }
    }
    setImporting(false); onImported(added);
  };

  return (
    <div className="bb-modal-overlay" onClick={onClose}>
      <div className="bb-modal" style={{ maxWidth: '600px' }} onClick={e => e.stopPropagation()}>
        <div className="bb-modal-header">
          <span className="bb-section-title">Import All Accounts from MCC</span>
          <button className="bb-btn bb-btn-ghost" onClick={onClose}>✕</button>
        </div>
        {error && <div className="bb-alert bb-alert-error">{error}</div>}
        <div className="bb-row" style={{ gap: '8px', marginBottom: '14px' }}>
          <input className="bb-input" value={mccId} onChange={e => setMccId(e.target.value)} placeholder="MCC ID (optional)" style={{ flex: 1 }} />
          <button className="bb-btn bb-btn-primary" onClick={fetchAccounts} disabled={loading}>{loading ? 'Loading…' : 'Load Accounts'}</button>
        </div>
        {accounts.length > 0 && (
          <>
            <div className="bb-row-between" style={{ marginBottom: '8px' }}>
              <span style={{ fontSize: 'var(--t-sm)', color: 'var(--muted)' }}>{accounts.length} found · {selected.size} selected</span>
              <button className="bb-btn bb-btn-ghost" style={{ fontSize: 'var(--t-sm)' }} onClick={toggleAll}>{selected.size === accounts.length ? 'Deselect all' : 'Select all'}</button>
            </div>
            <div style={{ maxHeight: '300px', overflowY: 'auto', border: '1px solid var(--line)', borderRadius: 'var(--r)', marginBottom: '14px' }}>
              {accounts.map(a => {
                const exists = existingIds.has(a.customer_id);
                return (
                  <div key={a.customer_id} style={{ display: 'flex', alignItems: 'center', gap: '12px', padding: '9px 13px', borderBottom: '1px solid var(--line)', opacity: exists ? 0.5 : 1 }}>
                    <input type="checkbox" checked={selected.has(a.customer_id)} onChange={() => !exists && toggle(a.customer_id)} disabled={exists} />
                    <span style={{ flex: 1, fontWeight: 500, fontSize: 'var(--t-md)' }}>{a.name}</span>
                    <span style={{ fontSize: 'var(--t-xs)', color: 'var(--muted)' }}>ID: {a.customer_id}</span>
                    {exists && <span className="bb-pill bb-pill-on" style={{ fontSize: 10 }}>Added</span>}
                  </div>
                );
              })}
            </div>
            <div className="bb-row" style={{ justifyContent: 'flex-end', gap: '8px' }}>
              <button className="bb-btn bb-btn-secondary" onClick={onClose}>Cancel</button>
              <button className="bb-btn bb-btn-primary" onClick={handleImport} disabled={!selected.size || importing}>{importing ? 'Importing…' : `Import ${selected.size} account(s)`}</button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ── Add Account Modal ─────────────────────────────────────────────────────
function AddAccountModal({ onClose, onAdded }) {
  const [name, setName]             = useState('');
  const [customerId, setCustomerId] = useState('');
  const [mccId, setMccId]           = useState('');
  const [loading, setLoading]       = useState(false);
  const [error, setError]           = useState('');

  const submit = async (e) => {
    e.preventDefault(); setError(''); setLoading(true);
    try {
      await axios.post('/api/accounts', { account_name: name, google_customer_id: customerId.replace(/-/g, ''), mcc_customer_id: mccId.replace(/-/g, '') || null });
      onAdded();
    } catch (err) { setError(err.response?.data?.error || 'Failed to add account'); setLoading(false); }
  };

  return (
    <div className="bb-modal-overlay" onClick={onClose}>
      <div className="bb-modal" onClick={e => e.stopPropagation()}>
        <div className="bb-modal-header">
          <span className="bb-section-title">Add Google Ads Account</span>
          <button className="bb-btn bb-btn-ghost" onClick={onClose}>✕</button>
        </div>
        {error && <div className="bb-alert bb-alert-error">{error}</div>}
        <form onSubmit={submit}>
          <div className="bb-form-group">
            <label className="bb-form-label">Account Name</label>
            <input className="bb-input" value={name} onChange={e => setName(e.target.value)} required />
          </div>
          <div className="bb-form-group">
            <label className="bb-form-label">Google Customer ID</label>
            <input className="bb-input" value={customerId} onChange={e => setCustomerId(e.target.value)} placeholder="123-456-7890" required />
            <p style={{ fontSize: 'var(--t-xs)', color: 'var(--muted)', marginTop: 3 }}>From URL: /d/<strong>THIS PART</strong>/edit</p>
          </div>
          <div className="bb-form-group">
            <label className="bb-form-label">MCC / Manager Account ID (optional)</label>
            <input className="bb-input" value={mccId} onChange={e => setMccId(e.target.value)} />
          </div>
          <div className="bb-row" style={{ justifyContent: 'flex-end', gap: '8px' }}>
            <button type="button" className="bb-btn bb-btn-secondary" onClick={onClose}>Cancel</button>
            <button type="submit" className="bb-btn bb-btn-primary" disabled={loading}>{loading ? 'Adding…' : 'Add Account'}</button>
          </div>
        </form>
      </div>
    </div>
  );
}

// ── Home (main export) ────────────────────────────────────────────────────
export default function Home({ onAccountsChange, accounts: propAccounts }) {
  const navigate       = useNavigate();
  const toast          = useToast();
  const { daysIn, daysInMonth } = getDaysInfo();

  const [accounts, setAccounts]   = useState(propAccounts || []);
  const [loading, setLoading]     = useState(!propAccounts?.length);
  const [runningAll, setRunningAll] = useState(false);
  const [showAdd, setShowAdd]     = useState(false);
  const [showMcc, setShowMcc]     = useState(false);
  const [capStates, setCapStates] = useState({});
  const [applyItem, setApplyItem] = useState(null);
  const [filter, setFilter]       = useState('all');
  const [sort, setSort]           = useState('worst');
  const [q, setQ]                 = useState('');

  useEffect(() => {
    if (propAccounts?.length) { setAccounts(propAccounts); setLoading(false); }
  }, [propAccounts]);

  const load = async () => {
    setLoading(true);
    try {
      const r = await axios.get('/api/campaigns/all');
      const loaded = r.data.accounts || [];
      setAccounts(loaded);
      const hasPlaceholders = loaded.some(a => {
        const n = (a.account_name || '').trim();
        return /^\d+$/.test(n) || n.toLowerCase().startsWith('account ') || n.replace(/-/g, '') === (a.google_customer_id || '').replace(/-/g, '');
      });
      if (hasPlaceholders) {
        axios.post('/api/accounts/refresh-names').then(res => { if (res.data.refreshed > 0) load(); }).catch(() => {});
      }
    } catch { toast.error('Failed to load accounts'); }
    finally { setLoading(false); }
  };

  useEffect(() => { if (!propAccounts?.length) load(); }, []);

  const runAllPacing = async () => {
    setRunningAll(true);
    toast.info(`Kicking off pacing for ${accounts.length} account(s)…`);
    try {
      await axios.post('/api/pacing/run-all');
      // Backend returns 202 immediately — actual pacing runs in a background thread.
      // Keep the button disabled and auto-refresh once the job should be done.
      toast.info('Pacing running in background — page will refresh in ~60 seconds.');
      setTimeout(() => {
        setRunningAll(false);
        load();
        onAccountsChange?.();
      }, 60000);
    } catch (e) {
      if (e.response?.status === 409) {
        toast.warn('Pacing already in progress — check back in about a minute.');
      } else {
        toast.error(e.response?.data?.error || 'Run all pacing failed');
      }
      setRunningAll(false);
    }
  };

  const setCap = async (accountId, value) => {
    setCapStates(s => ({ ...s, [accountId]: value }));
    try { await axios.put(`/api/settings/${accountId}`, { auto_pause_enabled: value }); } catch { /* silent */ }
  };

  const handleConfirmApply = async (item) => {
    setApplyItem(null);
    const { daysIn, daysInMonth } = getDaysInfo();
    const pace = computePace(item.monthly, item.spend, daysIn, daysInMonth);

    // Build one adjustment per campaign, splitting dailyRec evenly across the segment.
    // Each campaign in a segment shares the segment's monthly budget, so each gets an
    // equal slice of the recommended daily rate.
    const eligible = (item.campaigns || []).filter(c => c.budget_resource_name);
    if (!eligible.length) {
      toast.warn('No campaigns have a budget resource name yet — run pacing first to populate them.');
      return;
    }
    const perCampaign = Math.round((pace.dailyRec / eligible.length) * 100) / 100;
    const adjustments = eligible.map(c => ({
      campaign_id:          c.id,
      budget_resource_name: c.budget_resource_name,
      new_daily_budget:     perCampaign,
    }));

    toast.info(`Pushing ${eligible.length} budget(s) to Google Ads…`);
    try {
      const r = await axios.post(`/api/pacing/${item.accountId}/apply`, { adjustments });
      toast.success(r.data.message || 'Daily budgets updated in Google Ads');
      load();
      onAccountsChange?.();
    } catch (e) {
      toast.error(e.response?.data?.error || 'Failed to push to Google Ads');
    }
  };

  // Filter + sort
  const filteredAccounts = useMemo(() => {
    let list = accounts.map(a => {
      const { pace } = accountPacing(a, daysIn, daysInMonth);
      return { a, status: pace.status };
    });
    if (filter !== 'all') list = list.filter(x => x.status === filter);
    if (q.trim()) {
      const Q = q.trim().toLowerCase();
      list = list.filter(x => x.a.account_name.toLowerCase().includes(Q));
    }
    if (sort === 'worst') {
      list.sort((x, y) => {
        const xp = accountPacing(x.a, daysIn, daysInMonth).pace;
        const yp = accountPacing(y.a, daysIn, daysInMonth).pace;
        return Math.abs(yp.deltaPct) - Math.abs(xp.deltaPct);
      });
    }
    if (sort === 'spend') {
      list.sort((x, y) => accountPacing(y.a, daysIn, daysInMonth).spend - accountPacing(x.a, daysIn, daysInMonth).spend);
    }
    if (sort === 'name') list.sort((x, y) => x.a.account_name.localeCompare(y.a.account_name));
    return list.map(x => x.a);
  }, [accounts, filter, sort, q, daysIn, daysInMonth]);

  return (
    <div>
      {/* Top utility row */}
      <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '8px', marginBottom: '14px' }}>
        <button className="btn ghost small" onClick={() => setShowMcc(true)}>
          <CloudDownload size={13} /> Import MCC
        </button>
        <button className="btn small" onClick={runAllPacing} disabled={runningAll || !accounts.length}>
          <Play size={13} /> {runningAll ? 'Running…' : 'Run All Pacing'}
        </button>
        <button className="btn small" onClick={() => setShowAdd(true)}>
          <Plus size={13} /> Add Account
        </button>
      </div>

      {/* Summary strip */}
      {!loading && accounts.length > 0 && (
        <SummaryBar accounts={accounts} daysIn={daysIn} daysInMonth={daysInMonth} />
      )}

      {/* Filter bar */}
      {!loading && accounts.length > 0 && (
        <div className="filterbar">
          <div className="search-box">
            <Search size={13} style={{ color: 'var(--muted)', flexShrink: 0 }} />
            <input placeholder="Search accounts…" value={q} onChange={e => setQ(e.target.value)} />
          </div>
          <div className="segctrl">
            <button className={filter === 'all'   ? 'active' : ''} onClick={() => setFilter('all')}>All</button>
            <button className={filter === 'over'  ? 'active' : ''} onClick={() => setFilter('over')}>Over</button>
            <button className={filter === 'under' ? 'active' : ''} onClick={() => setFilter('under')}>Under</button>
            <button className={filter === 'ok'    ? 'active' : ''} onClick={() => setFilter('ok')}>On pace</button>
          </div>
          <div style={{ flex: 1 }} />
          <span className="label-sm">Sort</span>
          <div className="segctrl">
            <button className={sort === 'worst' ? 'active' : ''} onClick={() => setSort('worst')}>Worst pace</button>
            <button className={sort === 'spend' ? 'active' : ''} onClick={() => setSort('spend')}>By spend</button>
            <button className={sort === 'name'  ? 'active' : ''} onClick={() => setSort('name')}>A–Z</button>
          </div>
        </div>
      )}

      {/* Cards */}
      {loading ? (
        <div className="cards-grid">
          {[1,2,3,4,5,6,7,8].map(i => (
            <div key={i} style={{ height: '228px', borderRadius: 'var(--r)', overflow: 'hidden' }}>
              <div className="bb-skeleton" style={{ height: '100%' }} />
            </div>
          ))}
        </div>
      ) : accounts.length === 0 ? (
        <div className="bb-empty">
          <div className="bb-empty-icon"><Plus size={28} /></div>
          <div className="bb-empty-title">No accounts yet</div>
          <div className="bb-empty-body">Add your first Google Ads account to start tracking pacing.</div>
          <div className="bb-empty-actions">
            <button className="bb-btn bb-btn-primary" onClick={() => setShowAdd(true)}><Plus size={14} /> Add Account</button>
            <button className="bb-btn bb-btn-secondary" onClick={() => setShowMcc(true)}><CloudDownload size={14} /> Import from MCC</button>
          </div>
        </div>
      ) : (
        <>
          <div className="cards-grid">
            {filteredAccounts.map(account => (
              <AccountCard
                key={account.id}
                account={account}
                daysIn={daysIn}
                daysInMonth={daysInMonth}
                capStates={capStates}
                setCap={setCap}
                onApply={setApplyItem}
                navigate={navigate}
              />
            ))}
          </div>
          {filteredAccounts.length === 0 && (
            <div style={{ padding: '48px', textAlign: 'center', color: 'var(--muted)' }}>
              No accounts match your filter
            </div>
          )}
        </>
      )}

      {showAdd && (
        <AddAccountModal
          onClose={() => setShowAdd(false)}
          onAdded={() => { setShowAdd(false); load(); onAccountsChange?.(); toast.success('Account added'); }}
        />
      )}

      {showMcc && (
        <ImportMccModal
          onClose={() => setShowMcc(false)}
          existingIds={new Set(accounts.map(a => a.google_customer_id))}
          onImported={(count) => { setShowMcc(false); load(); onAccountsChange?.(); toast.success(`Imported ${count} account(s)`); }}
        />
      )}

      {applyItem && (
        <ApplyModal item={applyItem} onClose={() => setApplyItem(null)} onConfirm={handleConfirmApply} />
      )}
    </div>
  );
}
