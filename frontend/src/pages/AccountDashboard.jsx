import { useState, useEffect, useCallback } from 'react';
import { useParams, useNavigate, Link } from 'react-router-dom';
import { Play, Settings, History, Download, Plus, ArrowLeft, ArrowRight, Zap } from 'lucide-react';
import axios from 'axios';
import { useToast } from '../components/Toast';

// ── Pacing math ──────────────────────────────────────────────────────────
function getDaysInfo() {
  const today       = new Date();
  // Spend data from Google Ads is through EOD of the prior day, not the current day.
  // Use yesterday's day number so ideal-spend and % DIFF calculations match the sheet.
  const daysIn      = Math.max(today.getDate() - 1, 1);
  const daysInMonth = new Date(today.getFullYear(), today.getMonth() + 1, 0).getDate();
  return { daysIn, daysInMonth, daysLeft: daysInMonth - daysIn };
}

function computePace(monthly, spend, daysIn, daysInMonth) {
  const idealSpend   = monthly > 0 ? monthly * (daysIn / daysInMonth) : 0;
  const deltaPct     = idealSpend > 0 ? ((spend / idealSpend) - 1) * 100 : 0;
  const absDelta     = Math.abs(deltaPct);
  const status       = absDelta > 10 ? 'over' : absDelta > 5 ? 'warn' : 'ok';
  const daysLeft     = daysInMonth - daysIn;
  const dailyCurrent = daysIn > 0 ? spend / daysIn : 0;
  // Matches the Google Sheet formula: (Budget - Spend) / days_in_month
  const dailyRec     = daysInMonth > 0 ? Math.max(0, monthly - spend) / daysInMonth : 0;
  const pctOfBudget  = monthly > 0 ? (spend / monthly) * 100 : 0;
  return { idealSpend, deltaPct, pacePct: pctOfBudget, status, daysLeft, dailyCurrent, dailyRec, pctOfBudget };
}

function fmt(n) { return '$' + Math.round(n || 0).toLocaleString('en-US'); }
function fmtPct(n) { return (n > 0 ? '+' : '') + (n || 0).toFixed(1) + '%'; }
function fmtPlainPct(n) { return (n || 0).toFixed(1) + '%'; }
function currentDaily(c) {
  return c.latest_pacing?.current_daily_budget ?? c.current_daily_budget ?? 0;
}
function campaignKey(c) {
  const digits = String(c.google_campaign_id || '').replace(/\D/g, '');
  return digits || `db:${c.id}`;
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

function allocateByCurrentShare(campaigns, totalDaily) {
  const eligible = uniqueCampaigns(campaigns).filter(c => c.budget_resource_name);
  if (!eligible.length) return [];
  const totalCurrent = eligible.reduce((s, c) => s + currentDaily(c), 0);
  const even = Math.round((totalDaily / eligible.length) * 100) / 100;
  return eligible.map(c => ({
    campaign_id:          c.id,
    budget_resource_name: c.budget_resource_name,
    new_daily_budget:     totalCurrent > 0
      ? Math.round((totalDaily * (currentDaily(c) / totalCurrent)) * 100) / 100
      : even,
  }));
}

function getSegments(account, campaigns) {
  if (Array.isArray(account?.segment_summaries) && account.segment_summaries.length) {
    return account.segment_summaries.map(s => ({
      name: s.name,
      monthly: s.monthly || 0,
      spend: s.spend || 0,
      currentDaily: s.current_daily || 0,
      campaignCount: s.campaign_count || 0,
    }));
  }
  if (!campaigns.length) return [];
  // Only use campaigns from the most recent pacing run. Campaigns with older
  // pacing dates are stale (from pre-fix runs or campaigns no longer spending)
  // and would inflate the total.
  const mostRecentDate = campaigns.reduce((latest, c) => {
    const d = c.latest_pacing?.date;
    if (!d) return latest;
    return !latest || d > latest ? d : latest;
  }, null);
  const map = {};
  const seenGids = new Set();
  for (const c of uniqueCampaigns(campaigns)) {
    const label = c.budget_label || 'Primary';
    if (!map[label]) map[label] = { name: label, monthly: 0, spend: 0, currentDaily: 0, campaignCount: 0 };
    // Use max so an inactive campaign (monthly_budget=0) doesn't hide the
    // correct budget that an active campaign in the same segment carries.
    map[label].monthly = Math.max(map[label].monthly, c.monthly_budget || 0);
    // Only add spend once per unique Google campaign ID.
    if (
      (!mostRecentDate || c.latest_pacing?.date === mostRecentDate) &&
      !seenGids.has(campaignKey(c))
    ) {
      seenGids.add(campaignKey(c));
      map[label].spend += c.latest_pacing?.actual_spend || 0;
    }
    map[label].currentDaily += currentDaily(c);
    map[label].campaignCount += 1;
  }
  return Object.values(map);
}

// ── Switch ───────────────────────────────────────────────────────────────
function Switch({ on, onChange, label }) {
  return (
    <div className="switch-wrap">
      {label && <span className="switch-label-text">{label}</span>}
      <label className="switch">
        <input type="checkbox" checked={on} onChange={e => onChange(e.target.checked)} />
        <span className="switch-track" />
        <span className="switch-knob" />
      </label>
    </div>
  );
}

// ── Chart ─────────────────────────────────────────────────────────────────
function buildCum(spend, daysIn, accountId) {
  const cum = [];
  let r = 0;
  for (let i = 0; i < daysIn; i++) {
    const baseDaily = spend / Math.max(daysIn, 1);
    const noise = Math.sin(i * 1.3 + (accountId || 1)) * 0.18 + (i % 3 === 0 ? 0.08 : -0.04);
    r += baseDaily * (1 + noise);
    cum.push(r);
  }
  const last = cum[cum.length - 1] || 1;
  return cum.map(v => v * (spend / last));
}

function CumulativeLineChart({ monthly, spend, daysIn, daysInMonth, accountId }) {
  const pace = computePace(monthly, spend, daysIn, daysInMonth);
  const cum  = buildCum(spend, daysIn, accountId);
  const W = 720, H = 280;
  const padL = 56, padR = 16, padT = 22, padB = 28;
  const innerW = W - padL - padR;
  const innerH = H - padT - padB;
  const DIM = daysInMonth;
  const yMax = Math.max(monthly * 1.1, spend * (DIM / Math.max(daysIn, 1)) * 1.06, 1);
  const xFn = (d) => padL + (d / (DIM - 1)) * innerW;
  const yFn = (v) => padT + innerH - (v / yMax) * innerH;

  const smoothPath = (pts) => {
    if (!pts.length) return '';
    if (pts.length === 1) return `M ${pts[0].x} ${pts[0].y}`;
    let p = `M ${pts[0].x} ${pts[0].y}`;
    for (let i = 1; i < pts.length; i++) {
      const cp1x = pts[i-1].x + (pts[i].x - pts[i-1].x) * 0.5;
      const cp2x = pts[i-1].x + (pts[i].x - pts[i-1].x) * 0.5;
      p += ` C ${cp1x} ${pts[i-1].y}, ${cp2x} ${pts[i].y}, ${pts[i].x} ${pts[i].y}`;
    }
    return p;
  };

  const actualPts  = cum.map((v, i) => ({ x: xFn(i), y: yFn(v) }));
  const actualPath = smoothPath(actualPts);
  const areaPath   = actualPath + ` L ${xFn(daysIn - 1)} ${yFn(0)} L ${xFn(0)} ${yFn(0)} Z`;
  const projTotal  = pace.dailyCurrent * DIM;
  const lastCum    = cum[cum.length - 1] || 0;
  const gradId     = `ag${accountId}`;
  const dayTicks   = [0, 4, 9, 14, 19, 24, DIM - 1];

  return (
    <div>
      <div className="chart-legend">
        <span className="it"><span className="sw" style={{ background: 'var(--ink)' }} />Ideal pace</span>
        <span className="it"><span className="sw" style={{ background: '#2563eb' }} />Actual MTD</span>
        <span className="it"><span className="sw dash" style={{ borderColor: 'var(--red)' }} />Projection at current rate</span>
        <span className="it"><span className="sw dash" style={{ borderColor: 'var(--green)' }} />If recommended applied</span>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: H, display: 'block' }}>
        <defs>
          <linearGradient id={gradId} x1="0" x2="0" y1="0" y2="1">
            <stop offset="0%" stopColor="#2563eb" stopOpacity="0.22" />
            <stop offset="100%" stopColor="#2563eb" stopOpacity="0" />
          </linearGradient>
        </defs>
        {[0, 0.25, 0.5, 0.75, 1].map((t, i) => {
          const v = t * yMax;
          return (
            <g key={i}>
              <line x1={padL} y1={yFn(v)} x2={W - padR} y2={yFn(v)} stroke="var(--line)" strokeWidth="1" />
              <text x={padL - 8} y={yFn(v) + 3} fontSize="10.5" textAnchor="end" fill="var(--muted)" fontFamily="Inter">
                {v >= 1000 ? '$' + Math.round(v / 1000) + 'k' : '$' + Math.round(v)}
              </text>
            </g>
          );
        })}
        <line x1={padL} y1={padT + innerH} x2={W - padR} y2={padT + innerH} stroke="var(--line-2)" strokeWidth="1" />
        {dayTicks.map(d => (
          <text key={d} x={xFn(d)} y={H - padB + 16} fontSize="10.5" textAnchor="middle" fill="var(--muted)" fontFamily="Inter">{d + 1}</text>
        ))}
        <line x1={padL} y1={yFn(monthly)} x2={W - padR} y2={yFn(monthly)} stroke="var(--ink)" strokeWidth="1" strokeDasharray="4 4" opacity="0.6" />
        <text x={W - padR} y={yFn(monthly) - 5} fontSize="10.5" textAnchor="end" fill="var(--ink-2)" fontFamily="Inter" fontWeight="500">Monthly cap · {fmt(monthly)}</text>
        <line x1={xFn(daysIn - 1)} y1={padT} x2={xFn(daysIn - 1)} y2={padT + innerH} stroke="var(--ink)" strokeWidth="1" strokeDasharray="3 3" opacity="0.35" />
        <text x={xFn(daysIn - 1) + 6} y={padT + 12} fontSize="10.5" fill="var(--ink-2)" fontFamily="Inter" fontWeight="500">Today · d{daysIn}</text>
        <path d={`M ${xFn(0)} ${yFn(0)} L ${xFn(DIM - 1)} ${yFn(monthly)}`} stroke="var(--ink)" strokeWidth="1.6" fill="none" />
        <path d={areaPath} fill={`url(#${gradId})`} />
        <path d={`M ${xFn(daysIn - 1)} ${yFn(lastCum)} L ${xFn(DIM - 1)} ${yFn(monthly)}`} stroke="var(--green)" strokeWidth="2" fill="none" strokeDasharray="5 4" strokeLinecap="round" />
        <circle cx={xFn(DIM - 1)} cy={yFn(monthly)} r="3.5" fill="var(--green)" stroke="white" strokeWidth="1.5" />
        <path d={`M ${xFn(daysIn - 1)} ${yFn(lastCum)} L ${xFn(DIM - 1)} ${yFn(projTotal)}`} stroke="var(--red)" strokeWidth="2" fill="none" strokeDasharray="5 4" strokeLinecap="round" />
        <circle cx={xFn(DIM - 1)} cy={yFn(projTotal)} r="3.5" fill="var(--red)" stroke="white" strokeWidth="1.5" />
        <path d={actualPath} stroke="#2563eb" strokeWidth="2.4" fill="none" strokeLinecap="round" strokeLinejoin="round" />
        {cum.length > 0 && <circle cx={xFn(daysIn - 1)} cy={yFn(lastCum)} r="4.5" fill="white" stroke="#2563eb" strokeWidth="2.4" />}
        {projTotal !== monthly && (
          <g>
            <rect x={xFn(DIM - 1) - 90} y={yFn(projTotal) + (projTotal > monthly ? -28 : 4)} width="86" height="20" rx="4" fill="var(--red-bg)" stroke="var(--red-line)" strokeWidth="1" />
            <text x={xFn(DIM - 1) - 47} y={yFn(projTotal) + (projTotal > monthly ? -14 : 18)} fontSize="11" textAnchor="middle" fill="var(--red)" fontFamily="Inter" fontWeight="600">Proj {fmt(projTotal)}</text>
          </g>
        )}
      </svg>
    </div>
  );
}

// ── Apply Modal ───────────────────────────────────────────────────────────
function ApplyModal({ item, onClose, onConfirm }) {
  if (!item) return null;
  const { daysIn, daysInMonth, daysLeft } = getDaysInfo();
  if (item.bulk) {
    return (
      <div className="modal-backdrop" onClick={onClose}>
        <div className="modal modal-wide" onClick={e => e.stopPropagation()}>
          <h3>Apply all recommended daily budgets</h3>
          <div className="subtle">{item.accountName} · {item.segments.length} segments</div>
          <table className="modal-seg-table">
            <thead><tr><th>Segment</th><th>Now</th><th>New daily</th></tr></thead>
            <tbody>
              {item.segments.map(s => {
                const sp = computePace(s.monthly, s.spend, daysIn, daysInMonth);
                return <tr key={s.name}><td>{s.name}</td><td>{fmt(s.currentDaily ?? sp.dailyCurrent)}</td><td className="new-daily">{fmt(sp.dailyRec)}</td></tr>;
              })}
            </tbody>
          </table>
          <div className="mcopy">Calculated over the remaining {daysLeft} days of the month.</div>
          <div className="footer-row">
            <button className="btn ghost" onClick={onClose}>Cancel</button>
            <button className="btn primary" onClick={() => onConfirm(item)}>Push {item.segments.length} updates to Google Ads</button>
          </div>
        </div>
      </div>
    );
  }
  const pace = computePace(item.monthly, item.spend, daysIn, daysInMonth);
  // Use backend-computed rec if passed (item.rec), otherwise fall back to frontend calc.
  const newDaily = item.rec != null ? item.rec : pace.dailyRec;
  const diff = Math.abs(newDaily - pace.dailyCurrent);
  const dir  = newDaily > pace.dailyCurrent ? 'increase' : 'decrease';
  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={e => e.stopPropagation()}>
        <h3>Apply recommended daily budget</h3>
        <div className="subtle">{item.segmentOf ? `${item.name} · segment of ${item.segmentOf}` : item.name}</div>
        <div className="diff-card">
          <div className="dcol from"><div className="dk">Current daily</div><div className="dv">{fmt(pace.dailyCurrent)}</div></div>
          <div className="darrow"><ArrowRight size={14} /></div>
          <div className="dcol"><div className="dk">New daily</div><div className="dv" style={{ color: 'var(--green)' }}>{fmt(newDaily)}</div></div>
        </div>
        <div className="mcopy">{dir === 'increase' ? `An increase of ${fmt(diff)}/day to catch up.` : `A decrease of ${fmt(diff)}/day to stay within the monthly target.`} Calculated over the remaining {daysLeft} days.</div>
        <div className="footer-row">
          <button className="btn ghost" onClick={onClose}>Cancel</button>
          <button className="btn primary" onClick={() => onConfirm(item)}>Push to Google Ads</button>
        </div>
      </div>
    </div>
  );
}

// ── Import campaigns modal (preserved) ───────────────────────────────────
function ImportCampaignsModal({ account, onClose, onImported }) {
  const [live, setLive]         = useState([]);
  const [sel, setSel]           = useState(new Set());
  const [loading, setLoading]   = useState(true);
  const [importing, setImporting] = useState(false);
  const [error, setError]       = useState('');
  const toast = useToast();

  useEffect(() => {
    axios.get(`/api/accounts/${account.id}/sync-campaigns`)
      .then(r => setLive(r.data.campaigns || []))
      .catch(e => setError(e.response?.data?.error || 'Failed'))
      .finally(() => setLoading(false));
  }, [account.id]);

  const toggle = (id) => setSel(s => { const n = new Set(s); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const doImport = async () => {
    if (!sel.size) return;
    setImporting(true);
    try {
      const r = await axios.post(`/api/accounts/${account.id}/sync-campaigns`, { campaign_ids: [...sel] });
      toast.success(r.data.message); onImported();
    } catch (e) { setError(e.response?.data?.error || 'Import failed'); }
    finally { setImporting(false); }
  };

  return (
    <div className="bb-modal-overlay" onClick={onClose}>
      <div className="bb-modal" style={{ maxWidth: 580 }} onClick={e => e.stopPropagation()}>
        <div className="bb-modal-header">
          <span className="bb-section-title">Import Campaigns from Google Ads</span>
          <button className="bb-btn bb-btn-ghost" onClick={onClose}>✕</button>
        </div>
        {error && <div className="bb-alert bb-alert-error">{error}</div>}
        {loading ? <p style={{ color: 'var(--muted)' }}>Loading campaigns…</p> : (
          <>
            <div style={{ maxHeight: 280, overflowY: 'auto', border: '1px solid var(--line)', borderRadius: 'var(--r)' }}>
              {live.map(c => (
                <label key={c.campaign_id} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '9px 13px', borderBottom: '1px solid var(--line)', cursor: 'pointer' }}>
                  <input type="checkbox" checked={sel.has(c.campaign_id)} onChange={() => toggle(c.campaign_id)} />
                  <span style={{ flex: 1 }}>{c.campaign_name}</span>
                  <span style={{ fontSize: 'var(--t-xs)', color: 'var(--muted)' }}>{c.status}</span>
                </label>
              ))}
            </div>
            <div className="bb-row-between" style={{ marginTop: 14 }}>
              <span style={{ fontSize: 'var(--t-sm)', color: 'var(--muted)' }}>{sel.size} selected</span>
              <div className="bb-row" style={{ gap: 8 }}>
                <button className="bb-btn bb-btn-secondary" onClick={onClose}>Cancel</button>
                <button className="bb-btn bb-btn-primary" onClick={doImport} disabled={!sel.size || importing}>{importing ? 'Importing…' : `Import ${sel.size}`}</button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

// ── Main export ───────────────────────────────────────────────────────────
export default function AccountDashboard({ onPacingComplete }) {
  const { id }    = useParams();
  const navigate  = useNavigate();
  const toast     = useToast();
  const { daysIn, daysInMonth } = getDaysInfo();

  const [account, setAccount]       = useState(null);
  const [campaigns, setCampaigns]   = useState([]);
  const [recommendations, setRecs]  = useState([]);
  const [loading, setLoading]       = useState(true);
  const [running, setRunning]       = useState(false);
  const [applying, setApplying]     = useState(false);
  const [settingUp, setSettingUp]   = useState(false);
  const [setupStatus, setSetupStatus] = useState('');
  const [capOn, setCapOn]           = useState(false);
  const [showImport, setShowImport] = useState(false);
  const [applyItem, setApplyItem]   = useState(null);
  const [lastSync, setLastSync]     = useState(null);
  const [sheetSync, setSheetSync]   = useState(null);
  const [sheetWrite, setSheetWrite] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [accR, campR] = await Promise.all([
        axios.get(`/api/accounts/${id}/summary`),
        axios.get(`/api/campaigns/account/${id}`),
      ]);
      setAccount(accR.data.account);
      setCampaigns(campR.data.campaigns || []);
      setCapOn(accR.data.account?.settings?.auto_pause_enabled || false);
    } catch { toast.error('Failed to load account'); }
    finally { setLoading(false); }
  }, [id]);

  useEffect(() => { load(); }, [load]);

  const mergeRecs = (recs) => {
    const byId = new Map((recs || []).map(r => [r.campaign_id, r]));
    setCampaigns(prev => prev.map(c => {
      const rec = byId.get(c.id);
      if (!rec) return c;
      return { ...c, monthly_budget: rec.monthly_budget ?? c.monthly_budget, latest_pacing: { ...(c.latest_pacing || {}), date: rec.date, actual_spend: rec.actual_spend, pace_ratio: rec.pace_ratio, current_daily_budget: rec.current_daily_budget, recommended_daily_budget: rec.recommended_daily_budget, change_percent: rec.change_percent, status: rec.status } };
    }));
  };

  const runPacing = async () => {
    setRunning(true); setRecs([]);
    try {
      const r = await axios.post(`/api/pacing/${id}/run`);
      const recs = r.data.recommendations || [];
      setRecs(recs); setSheetSync(r.data.sheet_sync); setSheetWrite(r.data.sheet_write); setLastSync(new Date());
      setAccount(prev => prev ? {
        ...prev,
        total_monthly_budget: r.data.total_monthly_budget ?? prev.total_monthly_budget,
        mtd_spend: r.data.mtd_spend ?? prev.mtd_spend,
        segment_summaries: r.data.segment_summaries ?? prev.segment_summaries,
      } : prev);
      mergeRecs(recs);
      if (r.data.sheet_sync?.updated_count > 0) toast.info(`Pulled ${r.data.sheet_sync.updated_count} budget(s) from Google Sheet`);
      if (r.data.sheet_sync?.warning) toast.warn(r.data.sheet_sync.warning);
      if (r.data.sheet_write?.written_count > 0) toast.info(`Wrote spend to ${r.data.sheet_write.written_count} Sheet row(s)`);
      if (r.data.auto_pause_warning) toast.warn(r.data.auto_pause_warning.message);
      toast.success('Pacing run complete');
      onPacingComplete?.();
    } catch (e) { toast.error(e.response?.data?.error || 'Pacing run failed'); }
    finally { setRunning(false); }
  };

  const quickSetup = async () => {
    setSettingUp(true); setSetupStatus('Fetching campaigns…');
    try {
      const liveR = await axios.get(`/api/accounts/${id}/sync-campaigns`);
      const live  = liveR.data.campaigns || [];
      if (!live.length) { toast.warn('No campaigns found'); return; }
      setSetupStatus(`Importing ${live.length} campaign(s)…`);
      await axios.post(`/api/accounts/${id}/sync-campaigns`, { campaign_ids: live.map(c => c.campaign_id) });
      setSetupStatus('Running pacing…');
      const pacingR = await axios.post(`/api/pacing/${id}/run`);
      const recs = pacingR.data.recommendations || [];
      setRecs(recs); setLastSync(new Date()); mergeRecs(recs);
      if (pacingR.data.sheet_sync?.warning) toast.warn(pacingR.data.sheet_sync.warning);
      toast.success(`Set up ${live.length} campaign(s) and ran pacing`);
      load(); onPacingComplete?.();
    } catch (e) { toast.error(e.response?.data?.error || 'Setup failed'); }
    finally { setSettingUp(false); setSetupStatus(''); }
  };

  const handleConfirmApply = async (item) => {
    setApplyItem(null);
    setApplying(true);
    try {
      if (item.bulk) {
        // Bulk apply: backend recs already preserve each campaign's current
        // daily-budget ratio. If no run is loaded, preserve ratios in the UI.
        const { daysIn: di, daysInMonth: dim } = getDaysInfo();
        const adjustments = recommendations.length
          ? recommendations.map(r => ({
              campaign_id:          r.campaign_id,
              budget_resource_name: r.budget_resource_name,
              new_daily_budget:     r.recommended_daily_budget,
            }))
          : item.segments.flatMap(s => {
              const sp = computePace(s.monthly, s.spend, di, dim);
              const segCampaigns = campaigns.filter(c => (c.budget_label || 'Primary') === s.name);
              return allocateByCurrentShare(segCampaigns, sp.dailyRec);
            });
        if (!adjustments.length) {
          toast.warn('Run pacing first to generate recommendations, then apply.');
          return;
        }
        const r = await axios.post(`/api/pacing/${id}/apply`, { adjustments });
        toast.success(r.data.message);
      } else {
        // Single-budget or per-segment apply.
        // item.segmentOf is set when this is a per-segment row; item.name is the segment label.
        // For single-budget accounts item.segmentOf is undefined — use all campaigns.
        const segLabel = item.segmentOf ? item.name : null;
        const eligible = uniqueCampaigns(campaigns).filter(c =>
          c.budget_resource_name &&
          (segLabel === null || (c.budget_label || 'Primary') === segLabel)
        );

        if (!eligible.length) {
          toast.warn('No campaigns have a budget resource name — run pacing first to populate them.');
          return;
        }

        // Use each campaign's backend-stored recommended daily budget (already
        // ratio-preserving). Fall back to preserving current daily-budget shares.
        const hasPacingData = eligible.some(c => c.latest_pacing?.recommended_daily_budget != null);
        const { daysIn: di, daysInMonth: dim } = getDaysInfo();
        const fallbackRec = computePace(item.monthly, item.spend, di, dim).dailyRec;
        const fallbackById = new Map(
          allocateByCurrentShare(eligible, fallbackRec).map(a => [a.campaign_id, a.new_daily_budget])
        );
        const adjustments = eligible.map(c => ({
          campaign_id:          c.id,
          budget_resource_name: c.budget_resource_name,
          new_daily_budget:     hasPacingData
            ? (c.latest_pacing?.recommended_daily_budget ?? fallbackById.get(c.id) ?? 0)
            : (fallbackById.get(c.id) ?? 0),
        }));

        const r = await axios.post(`/api/pacing/${id}/apply`, { adjustments });
        toast.success(r.data.message || `${eligible.length} budget(s) updated in Google Ads`);
      }
      load();
    } catch (e) {
      toast.error(e.response?.data?.error || 'Apply failed');
    } finally {
      setApplying(false);
    }
  };

  const toggleCap = async (v) => {
    setCapOn(v);
    try { await axios.put(`/api/settings/${id}`, { auto_pause_enabled: v }); }
    catch { toast.error('Failed to save cap setting'); setCapOn(!v); }
  };

  if (loading) {
    return (
      <div>
        <div className="bb-skeleton" style={{ height: 44, marginBottom: 20, borderRadius: 'var(--r)' }} />
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5,1fr)', gap: 10, marginBottom: 16 }}>
          {[1,2,3,4,5].map(i => <div key={i} className="bb-skeleton" style={{ height: 82, borderRadius: 'var(--r)' }} />)}
        </div>
        <div className="bb-skeleton" style={{ height: 340, borderRadius: 'var(--r)' }} />
      </div>
    );
  }

  if (!account) return <div className="bb-alert bb-alert-error">Account not found</div>;

  const segments    = getSegments(account, campaigns);
  const isSegmented = segments.length > 1;
  const segBudgets  = {};
  for (const c of uniqueCampaigns(campaigns)) {
    const l = c.budget_label || 'Primary';
    segBudgets[l] = Math.max(segBudgets[l] || 0, c.monthly_budget || 0);
  }
  const monthly = typeof account.total_monthly_budget === 'number'
    ? account.total_monthly_budget
    : Object.values(segBudgets).reduce((s, b) => s + b, 0);
  // Only sum spend from the most recent pacing run to avoid stale campaigns
  // (from old/buggy runs) inflating the total. Also dedup by google_campaign_id.
  const _mostRecentDate = campaigns.reduce((latest, c) => {
    const d = c.latest_pacing?.date;
    if (!d) return latest;
    return !latest || d > latest ? d : latest;
  }, null);
  const _spendSeenGids = new Set();
  const spend = typeof account.mtd_spend === 'number'
    ? account.mtd_spend
    : campaigns.reduce((s, c) => {
      if (_mostRecentDate && c.latest_pacing?.date !== _mostRecentDate) return s;
      const key = campaignKey(c);
      if (_spendSeenGids.has(key)) return s;
      _spendSeenGids.add(key);
      return s + (c.latest_pacing?.actual_spend || 0);
    }, 0);
  const pace    = computePace(monthly, spend, daysIn, daysInMonth);
  const currentDailyTotal = uniqueCampaigns(campaigns).reduce((s, c) => s + currentDaily(c), 0);

  // Always compute recommended daily fresh from the current budget and spend.
  // Relying on recFromBackend (stored DB value) causes stale recommendations to
  // persist across runs — if a previous run stored a wrong value (e.g. from a
  // formula bug or duplicate-spend issue), that positive value would override the
  // correct live formula.  The formula is: max(0, monthly - spend) / daysInMonth,
  // which is identical to what the Google Sheet uses.
  const displayRec = pace.dailyRec;
  const hasSheetId = Boolean(account.settings?.google_sheet_id);
  const lastSyncStr = lastSync ? (() => { const mins = Math.round((Date.now() - lastSync) / 60000); return mins < 1 ? 'just now' : `${mins}m ago`; })() : 'not yet this session';

  return (
    <div>
      {!hasSheetId && <div className="bb-alert bb-alert-warn" style={{ marginBottom: 12 }}><strong>Google Sheet not configured</strong> — budgets will stay $0 until you add a Sheet ID in <a href={`/accounts/${id}/settings`} style={{ color: 'var(--amber)' }}>Settings</a>.</div>}
      {sheetSync?.error   && <div className="bb-alert bb-alert-warn" style={{ marginBottom: 12 }}>Sheet sync warning: {sheetSync.error}</div>}
      {sheetSync?.warning && <div className="bb-alert bb-alert-warn" style={{ marginBottom: 12 }}>Sheet sync warning: {sheetSync.warning}</div>}
      {sheetWrite?.error  && <div className="bb-alert bb-alert-warn" style={{ marginBottom: 12 }}>Sheet writeback: {sheetWrite.error}</div>}

      {/* Detail head */}
      <div className="detail-head">
        <div>
          <Link to="/" style={{ display: 'inline-flex', alignItems: 'center', gap: 5, fontSize: 'var(--t-sm)', color: 'var(--muted)', textDecoration: 'none', marginBottom: 8 }}>
            <ArrowLeft size={12} /> Back to dashboard
          </Link>
          <div className="dtitle">{account.account_name}</div>
          <div className="ddesc">
            {isSegmented ? `${segments.length} segments` : 'single budget'}
            <span style={{ color: 'var(--line-2)' }}>·</span>
            monthly {fmt(monthly)}
            <span style={{ color: 'var(--line-2)' }}>·</span>
            day {daysIn} of {daysInMonth}
            <span style={{ color: 'var(--line-2)' }}>·</span>
            <span className={`pill ${pace.status}`}>{fmtPct(pace.deltaPct)}</span>
          </div>
        </div>
        <div className="dactions">
          <button className="btn small" onClick={() => navigate(`/accounts/${id}/settings`)}><Settings size={13} /> Settings</button>
          <button className="btn small" onClick={() => navigate(`/accounts/${id}/history`)}><History size={13} /> History</button>
          <button className="btn small" onClick={() => navigate(`/accounts/${id}/leads`)}><Download size={13} /> Leads</button>
          <button className="btn small" onClick={runPacing} disabled={running}><Play size={13} /> {running ? 'Running…' : 'Run Pacing'}</button>
          {isSegmented
            ? <button className="btn primary" onClick={() => setApplyItem({ bulk: true, accountName: account.account_name, segments })} disabled={applying}>{applying ? 'Applying…' : 'Apply all recommended'}</button>
            : <button className="btn primary" onClick={() => setApplyItem({ name: account.account_name, monthly, spend, rec: displayRec })}>Set daily to {fmt(displayRec)}</button>}
        </div>
      </div>

      {/* Stat grid */}
      <div className="statgrid">
        <div className="s"><div className="sk">MTD Spend</div><div className="sv">{fmt(spend)}</div><div className="ssub">{pace.pctOfBudget.toFixed(0)}% of monthly · thru d{daysIn}</div></div>
        <div className="s"><div className="sk">Monthly Budget</div><div className="sv">{fmt(monthly)}</div><div className="ssub">{fmt(monthly - spend)} remaining</div></div>
        <div className="s"><div className="sk">Daily — Current</div><div className="sv">{fmt(currentDailyTotal)}</div><div className="ssub">live Google Ads budgets</div></div>
        <div className="s featured"><div className="sk">Daily — Recommended</div><div className="sv">{fmt(displayRec)}</div><div className="ssub accent">over {pace.daysLeft} remaining days</div></div>
        <div className="s"><div className="sk">Pace</div><div className={`sv ${pace.status}`}>{fmtPct(pace.deltaPct)}</div><div className="ssub">vs ideal pace (% DIFF)</div></div>
      </div>

      {/* Two-column */}
      <div className="detail-grid">
        {/* LEFT */}
        <div>
          <div className="panel">
            <div className="panel-head">
              <div><h3>Pace vs projection</h3><div className="ph-desc">Cumulative spend against ideal — today is d{daysIn}</div></div>
              <span className="sync-label">last sync · {lastSyncStr}</span>
            </div>
            <CumulativeLineChart monthly={monthly} spend={spend} daysIn={daysIn} daysInMonth={daysInMonth} accountId={account.id} />
          </div>
          <div className="cap-control">
            <div>
              <div className="ct">Stop at 100% pace</div>
              <div className="cd">{capOn ? 'Daily budgets will throttle to keep MTD spend within the monthly cap.' : 'No cap — campaigns may exceed monthly budget if left running.'}</div>
            </div>
            <Switch on={capOn} onChange={toggleCap} label={capOn ? 'on' : 'off'} />
          </div>
        </div>

        {/* RIGHT */}
        <div className="panel" style={{ padding: 0 }}>
          <div className="panel-head" style={{ padding: '14px 16px 0' }}>
            <div><h3>Segments</h3><div className="ph-desc">{isSegmented ? `${segments.length} segments · click Apply to push recommended daily` : 'Single-budget account'}</div></div>
            {isSegmented && <button className="btn ghost small" onClick={() => setApplyItem({ bulk: true, accountName: account.account_name, segments })}>Apply all</button>}
          </div>

          {campaigns.length === 0 ? (
            <div style={{ padding: '32px 16px', textAlign: 'center' }}>
              <div style={{ marginBottom: 12, color: 'var(--muted)' }}><Zap size={30} /></div>
              <div style={{ fontSize: 'var(--t-md)', fontWeight: 600, marginBottom: 6 }}>No campaigns tracked yet</div>
              <p style={{ fontSize: 'var(--t-sm)', color: 'var(--muted)', marginBottom: 14 }}>Pull all campaigns and run pacing in one click.</p>
              <div style={{ display: 'flex', gap: 8, justifyContent: 'center' }}>
                <button className="btn primary small" onClick={quickSetup} disabled={settingUp}><Zap size={12} /> {settingUp ? (setupStatus || 'Setting up…') : 'Set Up This Account'}</button>
                <button className="btn small" onClick={() => setShowImport(true)} disabled={settingUp}><Plus size={12} /> Pick manually</button>
              </div>
            </div>
          ) : isSegmented ? (
            <div style={{ overflowX: 'auto' }}>
              <table className="seg-table">
                <thead>
                  <tr>
                    <th style={{ paddingLeft: 16 }}>Segment</th>
                    <th>Pace</th><th>MTD</th><th>Monthly</th><th>Now</th><th>Rec</th>
                    <th style={{ paddingRight: 16 }} />
                  </tr>
                </thead>
                <tbody>
                  {segments.map(s => {
                    const sp = computePace(s.monthly, s.spend, daysIn, daysInMonth);
                    return (
                      <tr key={s.name}>
                        <td style={{ paddingLeft: 16 }}>
                          <div style={{ display: 'flex', alignItems: 'center', gap: 7 }}>
                            <span className={`dot ${sp.status}`} />
                            <span style={{ fontWeight: 500 }}>{s.name}</span>
                          </div>
                        </td>
                        <td><span className={`pill ${sp.status}`}>{fmtPct(sp.deltaPct)}</span></td>
                        <td>{fmt(s.spend)}</td>
                        <td>{fmt(s.monthly)}</td>
                        <td>{fmt(s.currentDaily)}</td>
                        <td className="seg-rec">{fmt(sp.dailyRec)}</td>
                        <td style={{ paddingRight: 16, textAlign: 'right' }}>
                          <button className="btn primary small" onClick={() => setApplyItem({ name: s.name, monthly: s.monthly, spend: s.spend, segmentOf: account.account_name })}>Apply</button>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ) : (
            <div style={{ padding: '32px 16px', textAlign: 'center', color: 'var(--ink-3)', fontSize: 'var(--t-sm)' }}>
              This account has a single monthly budget. Use the primary action above to apply the recommended daily.
            </div>
          )}
        </div>
      </div>

      <div className="bb-card" style={{ marginTop: 14 }}>
        <div style={{ fontFamily: "'Inter Tight', sans-serif", fontWeight: 600, fontSize: 'var(--t-lg)', marginBottom: 10 }}>Live Campaigns</div>
        <table className="bb-table">
          <thead><tr><th>Campaign</th><th>Segment</th><th>Status</th><th>Current Daily</th><th>Share</th><th>MTD Spend</th></tr></thead>
          <tbody>
            {campaigns.map(c => {
              const cd = currentDaily(c);
              const share = currentDailyTotal > 0 ? (cd / currentDailyTotal) * 100 : 0;
              return (
                <tr key={c.id} style={{ cursor: 'pointer' }} onClick={() => navigate(`/campaigns/${c.id}`)}>
                  <td style={{ fontWeight: 500 }}>{c.campaign_name}</td>
                  <td>{c.budget_label || 'Primary'}</td>
                  <td>{c.is_active ? 'Live' : 'Inactive w/ spend'}</td>
                  <td>{fmt(cd)}</td>
                  <td>{share.toFixed(1)}%</td>
                  <td>{fmt(c.latest_pacing?.actual_spend || 0)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>

      {/* Raw recommendations table (kept for completeness) */}
      {recommendations.length > 0 && (
        <div className="bb-card" style={{ marginTop: 14 }}>
          <div style={{ fontFamily: "'Inter Tight', sans-serif", fontWeight: 600, fontSize: 'var(--t-lg)', marginBottom: 10 }}>Campaign Budget Allocations</div>
          <table className="bb-table">
            <thead><tr><th>Campaign</th><th>Campaign MTD</th><th>Segment Pace</th><th>Current Daily</th><th>Rec Daily</th><th>Change</th></tr></thead>
            <tbody>
              {recommendations.map(rec => (
                <tr key={rec.campaign_id} style={{ cursor: 'pointer' }} onClick={() => navigate(`/campaigns/${rec.campaign_id}`)}>
                  <td style={{ fontWeight: 500 }}>{rec.campaign_name}</td>
                  <td>{fmt(rec.actual_spend)}</td>
                  <td>{(rec.pace_ratio * 100).toFixed(1)}%</td>
                  <td>{fmt(rec.current_daily_budget)}</td>
                  <td style={{ fontWeight: 700 }}>{fmt(rec.recommended_daily_budget)}</td>
                  <td style={{ color: rec.change_percent > 0 ? 'var(--amber)' : rec.change_percent < 0 ? 'var(--red)' : 'var(--muted)' }}>
                    {rec.change_percent > 0 ? '+' : ''}{rec.change_percent?.toFixed(1)}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {showImport && <ImportCampaignsModal account={account} onClose={() => setShowImport(false)} onImported={() => { setShowImport(false); load(); }} />}
      {applyItem  && <ApplyModal item={applyItem} onClose={() => setApplyItem(null)} onConfirm={handleConfirmApply} />}
    </div>
  );
}
