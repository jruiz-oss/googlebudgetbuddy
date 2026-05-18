import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { Link2, Link2Off, Save } from 'lucide-react';
import axios from 'axios';
import { useToast } from '../components/Toast';
import { useAuth } from '../App';

export default function Settings() {
  const { id } = useParams();
  const { addToast } = useToast();
  const { googleConnected, setGoogleConnected } = useAuth();

  const [account, setAccount] = useState(null);
  const [settings, setSettings] = useState(null);
  const [sheetId, setSheetId] = useState('');
  const [autoPause, setAutoPause] = useState(false);
  const [autoPauseThreshold, setAutoPauseThreshold] = useState(95);
  const [digestEnabled, setDigestEnabled] = useState(false);
  const [trackLeads, setTrackLeads] = useState(false);
  const [saving, setSaving] = useState(false);
  const [connectingGoogle, setConnectingGoogle] = useState(false);
  const [previewRows, setPreviewRows] = useState(null);
  const [previewing, setPreviewing] = useState(false);

  useEffect(() => {
    Promise.all([
      axios.get(`/api/accounts/${id}`),
      axios.get(`/api/settings/${id}`),
    ]).then(([accR, setR]) => {
      setAccount(accR.data.account);
      const s = setR.data.settings;
      setSettings(s);
      setSheetId(s.google_sheet_id || '');
      setAutoPause(s.auto_pause_enabled);
      setAutoPauseThreshold(s.auto_pause_threshold);
      setDigestEnabled(s.daily_digest_enabled);
      setTrackLeads(s.track_leads);
    }).catch(() => addToast('Failed to load settings', 'error'));
  }, [id]);

  const save = async () => {
    setSaving(true);
    try {
      await axios.put(`/api/settings/${id}`, {
        google_sheet_id: sheetId.trim() || null,
        auto_pause_enabled: autoPause,
        auto_pause_threshold: autoPauseThreshold,
        daily_digest_enabled: digestEnabled,
        track_leads: trackLeads,
      });
      addToast('Settings saved', 'success');
    } catch (e) {
      addToast(e.response?.data?.error || 'Save failed', 'error');
    } finally {
      setSaving(false);
    }
  };

  const connectGoogle = async () => {
    setConnectingGoogle(true);
    try {
      const r = await axios.get('/api/oauth/authorize');
      window.location.href = r.data.url;
    } catch (e) {
      addToast(e.response?.data?.error || 'Failed to start Google auth', 'error');
      setConnectingGoogle(false);
    }
  };

  const disconnectGoogle = async () => {
    try {
      await axios.post('/api/oauth/disconnect');
      setGoogleConnected(false);
      addToast('Google account disconnected', 'info');
    } catch {
      addToast('Disconnect failed', 'error');
    }
  };

  const previewSheet = async () => {
    if (!sheetId.trim()) { addToast('Enter a Sheet ID first', 'warn'); return; }
    setPreviewing(true);
    try {
      await axios.put(`/api/settings/${id}`, { google_sheet_id: sheetId.trim() });
      const r = await axios.get(`/api/sheets/${id}/preview`);
      setPreviewRows(r.data.preview || []);
    } catch (e) {
      addToast(e.response?.data?.error || 'Preview failed', 'error');
    } finally {
      setPreviewing(false);
    }
  };

  if (!account || !settings) return <p className="bb-muted">Loading…</p>;

  return (
    <div>
      <h1 className="bb-page-title" style={{ marginBottom: '24px' }}>Settings — {account.account_name}</h1>

      {/* Google Account Connection */}
      <div className="bb-card" style={{ marginBottom: '20px' }}>
        <h2 className="bb-section-title">Google Account</h2>
        <p className="bb-muted" style={{ marginBottom: '16px' }}>
          Connect your Google account to enable live spend syncing, budget updates, and lead exports.
          One connection covers all accounts.
        </p>
        {googleConnected ? (
          <div className="bb-row" style={{ gap: '12px', alignItems: 'center' }}>
            <span className="bb-pill bb-pill-on">● Connected</span>
            <button className="bb-btn bb-btn-ghost" onClick={disconnectGoogle}>
              <Link2Off size={15} /> Disconnect
            </button>
          </div>
        ) : (
          <button className="bb-btn bb-btn-primary" onClick={connectGoogle} disabled={connectingGoogle}>
            <Link2 size={15} /> {connectingGoogle ? 'Redirecting to Google…' : 'Connect Google Account'}
          </button>
        )}
      </div>

      {/* Google Sheets */}
      <div className="bb-card" style={{ marginBottom: '20px' }}>
        <h2 className="bb-section-title">Google Sheets Integration</h2>
        <p className="bb-muted" style={{ marginBottom: '16px' }}>
          Your Google Sheet is the source of truth for monthly budgets. Paste the Sheet ID below (the long string in the URL between /d/ and /edit).
        </p>
        <div className="bb-form-group">
          <label className="bb-form-label">Sheet ID</label>
          <input
            className="bb-input"
            value={sheetId}
            onChange={e => setSheetId(e.target.value)}
            placeholder="1BxiMVs0XRA5nFMdKvBdBZjgmUUqptlbs74OgVE2upms"
          />
          <p className="bb-form-help">From the URL: docs.google.com/spreadsheets/d/<strong>[THIS PART]</strong>/edit</p>
        </div>
        <div className="bb-row" style={{ gap: '8px' }}>
          <button className="bb-btn bb-btn-secondary" onClick={previewSheet} disabled={previewing}>
            {previewing ? 'Previewing…' : 'Preview Match'}
          </button>
        </div>

        {previewRows !== null && (
          <div style={{ marginTop: '16px' }}>
            <p className="bb-section-meta" style={{ marginBottom: '8px' }}>{previewRows.length} row(s) matched</p>
            {previewRows.length > 0 && (
              <table className="bb-table" style={{ width: '100%' }}>
                <thead>
                  <tr><th>Sheet Row</th><th>Matched Campaign</th><th>Budget</th><th>Match Type</th></tr>
                </thead>
                <tbody>
                  {previewRows.map((row, i) => (
                    <tr key={i}>
                      <td className="bb-muted">{row.sheet_name}</td>
                      <td>{row.campaign_name || <span className="bb-muted">No match</span>}</td>
                      <td>{row.monthly_budget ? `$${row.monthly_budget}` : '—'}</td>
                      <td><span className="bb-muted" style={{ fontSize: '12px' }}>{row.match_type || '—'}</span></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            )}
          </div>
        )}
      </div>

      {/* Auto-Pause */}
      <div className="bb-card" style={{ marginBottom: '20px' }}>
        <h2 className="bb-section-title">Auto-Pause</h2>
        <p className="bb-muted" style={{ marginBottom: '16px' }}>
          Automatically flag a warning (and optionally pause all campaigns) when MTD spend hits a threshold of the monthly budget.
        </p>
        <div className="bb-form-group">
          <label style={{ display: 'flex', alignItems: 'center', gap: '10px', cursor: 'pointer' }}>
            <input type="checkbox" checked={autoPause} onChange={e => setAutoPause(e.target.checked)} />
            <span className="bb-form-label" style={{ margin: 0 }}>Enable auto-pause warnings</span>
          </label>
        </div>
        <div className="bb-form-group">
          <label className="bb-form-label">Pause Threshold (%)</label>
          <input
            className="bb-input"
            type="number"
            min="50" max="100" step="1"
            value={autoPauseThreshold}
            onChange={e => setAutoPauseThreshold(Number(e.target.value))}
            style={{ width: '120px' }}
          />
          <p className="bb-form-help">When MTD spend reaches this % of monthly budget, you'll see a warning on the dashboard.</p>
        </div>
      </div>

      {/* Leads tracking */}
      <div className="bb-card" style={{ marginBottom: '20px' }}>
        <h2 className="bb-section-title">Lead Tracking</h2>
        <p className="bb-muted" style={{ marginBottom: '16px' }}>
          Enable to pull Google Ads lead form submissions for this account. You can then export them as CSV from the Leads page.
        </p>
        <label style={{ display: 'flex', alignItems: 'center', gap: '10px', cursor: 'pointer' }}>
          <input type="checkbox" checked={trackLeads} onChange={e => setTrackLeads(e.target.checked)} />
          <span className="bb-form-label" style={{ margin: 0 }}>Track lead form submissions</span>
        </label>
      </div>

      {/* Digest email */}
      <div className="bb-card" style={{ marginBottom: '24px' }}>
        <h2 className="bb-section-title">Daily Digest Email</h2>
        <p className="bb-muted" style={{ marginBottom: '16px' }}>
          Receive an email after each scheduled pacing run (daily at 6:00 AM UTC) with a summary of recommendations.
          Requires SMTP env vars to be set on Railway.
        </p>
        <label style={{ display: 'flex', alignItems: 'center', gap: '10px', cursor: 'pointer' }}>
          <input type="checkbox" checked={digestEnabled} onChange={e => setDigestEnabled(e.target.checked)} />
          <span className="bb-form-label" style={{ margin: 0 }}>Enable daily digest email</span>
        </label>
      </div>

      <button className="bb-btn bb-btn-primary" onClick={save} disabled={saving}>
        <Save size={15} /> {saving ? 'Saving…' : 'Save Settings'}
      </button>
    </div>
  );
}
