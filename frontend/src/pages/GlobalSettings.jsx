import { useState, useEffect } from 'react';
import { Plus, RefreshCw, Link2, Link2Off } from 'lucide-react';
import axios from 'axios';
import { useAuth } from '../App';
import { useToast } from '../components/Toast';

function Switch({ on, onChange }) {
  return (
    <label className="switch">
      <input type="checkbox" checked={on} onChange={e => onChange(e.target.checked)} />
      <span className="switch-track" />
      <span className="switch-knob" />
    </label>
  );
}

export default function GlobalSettings() {
  const toast = useToast();
  const { googleConnected, setGoogleConnected } = useAuth();
  const [syncing, setSyncing] = useState(false);
  const [sharedSheetId, setSharedSheetId] = useState('');
  const [applyingSheet, setApplyingSheet] = useState(false);
  const [connectingGoogle, setConnectingGoogle] = useState(false);
  const [lastSync, setLastSync] = useState(null);
  const [anthropicKey, setAnthropicKey] = useState('');
  const [anthropicKeyHint, setAnthropicKeyHint] = useState(null);
  const [savingKey, setSavingKey] = useState(false);

  // Notification settings
  const [s, setS] = useState({
    emailDigest: true,
    digestFreq: 'daily',
    emailOver: true,
    emailUnder: false,
    emailRunning: true,
    threshold: 5,
    defaultCap: true,
    timezone: 'America/Chicago',
    fiscalStart: '1',
    syncFreq: 'hourly',
  });
  const set = (k, v) => setS(prev => ({ ...prev, [k]: v }));

  useEffect(() => {
    axios.get('/api/campaigns/all').then(r => {
      const accounts = r.data.accounts || [];
      const ids = [...new Set(accounts.map(a => a.settings?.google_sheet_id).filter(Boolean))];
      if (ids.length === 1) setSharedSheetId(ids[0]);
    }).catch(() => {});
    axios.get('/api/reports/user-settings').then(r => {
      if (r.data.anthropic_api_key_hint) setAnthropicKeyHint(r.data.anthropic_api_key_hint);
    }).catch(() => {});
  }, []);

  const saveAnthropicKey = async () => {
    if (!anthropicKey.trim()) return;
    setSavingKey(true);
    try {
      const r = await axios.put('/api/reports/user-settings', { anthropic_api_key: anthropicKey.trim() });
      setAnthropicKeyHint(r.data.anthropic_api_key_hint);
      setAnthropicKey('');
      toast.success('Anthropic API key saved');
    } catch (e) {
      toast.error(e.response?.data?.error || 'Failed to save key');
    } finally { setSavingKey(false); }
  };

  const connectGoogle = async () => {
    setConnectingGoogle(true);
    try {
      const r = await axios.get('/api/oauth/authorize');
      window.location.href = r.data.url;
    } catch (e) {
      toast.error(e.response?.data?.error || 'Failed to start Google auth');
      setConnectingGoogle(false);
    }
  };

  const disconnectGoogle = async () => {
    try {
      await axios.post('/api/oauth/disconnect');
      setGoogleConnected(false);
      toast.info('Google account disconnected');
    } catch { toast.error('Disconnect failed'); }
  };

  const applySheetToAll = async () => {
    if (!sharedSheetId.trim()) { toast.warn('Enter a Google Sheet ID first'); return; }
    setApplyingSheet(true);
    try {
      const r = await axios.post('/api/settings/apply-sheet-to-all', { google_sheet_id: sharedSheetId.trim() });
      toast.success(r.data.message || 'Applied sheet to all accounts');
    } catch (e) { toast.error(e.response?.data?.error || 'Failed to apply sheet'); }
    finally { setApplyingSheet(false); }
  };

  const syncNow = async () => {
    setSyncing(true);
    try {
      const r = await axios.post('/api/accounts/sync-from-mcc', {});
      if (r.status === 202) {
        toast.info('Sync started in background — check back in ~75 seconds');
      } else {
        toast.success('Sync complete');
      }
      setLastSync(new Date());
    } catch (e) {
      const status = e.response?.status;
      if (status === 409) toast.info(e.response?.data?.message || 'Sync already running');
      else toast.error(e.response?.data?.error || 'Sync failed');
    } finally { setSyncing(false); }
  };

  return (
    <div>
      {/* Head */}
      <div className="detail-head" style={{ marginBottom: 16 }}>
        <div>
          <div className="dtitle">Settings</div>
          <div className="ddesc">Global rules and defaults that apply across all accounts</div>
        </div>
        <div className="dactions">
          <button className="btn" onClick={syncNow} disabled={syncing}>
            <RefreshCw size={13} /> {syncing ? 'Syncing…' : 'Sync now'}
          </button>
        </div>
      </div>

      <div className="set-grid">

        {/* ── Email notifications ── */}
        <div className="set-block">
          <div className="set-h">Email notifications</div>
          <div className="set-sub">When should we ping you?</div>

          <div className="set-row">
            <div><div className="set-t">Recurring digest</div><div className="set-d">Summary of every account &amp; segment</div></div>
            <Switch on={s.emailDigest} onChange={(v) => set('emailDigest', v)} />
          </div>
          <div className="set-row">
            <div><div className="set-t">Digest frequency</div><div className="set-d">How often the summary lands</div></div>
            <div className="segctrl">
              <button className={s.digestFreq === 'daily'   ? 'active' : ''} onClick={() => set('digestFreq', 'daily')}>Daily</button>
              <button className={s.digestFreq === 'weekly'  ? 'active' : ''} onClick={() => set('digestFreq', 'weekly')}>Weekly</button>
              <button className={s.digestFreq === 'monthly' ? 'active' : ''} onClick={() => set('digestFreq', 'monthly')}>Monthly</button>
            </div>
          </div>
          <div className="set-row">
            <div><div className="set-t">Alert on over-pace</div><div className="set-d">When any account or segment crosses +{s.threshold}%</div></div>
            <Switch on={s.emailOver} onChange={(v) => set('emailOver', v)} />
          </div>
          <div className="set-row">
            <div><div className="set-t">Alert on under-pace</div><div className="set-d">When any account or segment crosses −{s.threshold}%</div></div>
            <Switch on={s.emailUnder} onChange={(v) => set('emailUnder', v)} />
          </div>
          <div className="set-row" style={{ borderBottom: 'none' }}>
            <div><div className="set-t">Campaign running when paused</div><div className="set-d">Get pinged when the script catches one</div></div>
            <Switch on={s.emailRunning} onChange={(v) => set('emailRunning', v)} />
          </div>
        </div>

        {/* ── Pacing defaults ── */}
        <div className="set-block">
          <div className="set-h">Pacing defaults</div>
          <div className="set-sub">Applies to new accounts and segments</div>

          <div className="set-row">
            <div><div className="set-t">Off-pace threshold</div><div className="set-d">±% before flagging an account or segment</div></div>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <input
                type="range"
                className="range-input"
                min="2" max="15" step="1"
                value={s.threshold}
                onChange={e => set('threshold', +e.target.value)}
              />
              <span className="mono" style={{ fontSize: 12, minWidth: 36, textAlign: 'right' }}>±{s.threshold}%</span>
            </div>
          </div>
          <div className="set-row">
            <div><div className="set-t">Default: stop at 100% pace</div><div className="set-d">Cap new accounts at their monthly budget</div></div>
            <Switch on={s.defaultCap} onChange={(v) => set('defaultCap', v)} />
          </div>
          <div className="set-row">
            <div><div className="set-t">Timezone</div><div className="set-d">Used for "today" and daily roll-ups</div></div>
            <select className="hifi-select" value={s.timezone} onChange={e => set('timezone', e.target.value)}>
              <option>America/Chicago</option>
              <option>America/New_York</option>
              <option>America/Denver</option>
              <option>America/Los_Angeles</option>
            </select>
          </div>
          <div className="set-row" style={{ borderBottom: 'none' }}>
            <div><div className="set-t">Fiscal month starts</div><div className="set-d">When the pacing month rolls over</div></div>
            <select className="hifi-select" value={s.fiscalStart} onChange={e => set('fiscalStart', e.target.value)}>
              <option value="1">1st of the month</option>
              <option value="15">15th of the month</option>
              <option value="-1">Last Monday</option>
            </select>
          </div>
        </div>

        {/* ── Google Ads connection ── */}
        <div className="set-block">
          <div className="set-h">Google Ads connection</div>
          <div className="set-sub">Linked accounts, sync status, and Google Sheets</div>

          {/* OAuth row */}
          <div className="set-row">
            <div>
              <div className="set-t">Google account</div>
              <div className="set-d">
                {googleConnected
                  ? 'Connected — enables live spend syncing and budget updates'
                  : 'Not connected — connect to enable live spend syncing'}
              </div>
            </div>
            {googleConnected ? (
              <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                <span className="pill ok" style={{ fontSize: 11 }}>Connected</span>
                <button className="btn ghost small" onClick={disconnectGoogle}><Link2Off size={12} /> Disconnect</button>
              </div>
            ) : (
              <button className="btn primary small" onClick={connectGoogle} disabled={connectingGoogle}>
                <Link2 size={12} /> {connectingGoogle ? 'Redirecting…' : 'Connect Google'}
              </button>
            )}
          </div>

          {/* Sync frequency */}
          <div className="set-row">
            <div><div className="set-t">Sync frequency</div><div className="set-d">How often we pull spend from Google Ads</div></div>
            <div className="segctrl">
              <button className={s.syncFreq === 'hourly' ? 'active' : ''} onClick={() => set('syncFreq', 'hourly')}>Hourly</button>
              <button className={s.syncFreq === '6h'     ? 'active' : ''} onClick={() => set('syncFreq', '6h')}>6×/day</button>
              <button className={s.syncFreq === 'daily'  ? 'active' : ''} onClick={() => set('syncFreq', 'daily')}>Daily</button>
            </div>
          </div>

          {/* Last sync */}
          <div className="set-row">
            <div><div className="set-t">Last sync</div><div className="set-d">Spend captured through yesterday 11:59 PM</div></div>
            <span className="label-sm">{lastSync ? `${Math.round((Date.now() - lastSync) / 60000)}m ago` : 'unknown'}</span>
          </div>

          {/* Shared Google Sheet */}
          <div className="set-row" style={{ flexDirection: 'column', alignItems: 'flex-start', gap: 8, borderBottom: 'none' }}>
            <div>
              <div className="set-t">Shared Google Sheet</div>
              <div className="set-d">All accounts use the same pacing sheet — paste the Sheet ID here and apply it to all at once.</div>
            </div>
            <div style={{ display: 'flex', gap: 8, width: '100%' }}>
              <input
                className="bb-input"
                style={{ flex: 1 }}
                value={sharedSheetId}
                onChange={e => setSharedSheetId(e.target.value)}
                placeholder="Paste Sheet ID or full URL"
              />
              <button className="bb-btn bb-btn-primary" onClick={applySheetToAll} disabled={applyingSheet || !sharedSheetId.trim()}>
                {applyingSheet ? 'Applying…' : 'Apply to all'}
              </button>
            </div>
          </div>
        </div>

        {/* ── AI Summaries ── */}
        <div className="set-block">
          <div className="set-h">AI monthly summaries</div>
          <div className="set-sub">Paste your Anthropic API key to unlock one-click narrative summaries per account</div>

          <div className="set-row" style={{ flexDirection: 'column', alignItems: 'flex-start', gap: 8, borderBottom: 'none' }}>
            <div>
              <div className="set-t">Anthropic API key</div>
              <div className="set-d">
                {anthropicKeyHint
                  ? <>Key saved ({anthropicKeyHint}) — paste a new one to replace it</>
                  : <>Get yours at <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noreferrer">console.anthropic.com</a></>}
              </div>
            </div>
            <div style={{ display: 'flex', gap: 8, width: '100%' }}>
              <input
                className="bb-input"
                style={{ flex: 1 }}
                type="password"
                value={anthropicKey}
                onChange={e => setAnthropicKey(e.target.value)}
                placeholder="sk-ant-…"
              />
              <button
                className="bb-btn bb-btn-primary"
                onClick={saveAnthropicKey}
                disabled={savingKey || !anthropicKey.trim()}
              >
                {savingKey ? 'Saving…' : 'Save key'}
              </button>
            </div>
          </div>
        </div>

        {/* ── Recipients ── */}
        <div className="set-block">
          <div className="set-h">Recipients &amp; team</div>
          <div className="set-sub">Who else gets pinged?</div>

          <div className="set-row">
            <div>
              <div className="set-t">you@commitagency.com</div>
              <div className="set-d">Admin · all alerts · daily digest</div>
            </div>
            <span className="pill neutral">Primary</span>
          </div>
          <div className="set-row">
            <div>
              <div className="set-t">team-leads@commitagency.com</div>
              <div className="set-d">Digest only · weekly</div>
            </div>
            <button className="btn ghost small">Edit</button>
          </div>
          <div className="set-row">
            <div>
              <div className="set-t">alerts@commitagency.com</div>
              <div className="set-d">Over-pace alerts only</div>
            </div>
            <button className="btn ghost small">Edit</button>
          </div>
          <div className="set-row" style={{ justifyContent: 'flex-start', borderBottom: 'none' }}>
            <button className="btn small"><Plus size={12} /> Add recipient</button>
          </div>
        </div>

      </div>
    </div>
  );
}
