import { useState, useEffect, useMemo } from 'react';
import { useNavigate } from 'react-router-dom';
import { Plus, CloudDownload, Play, Search, ChevronRight, ChevronDown, ArrowRight, AlertTriangle, Check, Activity } from 'lucide-react';
import axios from 'axios';
import { useToast } from '../components/Toast';

// ── Pacing math ──────────────────────────────────────────────────────────
function getDaysInfo() {
  const today       = new Date();
  // Spend data from Google Ads is through EOD of the prior day, not the current day.
  // Use yesterday's day number so ideal-spend and % DIFF calculations match the sheet.
  const daysIn      = Math.max(today.getDate() - 1, 1);
  const daysInMonth = new Date(today.getFullYear(), today.getMonth() + 1, 0).getDate();
  const dayOfMonth  = today.getDate();
  return { daysIn, daysInMonth, dayOfMonth, daysLeft: daysInMonth - daysIn };
}

function computePace(monthly, spend, daysIn, daysInMonth) {
  const idealSpend   = monthly > 0 ? monthly * (daysIn / daysInMonth) : 0;
  const deltaPct     = idealSpend > 0 ? ((spend / idealSpend) - 1) * 100 : 0;
  const absDelta     = Math.abs(deltaPct);
  const status       = absDelta > 10 ? 'over' : absDelta > 5 ? 'warn' : 'ok';
  const daysLeft     = daysInMonth - daysIn;
  const dailyCurrent = daysIn > 0 ? spend / daysIn : 0;
  // Divide by days remaining so the daily rate actually reaches monthly budget by EOM.
  const dailyRec     = daysLeft > 0 ? Math.max(0, monthly - spend) / daysLeft : 0;
  const pctOfBudget  = monthly > 0 ? (spend / monthly) * 100 : 0;
  const ratio        = idealSpend > 0 ? spend / idealSpend : 0;
  return { idealSpend, deltaPct, ratio, pacePct: pctOfBudget, status, daysLeft, dailyCurrent, dailyRec, pctOfBudget };
}

// ── Formatters ─────────────────────────────────────────────────────────────
function fmt0(n) {
  if (n == null || isNaN(n)) return '$0';
  return '$' + Math.round(n).toLocaleString('en-US');
}
function fmt2(n) {
  if (n == null || isNaN(n)) return '$0.00';
  return '$' + Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}
function fmtK(n) {
  if (n == null || isNaN(n)) return '$0';
  const abs = Math.abs(n);
  if (abs >= 10000) return '$' + Math.round(n / 1000).toLocaleString('en-US') + 'k';
  if (abs >= 1000)  return '$' + (n / 1000).toFixed(1).replace(/\.0$/, '') + 'k';
  return '$' + Math.round(n).toLocaleString('en-US');
}
function compactMoney(n, cents) {
  if (n == null || isNaN(n)) return cents ? '$0.00' : '$0';
  if (Math.abs(n) >= 1000) return fmtK(n);
  return cents ? fmt2(n) : fmt0(n);
}
function fmtPct(n) {
  if (n == null || isNaN(n)) return '0.0%';
  return (n > 0 ? '+' : '') + n.toFixed(1) + '%';
}
function timeAgo(isoStr) {
  if (!isoStr) return null;
  const diffMs = Date.now() - new Date(isoStr + 'Z').getTime();
  const mins = Math.round(diffMs / 60000);
  if (mins < 1)   return 'just now';
  if (mins < 60)  return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24)   return `${hrs}h ago`;
  const days = Math.round(hrs / 24);
  return `${days}d ago`;
}
function currentDaily(c) {
  return c.latest_pacing?.current_daily_budget ?? c.current_daily_budget ?? 0;
}
function campaignKey(c) {
  const digits = String(c.google_campaign_id || '').replace(/\D/g, '');
  return digits || `db:${c.id}`;
}
function normLabel(label) {
  return (label || 'Primary').trim().toLowerCase();
}
function uniqueCampaigns(campaigns) {
  const byKey = new Map();
  for (const c of campaigns || []) {
    const key = campaignKey(c);
    const prev = byKey.get(key);
    if (!prev || (!prev.budget_resource_name && c.budget_resource_name)) byKey.set(key, c);
  }
  return [...byKey.values()];
}

// Status label/class from a pace result. Under-pacing reads green (it's safe);
// only meaningful over-pace turns amber/red.
function paceInfo(pace) {
  const d   = pace.deltaPct;
  const cls = d > 10 ? 'over' : d > 5 ? 'warn' : 'ok';
  const pct = Math.abs(Math.round(d));
  const text = pct < 1 ? 'on pace' : `${pct}% ${d < 0 ? 'under' : 'over'}`;
  const arrow = pct < 1 ? '' : d < 0 ? '↗' : '↘';
  return { cls, text, arrow };
}

const APPLY_THRESHOLD = 1.0;

// Build the Account → Segment → Campaign table model for one account.
function buildAccountTable(account, daysIn, daysInMonth) {
  const all  = uniqueCampaigns(account.campaigns || []);
  const mostRecentDate = all.reduce((latest, c) => {
    const d = c.latest_pacing?.date;
    if (!d) return latest;
    return !latest || d > latest ? d : latest;
  }, null);
  const live = all.filter(c => !mostRecentDate || c.latest_pacing?.date === mostRecentDate);

  const groups = new Map();
  for (const c of live) {
    const key = normLabel(c.budget_label);
    if (!groups.has(key)) groups.set(key, { name: c.budget_label || 'Primary', campaigns: [] });
    groups.get(key).campaigns.push(c);
  }

  const segments = [...groups.values()].map(g => {
    const monthly      = g.campaigns.reduce((m, c) => Math.max(m, c.monthly_budget || 0), 0);
    const spend        = g.campaigns.reduce((s, c) => s + (c.latest_pacing?.actual_spend || 0), 0);
    const currentTotal = g.campaigns.reduce((s, c) => s + currentDaily(c), 0);
    const pace         = computePace(monthly, spend, daysIn, daysInMonth);

    const children = g.campaigns.map(c => {
      const share   = currentTotal > 0 ? currentDaily(c) / currentTotal : 1 / g.campaigns.length;
      const cMonthly = monthly * share;
      const cSpend   = c.latest_pacing?.actual_spend || 0;
      const cCurrent = currentDaily(c);
      const cRec     = pace.dailyRec * share;
      const cPace    = computePace(cMonthly, cSpend, daysIn, daysInMonth);
      const needsApply = Boolean(c.budget_resource_name) && c.is_active &&
                         Math.abs(cRec - cCurrent) > APPLY_THRESHOLD;
      return {
        campaign: c,
        name: c.campaign_name || c.name || 'Campaign',
        share, monthly: cMonthly, spend: cSpend,
        current: cCurrent, rec: cRec, pace: cPace, needsApply,
      };
    }).sort((a, b) => b.spend - a.spend);

    return {
      key: normLabel(g.name), name: g.name, monthly, spend,
      currentTotal, pace, children, campaignCount: g.campaigns.length,
    };
  }).sort((a, b) => b.spend - a.spend);

  const monthly = segments.reduce((s, x) => s + x.monthly, 0);
  const spend   = segments.reduce((s, x) => s + x.spend, 0);
  const pace    = computePace(monthly, spend, daysIn, daysInMonth);
  const hidden  = Math.max(all.length - live.length, 0);
  return { segments, monthly, spend, pace, hidden, totalCampaigns: live.length };
}

// ── Apply Modal ──────────────────────────────────────────────────────────
function ApplyModal({ item, onClose, onConfirm }) {
  if (!item) return null;
  const { daysIn, daysInMonth, daysLeft } = getDaysInfo();
  const pace = computePace(item.monthly, item.spend, daysIn, daysInMonth);
  const current   = item.currentDailyBudget ?? pace.dailyCurrent;
  const rec       = item.recOverride ?? pace.dailyRec;
  const diff      = Math.abs(rec - current);
  const direction = rec > current ? 'increase' : 'decrease';

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <h3>Apply recommended daily budget</h3>
        <div className="subtle">{item.name}</div>
        <div className="diff-card">
          <div className="dcol from">
            <div className="dk">Current daily</div>
            <div className="dv">{fmt2(current)}</div>
          </div>
          <div className="darrow"><ArrowRight size={14} /></div>
          <div className="dcol">
            <div className="dk">New daily</div>
            <div className="dv" style={{ color: 'var(--green)' }}>{fmt2(rec)}</div>
          </div>
        </div>
        <div className="mcopy">
          {direction === 'increase'
            ? `An increase of ${fmt2(diff)}/day to catch up to the monthly target.`
            : `A decrease of ${fmt2(diff)}/day to stay within the monthly target.`}{' '}
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

// ── Stat cards ─────────────────────────────────────────────────────────────
function StatCards({ accounts, tables, daysIn, daysInMonth, dayOfMonth, attention }) {
  const totals = useMemo(() => {
    let monthly = 0, spend = 0, ideal = 0, campaigns = 0, segments = 0;
    for (const a of accounts) {
      const t = tables.get(a.id);
      if (!t) continue;
      monthly   += t.monthly;
      spend     += t.spend;
      ideal     += t.pace.idealSpend;
      campaigns += t.totalCampaigns;
      segments  += t.segments.length;
    }
    const delta = ideal > 0 ? ((spend / ideal) - 1) * 100 : 0;
    const pctOfBudget = monthly > 0 ? (spend / monthly) * 100 : 0;
    return { monthly, spend, campaigns, segments, delta, pctOfBudget };
  }, [accounts, tables]);

  const paceWord = Math.abs(totals.delta) <= 5 ? 'pacing on track'
    : totals.delta > 5 ? 'pacing over budget' : 'pacing under budget';
  const paceCls  = Math.abs(totals.delta) <= 5 ? 'ok' : totals.delta > 5 ? 'over' : 'under';

  return (
    <div className="statcards">
      <div className="statcard">
        <div className="statcard-label">Monthly Budget</div>
        <div className="statcard-value">{fmtK(totals.monthly)}</div>
        <div className="statcard-sub">{totals.campaigns} campaigns · {accounts.length} accounts</div>
      </div>

      <div className="statcard">
        <div className="statcard-label">Spend (MTD)</div>
        <div className="statcard-value">{fmt2(totals.spend)}</div>
        <div className="statcard-bar">
          <div className="statcard-bar-fill" style={{ width: `${Math.min(totals.pctOfBudget, 100)}%` }} />
        </div>
        <div className="statcard-sub">
          {totals.pctOfBudget.toFixed(1)}% of budget · <span className={`pace-word ${paceCls}`}>{paceWord}</span>
        </div>
      </div>

      <div className="statcard">
        <div className="statcard-label">Tracked Units</div>
        <div className="statcard-value">{totals.campaigns} <span className="statcard-value-sub">/ {totals.segments} segments</span></div>
        <div className="statcard-sub">across {accounts.length} accounts</div>
      </div>

      <div className="statcard">
        <div className="statcard-label">Needs Attention</div>
        <div className={`statcard-value ${attention > 0 ? 'danger' : ''}`}>{attention}</div>
        <div className="statcard-sub">pending recommendations</div>
      </div>
    </div>
  );
}

// ── Account group (header + nested table) ────────────────────────────────────
const SEG_COLORS = ['#2563eb', '#7c3aed', '#0891b2', '#c2410c', '#15803d', '#be185d'];

function AccountGroup({ account, table, index, collapsed, onToggle, skipped, onSkip, onApplyOne, onApplyAll, navigate }) {
  const actionable = table.segments.reduce(
    (n, s) => n + s.children.filter(c => c.needsApply && !skipped.has(c.campaign.id)).length, 0);
  const accentColor = SEG_COLORS[index % SEG_COLORS.length];

  const headerRight = (
    <div className="acct-head-right">
      <div className="acct-metric"><span className="acct-metric-k">BUDGET</span><span className="acct-metric-v">{compactMoney(table.monthly, false)}</span></div>
      <div className="acct-metric"><span className="acct-metric-k">MTD</span><span className="acct-metric-v">{compactMoney(table.spend, true)}</span></div>
      <div className="acct-metric"><span className="acct-metric-k">ACTIONABLE</span><span className={`acct-metric-v ${actionable > 0 ? 'danger' : ''}`}>{actionable}</span></div>
      <button
        className="btn-applyall"
        disabled={actionable === 0}
        onClick={(e) => { e.stopPropagation(); onApplyAll(account, table); }}
      >
        <Check size={13} /> Apply all ({actionable})
      </button>
      <button className="btn-dash" onClick={(e) => { e.stopPropagation(); navigate(`/accounts/${account.id}`); }}>
        Dashboard <ArrowRight size={12} />
      </button>
      <button className="acct-chevron" onClick={(e) => { e.stopPropagation(); onToggle(account.id); }}>
        {collapsed ? <ChevronRight size={16} /> : <ChevronDown size={16} />}
      </button>
    </div>
  );

  return (
    <div className="acct-group">
      <div className="acct-group-head" onClick={() => navigate(`/accounts/${account.id}`)}>
        <span className="acct-bar" style={{ background: accentColor }} />
        <div className="acct-head-id">
          <div className="acct-name">{account.account_name}</div>
          <div className="acct-meta">
            Last run: <strong>{timeAgo(account.last_pacing_run_at) || 'never'}</strong>
            {table.hidden > 0 && <> · {table.hidden} hidden</>}
          </div>
        </div>
        {headerRight}
      </div>

      {!collapsed && (
        <table className="ac-table">
          <thead>
            <tr>
              <th className="col-name">CAMPAIGN / SEGMENT</th>
              <th>TYPE</th>
              <th className="num">BUDGET</th>
              <th className="num">MTD SPEND</th>
              <th className="num">PACE</th>
              <th className="num">CURRENT DAILY</th>
              <th className="num">REC. DAILY</th>
              <th>STATUS</th>
              <th className="col-action">ACTION</th>
            </tr>
          </thead>
          <tbody>
            {table.segments.map(seg => {
              const segInfo = paceInfo(seg.pace);
              const segActionable = seg.children.filter(c => c.needsApply && !skipped.has(c.campaign.id)).length;
              return (
                <SegmentRows
                  key={seg.key}
                  account={account}
                  seg={seg}
                  segInfo={segInfo}
                  segActionable={segActionable}
                  skipped={skipped}
                  onSkip={onSkip}
                  onApplyOne={onApplyOne}
                  navigate={navigate}
                />
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

function SegmentRows({ account, seg, segInfo, segActionable, skipped, onSkip, onApplyOne, navigate }) {
  return (
    <>
      <tr className="ac-row parent">
        <td className="col-name"><span className="parent-name">{seg.name}</span></td>
        <td><span className="mode-badge">SEG</span></td>
        <td className="num mono">{fmt0(seg.monthly)}/mo</td>
        <td className="num mono">{fmt2(seg.spend)}</td>
        <td className="num mono pace-val">{seg.pace.ratio.toFixed(2)}x</td>
        <td className="num mono dim">—</td>
        <td className="num mono dim">—</td>
        <td><span className={`pill ${segInfo.cls}`}>{segInfo.arrow} {segInfo.text}</span></td>
        <td className="col-action">
          <button className="btn-perset" onClick={() => navigate(`/accounts/${account.id}`)}>
            per campaign <ArrowRight size={11} />
          </button>
        </td>
      </tr>

      {seg.children.map(child => {
        const info     = paceInfo(child.pace);
        const isSkipped = skipped.has(child.campaign.id);
        const recDelta = child.current > 0 ? ((child.rec / child.current) - 1) * 100 : 0;
        return (
          <tr className="ac-row child" key={campaignKey(child.campaign)}>
            <td className="col-name">
              <span className="child-name"><span className="child-arrow">↳</span>{child.name}</span>
            </td>
            <td>
              <span className="share-badge">{Math.round(child.share * 100)}%</span>
              <span className="mode-badge sub">camp</span>
            </td>
            <td className="num mono">{fmt0(child.monthly)}/mo</td>
            <td className="num mono">{fmt2(child.spend)}</td>
            <td className="num mono pace-val">{child.pace.ratio.toFixed(2)}x</td>
            <td className="num mono">{child.current > 0 ? fmt2(child.current) : '—'}</td>
            <td className="num mono">
              {child.needsApply ? (
                <div className="rec-cell">
                  <span>{fmt2(child.rec)}</span>
                  {Math.abs(recDelta) >= 0.1 && (
                    <span className={`rec-delta ${recDelta >= 0 ? 'up' : 'down'}`}>
                      {recDelta >= 0 ? '↑' : '↓'} {Math.abs(recDelta).toFixed(1)}%
                    </span>
                  )}
                </div>
              ) : <span className="dim">{child.current > 0 ? fmt2(child.rec) : '—'}</span>}
            </td>
            <td><span className={`pill ${info.cls}`}>{info.arrow} {info.text}</span></td>
            <td className="col-action">
              {child.needsApply && !isSkipped ? (
                <div className="action-pair">
                  <button className="btn-apply" onClick={() => onApplyOne(account, child)}><Check size={12} /> Apply</button>
                  <button className="btn-skip" onClick={() => onSkip(child.campaign.id)}>Skip</button>
                </div>
              ) : isSkipped ? (
                <span className="action-done">skipped</span>
              ) : (
                <span className="action-done">— </span>
              )}
            </td>
          </tr>
        );
      })}
    </>
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
export default function Home({ onAccountsChange, onAccountSettingChange, accounts: propAccounts }) {
  const navigate       = useNavigate();
  const toast          = useToast();
  const { daysIn, daysInMonth, dayOfMonth } = getDaysInfo();

  const [accounts, setAccounts]   = useState(propAccounts || []);
  const [loading, setLoading]     = useState(!propAccounts?.length);
  const [runningAll, setRunningAll] = useState(false);
  const [syncPhase, setSyncPhase]   = useState(false);
  const [paceProgress, setPaceProgress] = useState({ completed: 0, total: 0 });
  const [showAdd, setShowAdd]     = useState(false);
  const [showMcc, setShowMcc]     = useState(false);
  const [applyItem, setApplyItem] = useState(null);
  const [q, setQ]                 = useState('');
  const [collapsed, setCollapsed] = useState(new Set());
  const [skipped, setSkipped]     = useState(new Set());
  const [oldestFirst, setOldestFirst] = useState(false);

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

  useEffect(() => { load(); }, []);

  // Build the table model once per accounts change.
  const tables = useMemo(() => {
    const m = new Map();
    for (const a of accounts) m.set(a.id, buildAccountTable(a, daysIn, daysInMonth));
    return m;
  }, [accounts, daysIn, daysInMonth]);

  const totalAttention = useMemo(() => {
    let n = 0;
    for (const a of accounts) {
      const t = tables.get(a.id);
      if (!t) continue;
      for (const s of t.segments) n += s.children.filter(c => c.needsApply && !skipped.has(c.campaign.id)).length;
    }
    return n;
  }, [accounts, tables, skipped]);

  const accountsWithAttention = useMemo(() => {
    let n = 0;
    for (const a of accounts) {
      const t = tables.get(a.id);
      if (!t) continue;
      const has = t.segments.some(s => s.children.some(c => c.needsApply && !skipped.has(c.campaign.id)));
      if (has) n++;
    }
    return n;
  }, [accounts, tables, skipped]);

  // Poll /run-all/status until pacing completes, then reload.
  const _pollPacingProgress = () => {
    let elapsed = 0;
    const MAX_WAIT_MS = 5 * 60 * 1000;
    const POLL_MS = 5000;
    const pollId = setInterval(async () => {
      elapsed += POLL_MS;
      try {
        const { data } = await axios.get('/api/pacing/run-all/status');
        if (data.total > 0) setPaceProgress({ completed: data.completed, total: data.total });
        if (!data.running) {
          clearInterval(pollId);
          setSyncPhase(false); setRunningAll(false);
          await load(); onAccountsChange?.();
          toast.success('Sync & pacing complete — data updated!');
        } else if (elapsed >= MAX_WAIT_MS) {
          clearInterval(pollId);
          setSyncPhase(false); setRunningAll(false);
          await load(); onAccountsChange?.();
          toast.warn('Pacing is taking longer than expected — refreshed anyway.');
        }
      } catch {
        clearInterval(pollId);
        setSyncPhase(false); setRunningAll(false);
        await load();
      }
    }, POLL_MS);
  };

  const runAllPacing = async () => {
    setRunningAll(true); setSyncPhase(true);
    setPaceProgress({ completed: 0, total: accounts.length });
    try {
      await axios.post('/api/accounts/sync-from-mcc', { skip_pacing: true });
    } catch (e) {
      if (e.response?.status !== 409) console.warn('Campaign sync skipped:', e.response?.data?.error || e.message);
    }
    await new Promise(resolve => setTimeout(resolve, 3000));
    setSyncPhase(false);
    try {
      await axios.post('/api/pacing/run-all');
      _pollPacingProgress();
    } catch (e) {
      if (e.response?.status === 409) {
        toast.warn('Pacing already in progress — dashboard will update when it finishes.');
        _pollPacingProgress();
      } else {
        toast.error(e.response?.data?.error || 'Pacing failed');
        setRunningAll(false);
      }
    }
  };

  const toggleCollapse = (id) => setCollapsed(s => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const skipCampaign   = (id) => setSkipped(s => new Set(s).add(id));

  // Push a set of {campaign, rec} adjustments to Google Ads for one account.
  const pushAdjustments = async (account, rows) => {
    const eligible = rows.filter(r => r.campaign.budget_resource_name && r.campaign.is_active);
    if (!eligible.length) {
      toast.warn('No active campaigns have a budget resource name yet — run pacing first.');
      return;
    }
    const adjustments = eligible.map(r => ({
      campaign_id:          r.campaign.id,
      budget_resource_name: r.campaign.budget_resource_name,
      new_daily_budget:     Math.round(r.rec * 100) / 100,
    }));
    toast.info(`Pushing ${eligible.length} budget(s) to Google Ads…`);
    try {
      const res = await axios.post(`/api/pacing/${account.id}/apply`, { adjustments });
      const { applied = [], errors = [] } = res.data;
      if (errors.length && !applied.length)      toast.error(`All ${errors.length} failed: ${errors[0]?.error || 'unknown error'}`);
      else if (errors.length)                    toast.error(`${applied.length} updated, ${errors.length} failed: ${errors[0]?.error || 'unknown error'}`);
      else                                       toast.success(res.data.message || 'Daily budgets updated in Google Ads');
      // Optimistic local update so Apply buttons clear immediately.
      const byId = new Map(adjustments.map(a => [a.campaign_id, a.new_daily_budget]));
      setAccounts(prev => prev.map(a => {
        if (a.id !== account.id) return a;
        return { ...a, campaigns: (a.campaigns || []).map(c => {
          const nb = byId.get(c.id);
          if (nb == null) return c;
          return { ...c, current_daily_budget: nb,
            latest_pacing: c.latest_pacing ? { ...c.latest_pacing, current_daily_budget: nb, recommended_daily_budget: nb, status: 'ON_PACE', change_percent: 0 } : c.latest_pacing };
        }) };
      }));
      load(); onAccountsChange?.();
    } catch (e) {
      toast.error(e.response?.data?.error || 'Failed to push to Google Ads');
    }
  };

  const handleApplyOne = (account, child) => pushAdjustments(account, [child]);
  const handleApplyAll = (account, table) => {
    const rows = [];
    for (const s of table.segments)
      for (const c of s.children)
        if (c.needsApply && !skipped.has(c.campaign.id)) rows.push(c);
    pushAdjustments(account, rows);
  };

  // Filter + order accounts.
  const visibleAccounts = useMemo(() => {
    let list = [...accounts];
    if (q.trim()) {
      const Q = q.trim().toLowerCase();
      list = list.filter(a => {
        if (a.account_name.toLowerCase().includes(Q)) return true;
        const t = tables.get(a.id);
        return t?.segments.some(s => s.name.toLowerCase().includes(Q) || s.children.some(c => c.name.toLowerCase().includes(Q)));
      });
    }
    list.sort((x, y) => {
      if (oldestFirst) {
        const xt = x.last_pacing_run_at ? new Date(x.last_pacing_run_at + 'Z').getTime() : 0;
        const yt = y.last_pacing_run_at ? new Date(y.last_pacing_run_at + 'Z').getTime() : 0;
        return xt - yt;
      }
      // Default: most actionable first.
      const xa = tables.get(x.id)?.segments.reduce((n, s) => n + s.children.filter(c => c.needsApply && !skipped.has(c.campaign.id)).length, 0) || 0;
      const ya = tables.get(y.id)?.segments.reduce((n, s) => n + s.children.filter(c => c.needsApply && !skipped.has(c.campaign.id)).length, 0) || 0;
      return ya - xa;
    });
    return list;
  }, [accounts, tables, q, oldestFirst, skipped]);

  return (
    <div className="allcamp">
      {/* Page header */}
      <div className="ac-header">
        <div>
          <h1 className="ac-title">All Campaigns</h1>
          <p className="ac-sub">Every tracked campaign across all accounts. Apply pacing recommendations or skip the ones you've already addressed.</p>
        </div>
        <div className="ac-header-actions">
          {runningAll && (
            <span className="pace-chip">
              <Activity size={13} />
              {syncPhase ? 'Syncing campaigns…'
                : paceProgress.total > 0 ? `Pacing ${paceProgress.completed}/${paceProgress.total}` : 'Starting…'}
            </span>
          )}
          <button className="btn ghost small" onClick={() => setShowMcc(true)}><CloudDownload size={13} /> Import MCC</button>
          <button className="btn primary small" onClick={runAllPacing} disabled={runningAll}><Play size={13} /> Sync & Pace All</button>
          <button className="btn small" onClick={() => setShowAdd(true)}><Plus size={13} /> Add Account</button>
        </div>
      </div>

      {/* Stat cards */}
      {!loading && accounts.length > 0 && (
        <StatCards accounts={accounts} tables={tables} daysIn={daysIn} daysInMonth={daysInMonth} dayOfMonth={dayOfMonth} attention={totalAttention} />
      )}

      {/* Recommendation banner */}
      {!loading && totalAttention > 0 && (
        <div className="rec-banner">
          <AlertTriangle size={16} className="rec-banner-icon" />
          <span className="rec-banner-text">
            <strong>{totalAttention}</strong> recommendation{totalAttention === 1 ? '' : 's'} across <strong>{accountsWithAttention}</strong> account{accountsWithAttention === 1 ? '' : 's'}. Newly-detected pace deviations from the latest run.
          </span>
          <button className={`rec-banner-btn ${oldestFirst ? 'active' : ''}`} onClick={() => setOldestFirst(v => !v)}>
            {oldestFirst ? 'Most actionable first' : 'Review oldest first'}
          </button>
        </div>
      )}

      {/* Search */}
      {!loading && accounts.length > 0 && (
        <div className="ac-search">
          <Search size={15} style={{ color: 'var(--muted)', flexShrink: 0 }} />
          <input placeholder="Search accounts, campaigns, or segments…" value={q} onChange={e => setQ(e.target.value)} />
        </div>
      )}

      {/* Account groups */}
      {loading ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 14 }}>
          {[1, 2, 3].map(i => <div key={i} className="bb-skeleton" style={{ height: 180, borderRadius: 'var(--r-lg)' }} />)}
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
        <div className="acct-groups">
          {visibleAccounts.map((account, i) => (
            <AccountGroup
              key={account.id}
              account={account}
              table={tables.get(account.id)}
              index={i}
              collapsed={collapsed.has(account.id)}
              onToggle={toggleCollapse}
              skipped={skipped}
              onSkip={skipCampaign}
              onApplyOne={handleApplyOne}
              onApplyAll={handleApplyAll}
              navigate={navigate}
            />
          ))}
          {visibleAccounts.length === 0 && (
            <div style={{ padding: '48px', textAlign: 'center', color: 'var(--muted)' }}>No accounts match your search</div>
          )}
        </div>
      )}

      {showAdd && (
        <AddAccountModal onClose={() => setShowAdd(false)} onAdded={() => { setShowAdd(false); load(); onAccountsChange?.(); toast.success('Account added'); }} />
      )}
      {showMcc && (
        <ImportMccModal onClose={() => setShowMcc(false)} existingIds={new Set(accounts.map(a => a.google_customer_id))} onImported={(count) => { setShowMcc(false); load(); onAccountsChange?.(); toast.success(`Imported ${count} account(s)`); }} />
      )}
      {applyItem && (
        <ApplyModal item={applyItem} onClose={() => setApplyItem(null)} onConfirm={() => setApplyItem(null)} />
      )}
    </div>
  );
}
