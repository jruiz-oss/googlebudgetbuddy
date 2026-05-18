import { useState, useEffect, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { Play, Check, TrendingUp, TrendingDown, Minus, Settings, History, Download, Plus, RefreshCw, PauseCircle, Zap } from 'lucide-react';
import axios from 'axios';
import { useToast } from '../components/Toast';
import { SkeletonTable } from '../components/Skeleton';
import EmptyState from '../components/EmptyState';
import SpendChart from '../components/SpendChart';

/** Group an array of objects by a key, preserving first-seen order. */
function groupBy(arr, key) {
  const map = new Map();
  for (const item of arr) {
    const k = item[key] || 'Primary';
    if (!map.has(k)) map.set(k, []);
    map.get(k).push(item);
  }
  return map; // Map<string, item[]>
}

function StatusPill({ status }) {
  if (!status) return <span className="bb-pill bb-pill-muted">No data</span>;
  if (status === 'INCREASE') return <span className="bb-pill bb-pill-up"><TrendingUp size={12} /> Increase</span>;
  if (status === 'DECREASE') return <span className="bb-pill bb-pill-down"><TrendingDown size={12} /> Decrease</span>;
  return <span className="bb-pill bb-pill-on"><Minus size={12} /> On Pace</span>;
}

function ImportCampaignsModal({ account, onClose, onImported }) {
  const [liveCampaigns, setLiveCampaigns] = useState([]);
  const [selected, setSelected] = useState(new Set());
  const [loading, setLoading] = useState(true);
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState('');
  const { addToast } = useToast();

  useEffect(() => {
    axios.get(`/api/accounts/${account.id}/sync-campaigns`)
      .then(r => setLiveCampaigns(r.data.campaigns || []))
      .catch(e => setError(e.response?.data?.error || 'Failed to load campaigns'))
      .finally(() => setLoading(false));
  }, [account.id]);

  const toggle = (id) => setSelected(s => {
    const n = new Set(s);
    n.has(id) ? n.delete(id) : n.add(id);
    return n;
  });

  const handleImport = async () => {
    if (!selected.size) return;
    setImporting(true);
    try {
      const r = await axios.post(`/api/accounts/${account.id}/sync-campaigns`, {
        campaign_ids: [...selected],
      });
      addToast(r.data.message, 'success');
      onImported();
    } catch (e) {
      setError(e.response?.data?.error || 'Import failed');
    } finally {
      setImporting(false);
    }
  };

  return (
    <div className="bb-modal-overlay" onClick={onClose}>
      <div className="bb-modal" style={{ maxWidth: '600px' }} onClick={e => e.stopPropagation()}>
        <div className="bb-modal-header">
          <h2 className="bb-section-title">Import Campaigns from Google Ads</h2>
          <button className="bb-btn bb-btn-ghost" onClick={onClose}>✕</button>
        </div>
        {error && <div className="bb-alert bb-alert-error">{error}</div>}
        {loading ? <p className="bb-muted">Loading campaigns from Google Ads…</p> : (
          <>
            <p className="bb-muted" style={{ marginBottom: '12px' }}>Select campaigns to track for pacing. Monthly budgets will be pulled from your Google Sheet.</p>
            <div style={{ maxHeight: '320px', overflowY: 'auto', border: '1px solid var(--color-border)', borderRadius: '8px' }}>
              {liveCampaigns.map(c => (
                <label key={c.campaign_id} style={{ display: 'flex', alignItems: 'center', gap: '12px', padding: '10px 14px', borderBottom: '1px solid var(--color-border)', cursor: 'pointer' }}>
                  <input type="checkbox" checked={selected.has(c.campaign_id)} onChange={() => toggle(c.campaign_id)} />
                  <span style={{ flex: 1 }}>{c.campaign_name}</span>
                  <span className="bb-muted" style={{ fontSize: '13px' }}>{c.status}</span>
                  <span className="bb-muted" style={{ fontSize: '13px' }}>${c.daily_budget_usd?.toFixed(2)}/day</span>
                </label>
              ))}
            </div>
            <div className="bb-row" style={{ justifyContent: 'space-between', marginTop: '16px' }}>
              <span className="bb-muted">{selected.size} selected</span>
              <div className="bb-row" style={{ gap: '8px' }}>
                <button className="bb-btn bb-btn-secondary" onClick={onClose}>Cancel</button>
                <button className="bb-btn bb-btn-primary" onClick={handleImport} disabled={!selected.size || importing}>
                  {importing ? 'Importing…' : `Import ${selected.size} campaign(s)`}
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

export default function AccountDashboard() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { addToast } = useToast();

  const [account, setAccount] = useState(null);
  const [campaigns, setCampaigns] = useState([]);
  const [recommendations, setRecommendations] = useState([]);
  const [summary, setSummary] = useState(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [applying, setApplying] = useState(false);
  const [settingUp, setSettingUp] = useState(false);
  const [setupStatus, setSetupStatus] = useState('');
  const [selected, setSelected] = useState(new Set());
  const [showImport, setShowImport] = useState(false);
  const [sheetSync, setSheetSync] = useState(null);
  const [sheetWrite, setSheetWrite] = useState(null);
  const [pauseWarning, setPauseWarning] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [accR, campR] = await Promise.all([
        axios.get(`/api/accounts/${id}/summary`),
        axios.get(`/api/campaigns/account/${id}`),
      ]);
      setAccount(accR.data.account);
      setCampaigns(campR.data.campaigns || []);
    } catch {
      addToast('Failed to load account', 'error');
    } finally {
      setLoading(false);
    }
  }, [id]);

  useEffect(() => { load(); }, [load]);

  const mergeRecommendationsIntoCampaigns = (nextRecommendations) => {
    const byCampaignId = new Map((nextRecommendations || []).map(rec => [rec.campaign_id, rec]));
    setCampaigns(prev => prev.map(c => {
      const rec = byCampaignId.get(c.id);
      if (!rec) return c;
      return {
        ...c,
        monthly_budget: rec.monthly_budget ?? c.monthly_budget,
        latest_pacing: {
          ...(c.latest_pacing || {}),
          actual_spend: rec.actual_spend,
          expected_spend: rec.expected_spend,
          pace_ratio: rec.pace_ratio,
          current_daily_budget: rec.current_daily_budget,
          recommended_daily_budget: rec.recommended_daily_budget,
          change_percent: rec.change_percent,
          status: rec.status,
          clicks: rec.clicks,
          conversions: rec.conversions,
          cpc: rec.cpc,
        },
      };
    }));
  };

  const runPacing = async () => {
    setRunning(true);
    setRecommendations([]);
    setSummary(null);
    setSheetSync(null);
    setSheetWrite(null);
    setPauseWarning(null);
    try {
      const r = await axios.post(`/api/pacing/${id}/run`);
      const nextRecommendations = r.data.recommendations || [];
      setRecommendations(nextRecommendations);
      setSummary(r.data.summary);
      setSheetSync(r.data.sheet_sync);
      setSheetWrite(r.data.sheet_write);
      setPauseWarning(r.data.auto_pause_warning);
      mergeRecommendationsIntoCampaigns(nextRecommendations);
      // Pre-select all non-on-pace campaigns
      const toSelect = new Set(
        nextRecommendations
          .filter(rec => rec.status !== 'ON_PACE')
          .map(rec => rec.campaign_id)
      );
      setSelected(toSelect);
      if (r.data.sheet_sync && !r.data.sheet_sync.error) {
        const budgetsUpdated = r.data.sheet_sync.updated_count || r.data.sheet_sync.budgets_updated || 0;
        if (budgetsUpdated > 0) addToast(`Pulled ${budgetsUpdated} budget(s) from Google Sheet`, 'info');
        if (r.data.sheet_sync.warning) addToast(r.data.sheet_sync.warning, 'warn');
      }
      if (r.data.sheet_write && !r.data.sheet_write.error) {
        const spendRowsWritten = r.data.sheet_write.written_count || 0;
        if (spendRowsWritten > 0) addToast(`Wrote spend to ${spendRowsWritten} Google Sheet row(s)`, 'info');
      }
      addToast('Pacing run complete', 'success');
    } catch (e) {
      addToast(e.response?.data?.error || 'Pacing run failed', 'error');
    } finally {
      setRunning(false);
    }
  };

  // One-click setup: fetch all campaigns from Google Ads, import them all,
  // then immediately run pacing so data appears straight away.
  const quickSetup = async () => {
    setSettingUp(true);
    setSetupStatus('Fetching campaigns from Google Ads…');
    try {
      // 1. Get live campaigns
      const liveR = await axios.get(`/api/accounts/${id}/sync-campaigns`);
      const live = liveR.data.campaigns || [];
      if (!live.length) {
        addToast('No campaigns found in Google Ads for this account', 'warn');
        setSettingUp(false);
        setSetupStatus('');
        return;
      }

      // 2. Import all of them
      setSetupStatus(`Importing ${live.length} campaign(s)…`);
      await axios.post(`/api/accounts/${id}/sync-campaigns`, {
        campaign_ids: live.map(c => c.campaign_id),
      });

      // 3. Run pacing immediately
      setSetupStatus('Running pacing…');
      const pacingR = await axios.post(`/api/pacing/${id}/run`);
      const nextRecommendations = pacingR.data.recommendations || [];
      setRecommendations(nextRecommendations);
      setSummary(pacingR.data.summary);
      setSheetSync(pacingR.data.sheet_sync);
      setSheetWrite(pacingR.data.sheet_write);
      setPauseWarning(pacingR.data.auto_pause_warning);
      mergeRecommendationsIntoCampaigns(nextRecommendations);
      if (pacingR.data.sheet_sync?.warning) addToast(pacingR.data.sheet_sync.warning, 'warn');
      const toSelect = new Set(
        nextRecommendations
          .filter(rec => rec.status !== 'ON_PACE')
          .map(rec => rec.campaign_id)
      );
      setSelected(toSelect);

      addToast(`Set up ${live.length} campaign(s) and ran pacing`, 'success');
      load();
    } catch (e) {
      addToast(e.response?.data?.error || 'Setup failed', 'error');
    } finally {
      setSettingUp(false);
      setSetupStatus('');
    }
  };

  const applySelected = async () => {
    if (!selected.size) return;
    setApplying(true);
    const adjustments = recommendations
      .filter(r => selected.has(r.campaign_id))
      .map(r => ({
        campaign_id: r.campaign_id,
        budget_resource_name: r.budget_resource_name,
        new_daily_budget: r.recommended_daily_budget,
      }));
    try {
      const r = await axios.post(`/api/pacing/${id}/apply`, { adjustments });
      addToast(r.data.message, r.data.errors?.length ? 'warn' : 'success');
      // Update UI to reflect applied
      setRecommendations(prev => prev.map(rec =>
        selected.has(rec.campaign_id) ? { ...rec, status: 'ON_PACE', current_daily_budget: rec.recommended_daily_budget } : rec
      ));
      setSelected(new Set());
      load();
    } catch (e) {
      addToast(e.response?.data?.error || 'Apply failed', 'error');
    } finally {
      setApplying(false);
    }
  };

  const toggleSelect = (campId) => setSelected(s => {
    const n = new Set(s);
    n.has(campId) ? n.delete(campId) : n.add(campId);
    return n;
  });

  const activeCampaigns = campaigns.filter(c => c.is_active !== false);
  const totalBudget = activeCampaigns.reduce((s, c) => s + (c.monthly_budget || 0), 0);
  const totalSpend = activeCampaigns.reduce((s, c) => s + (c.latest_pacing?.actual_spend || 0), 0);
  const spendPct = totalBudget > 0 ? (totalSpend / totalBudget * 100) : 0;
  const hasSheetId = Boolean(account?.settings?.google_sheet_id);

  if (loading) return <div><SkeletonTable /></div>;
  if (!account) return <div className="bb-alert bb-alert-error">Account not found</div>;

  return (
    <div>
      {/* Header */}
      <div className="bb-row-between" style={{ marginBottom: '8px' }}>
        <div>
          <h1 className="bb-page-title">{account.account_name}</h1>
          <p className="bb-muted" style={{ fontSize: '13px' }}>Customer ID: {account.google_customer_id}</p>
        </div>
        <div className="bb-row" style={{ gap: '8px' }}>
          <button className="bb-btn bb-btn-ghost" onClick={() => navigate(`/accounts/${id}/leads`)}>
            <Download size={15} /> Leads
          </button>
          <button className="bb-btn bb-btn-ghost" onClick={() => navigate(`/accounts/${id}/history`)}>
            <History size={15} /> History
          </button>
          <button className="bb-btn bb-btn-ghost" onClick={() => navigate(`/accounts/${id}/settings`)}>
            <Settings size={15} /> Settings
          </button>
        </div>
      </div>

      {/* Spend overview */}
      <div className="bb-grid" style={{ gridTemplateColumns: 'repeat(3, 1fr)', marginBottom: '24px' }}>
        <div className="bb-card bb-stat-tile">
          <p className="bb-section-meta">Monthly Budget</p>
          <p className="bb-stat-value">${totalBudget.toLocaleString('en-US', { minimumFractionDigits: 2 })}</p>
        </div>
        <div className="bb-card bb-stat-tile">
          <p className="bb-section-meta">MTD Spend</p>
          <p className="bb-stat-value">${totalSpend.toLocaleString('en-US', { minimumFractionDigits: 2 })}</p>
          <p className="bb-muted" style={{ fontSize: '13px', marginTop: '4px' }}>{spendPct.toFixed(1)}% of budget</p>
        </div>
        <div className="bb-card bb-stat-tile">
          <p className="bb-section-meta">Campaigns Tracked</p>
          <p className="bb-stat-value">{activeCampaigns.length}</p>
        </div>
      </div>

      {/* Auto-pause warning */}
      {pauseWarning && (
        <div className="bb-alert bb-alert-warn" style={{ marginBottom: '16px' }}>
          <PauseCircle size={16} style={{ marginRight: '8px' }} />
          <strong>Auto-pause threshold reached:</strong> {pauseWarning.message}
        </div>
      )}

      {!hasSheetId && (
        <div className="bb-alert bb-alert-warn" style={{ marginBottom: '16px' }}>
          <strong>Google Sheet not configured:</strong> budgets will stay at $0 until you add a Sheet ID in Settings.
        </div>
      )}

      {/* Sheet sync result */}
      {sheetSync?.error && (
        <div className="bb-alert bb-alert-warn" style={{ marginBottom: '16px' }}>
          Sheet sync warning: {sheetSync.error}
        </div>
      )}

      {sheetSync?.warning && (
        <div className="bb-alert bb-alert-warn" style={{ marginBottom: '16px' }}>
          Sheet sync warning: {sheetSync.warning}
        </div>
      )}

      {sheetWrite?.error && (
        <div className="bb-alert bb-alert-warn" style={{ marginBottom: '16px' }}>
          Sheet writeback warning: {sheetWrite.error}
        </div>
      )}

      {/* Run pacing + recommendations */}
      <div className="bb-card" style={{ marginBottom: '24px' }}>
        <div className="bb-row-between" style={{ marginBottom: recommendations.length ? '16px' : '0' }}>
          <div>
            <h2 className="bb-section-title">Budget Pacing</h2>
            {summary && (
              <p className="bb-section-meta">
                {summary.increase} increase · {summary.decrease} decrease · {summary.on_pace} on pace
              </p>
            )}
          </div>
          <div className="bb-row" style={{ gap: '8px' }}>
            {selected.size > 0 && (
              <button className="bb-btn bb-btn-primary" onClick={applySelected} disabled={applying}>
                <Check size={15} /> {applying ? 'Applying…' : `Apply ${selected.size} Change(s)`}
              </button>
            )}
            <button className="bb-btn bb-btn-primary" onClick={runPacing} disabled={running}>
              <Play size={15} /> {running ? 'Running…' : 'Run Pacing'}
            </button>
          </div>
        </div>

        {recommendations.length > 0 && (() => {
          const segMap = groupBy(recommendations, 'budget_label');
          const isMultiSegment = segMap.size > 1;
          return (
            <table className="bb-table" style={{ width: '100%' }}>
              <thead>
                <tr>
                  <th style={{ width: '32px' }}></th>
                  <th>Campaign</th>
                  <th>Monthly Budget</th>
                  <th>MTD Spend</th>
                  <th>Pace</th>
                  <th>Current Daily</th>
                  <th>Recommended Daily</th>
                  <th>Change</th>
                  <th>Status</th>
                </tr>
              </thead>
              <tbody>
                {[...segMap.entries()].map(([label, recs]) => {
                  const segBudget = recs.reduce((s, r) => s + (r.monthly_budget || 0), 0);
                  const segSpend  = recs.reduce((s, r) => s + (r.actual_spend  || 0), 0);
                  const segPct    = segBudget > 0 ? (segSpend / segBudget * 100).toFixed(1) : '—';
                  return [
                    isMultiSegment && (
                      <tr key={`seg-${label}`} style={{ background: 'var(--color-bg-subtle, #f5f7fa)' }}>
                        <td colSpan={9} style={{ padding: '6px 12px', fontWeight: 700, fontSize: '12px', letterSpacing: '0.05em', textTransform: 'uppercase', color: 'var(--color-text-muted)', borderBottom: '2px solid var(--color-border)' }}>
                          {label}
                          <span style={{ fontWeight: 400, marginLeft: '12px', fontSize: '11px' }}>
                            ${segBudget.toLocaleString('en-US', { minimumFractionDigits: 2 })} budget · ${segSpend.toLocaleString('en-US', { minimumFractionDigits: 2 })} spent ({segPct}%)
                          </span>
                        </td>
                      </tr>
                    ),
                    ...recs.map(rec => (
                      <tr
                        key={rec.campaign_id}
                        className={rec.status === 'DECREASE' ? 'bb-table-row-tint-down' : rec.status === 'INCREASE' ? 'bb-table-row-tint-up' : ''}
                        style={{ cursor: rec.status !== 'ON_PACE' ? 'pointer' : 'default' }}
                        onClick={() => rec.status !== 'ON_PACE' && toggleSelect(rec.campaign_id)}
                      >
                        <td>
                          {rec.status !== 'ON_PACE' && (
                            <input type="checkbox" checked={selected.has(rec.campaign_id)} onChange={() => toggleSelect(rec.campaign_id)} onClick={e => e.stopPropagation()} />
                          )}
                        </td>
                        <td style={{ fontWeight: 500, cursor: 'pointer' }} onClick={e => { e.stopPropagation(); navigate(`/campaigns/${rec.campaign_id}`); }}>
                          {rec.campaign_name}
                        </td>
                        <td>${rec.monthly_budget?.toFixed(2)}</td>
                        <td>${rec.actual_spend?.toFixed(2)}</td>
                        <td>{(rec.pace_ratio * 100).toFixed(1)}%</td>
                        <td>${rec.current_daily_budget?.toFixed(2)}</td>
                        <td style={{ fontWeight: 600 }}>${rec.recommended_daily_budget?.toFixed(2)}</td>
                        <td style={{ color: rec.change_percent > 0 ? 'var(--color-warning)' : rec.change_percent < 0 ? 'var(--color-danger)' : 'var(--color-text-muted)' }}>
                          {rec.change_percent > 0 ? '+' : ''}{rec.change_percent?.toFixed(1)}%
                        </td>
                        <td><StatusPill status={rec.status} /></td>
                      </tr>
                    )),
                  ];
                })}
              </tbody>
            </table>
          );
        })()}

        {!running && recommendations.length === 0 && (
          <EmptyState
            icon={<Play size={28} />}
            title="Ready to pace"
            body="Click Run Pacing to pull live spend from Google Ads and compute recommended daily budgets."
          />
        )}
      </div>

      {/* Tracked campaigns table */}
      <div className="bb-card">
        <div className="bb-row-between" style={{ marginBottom: '16px' }}>
          <h2 className="bb-section-title">Tracked Campaigns</h2>
          <button className="bb-btn bb-btn-secondary" onClick={() => setShowImport(true)}>
            <Plus size={15} /> Import from Google Ads
          </button>
        </div>

        {activeCampaigns.length === 0 ? (
          <div style={{ textAlign: 'center', padding: '40px 24px' }}>
            <div style={{ marginBottom: '16px', color: 'var(--color-text-muted)' }}>
              <Zap size={36} />
            </div>
            <h3 style={{ marginBottom: '8px', fontSize: '16px', fontWeight: 600 }}>No campaigns tracked yet</h3>
            <p className="bb-muted" style={{ marginBottom: '24px', maxWidth: '380px', margin: '0 auto 24px' }}>
              Pull all campaigns from Google Ads and run your first pacing check in one click.
            </p>
            <div className="bb-row" style={{ justifyContent: 'center', gap: '10px' }}>
              <button
                className="bb-btn bb-btn-primary"
                onClick={quickSetup}
                disabled={settingUp}
                style={{ fontSize: '15px', padding: '10px 24px' }}
              >
                <Zap size={16} />
                {settingUp ? setupStatus || 'Setting up…' : 'Set Up This Account'}
              </button>
              <button className="bb-btn bb-btn-secondary" onClick={() => setShowImport(true)} disabled={settingUp}>
                <Plus size={15} /> Pick manually
              </button>
            </div>
          </div>
        ) : (() => {
          const segMap = groupBy(activeCampaigns, 'budget_label');
          const isMultiSegment = segMap.size > 1;
          return (
            <table className="bb-table" style={{ width: '100%' }}>
              <thead>
                <tr>
                  <th>Campaign</th>
                  <th>Monthly Budget</th>
                  <th>Last Spend</th>
                  <th>Status</th>
                  <th>Flight</th>
                </tr>
              </thead>
              <tbody>
                {[...segMap.entries()].map(([label, camps]) => {
                  const segBudget = camps.reduce((s, c) => s + (c.monthly_budget || 0), 0);
                  const segSpend  = camps.reduce((s, c) => s + (c.latest_pacing?.actual_spend || 0), 0);
                  const segPct    = segBudget > 0 ? (segSpend / segBudget * 100).toFixed(1) : '—';
                  return [
                    isMultiSegment && (
                      <tr key={`seg-${label}`} style={{ background: 'var(--color-bg-subtle, #f5f7fa)' }}>
                        <td colSpan={5} style={{ padding: '6px 12px', fontWeight: 700, fontSize: '12px', letterSpacing: '0.05em', textTransform: 'uppercase', color: 'var(--color-text-muted)', borderBottom: '2px solid var(--color-border)' }}>
                          {label}
                          <span style={{ fontWeight: 400, marginLeft: '12px', fontSize: '11px' }}>
                            ${segBudget.toLocaleString('en-US', { minimumFractionDigits: 2 })} budget · ${segSpend.toLocaleString('en-US', { minimumFractionDigits: 2 })} spent ({segPct}%)
                          </span>
                        </td>
                      </tr>
                    ),
                    ...camps.map(c => (
                      <tr key={c.id} style={{ cursor: 'pointer' }} onClick={() => navigate(`/campaigns/${c.id}`)}>
                        <td style={{ fontWeight: 500 }}>{c.campaign_name}</td>
                        <td>${(c.monthly_budget || 0).toFixed(2)}</td>
                        <td>{c.latest_pacing ? `$${c.latest_pacing.actual_spend?.toFixed(2)}` : '—'}</td>
                        <td><StatusPill status={c.latest_pacing?.status} /></td>
                        <td><span className="bb-muted" style={{ fontSize: '13px' }}>{c.flight_type === 'ALWAYS_ON' ? 'Always On' : `${c.flight_start_date} → ${c.flight_end_date}`}</span></td>
                      </tr>
                    )),
                  ];
                })}
              </tbody>
            </table>
          );
        })()}
      </div>

      {showImport && (
        <ImportCampaignsModal
          account={account}
          onClose={() => setShowImport(false)}
          onImported={() => { setShowImport(false); load(); }}
        />
      )}
    </div>
  );
}
