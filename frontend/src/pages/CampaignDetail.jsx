import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { ArrowLeft, Save } from 'lucide-react';
import axios from 'axios';
import { useToast } from '../components/Toast';
import SpendChart from '../components/SpendChart';

export default function CampaignDetail() {
  const { id } = useParams();
  const navigate = useNavigate();
  const { addToast } = useToast();

  const [campaign, setCampaign] = useState(null);
  const [history, setHistory] = useState([]);
  const [monthlyBudget, setMonthlyBudget] = useState('');
  const [flightType, setFlightType] = useState('ALWAYS_ON');
  const [flightStart, setFlightStart] = useState('');
  const [flightEnd, setFlightEnd] = useState('');
  const [saving, setSaving] = useState(false);
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      axios.get(`/api/campaigns/${id}`),
      axios.get(`/api/campaigns/${id}/pacing-history`),
    ]).then(([campR, histR]) => {
      const c = campR.data.campaign;
      setCampaign(c);
      setMonthlyBudget(c.monthly_budget?.toString() || '0');
      setFlightType(c.flight_type || 'ALWAYS_ON');
      setFlightStart(c.flight_start_date || '');
      setFlightEnd(c.flight_end_date || '');
      setHistory(histR.data.history || []);
    }).catch(() => addToast('Failed to load campaign', 'error'))
      .finally(() => setLoading(false));
  }, [id]);

  const save = async () => {
    setSaving(true);
    try {
      await axios.put(`/api/campaigns/${id}`, {
        monthly_budget: parseFloat(monthlyBudget) || 0,
        flight_type: flightType,
        flight_start_date: flightType === 'LIMITED' ? flightStart : null,
        flight_end_date: flightType === 'LIMITED' ? flightEnd : null,
      });
      addToast('Campaign updated', 'success');
    } catch (e) {
      addToast(e.response?.data?.error || 'Save failed', 'error');
    } finally {
      setSaving(false);
    }
  };

  const removeCampaign = async () => {
    if (!confirm(`Remove "${campaign.campaign_name}" from tracking?`)) return;
    try {
      await axios.delete(`/api/campaigns/${id}`);
      addToast('Campaign removed', 'info');
      navigate(-1);
    } catch {
      addToast('Remove failed', 'error');
    }
  };

  if (loading) return <p className="bb-muted">Loading…</p>;
  if (!campaign) return <div className="bb-alert bb-alert-error">Campaign not found</div>;

  const latestPacing = campaign.latest_pacing;

  return (
    <div>
      <button className="bb-btn bb-btn-ghost" onClick={() => navigate(-1)} style={{ marginBottom: '16px' }}>
        <ArrowLeft size={15} /> Back
      </button>

      <h1 className="bb-page-title" style={{ marginBottom: '4px' }}>{campaign.campaign_name}</h1>
      <p className="bb-muted" style={{ marginBottom: '24px', fontSize: '13px' }}>Campaign ID: {campaign.google_campaign_id}</p>

      {/* Current pacing stats */}
      {latestPacing && (
        <div className="bb-grid" style={{ gridTemplateColumns: 'repeat(4, 1fr)', marginBottom: '24px' }}>
          <div className="bb-card bb-stat-tile">
            <p className="bb-section-meta">MTD Spend</p>
            <p className="bb-stat-value">${latestPacing.actual_spend?.toFixed(2)}</p>
          </div>
          <div className="bb-card bb-stat-tile">
            <p className="bb-section-meta">Expected</p>
            <p className="bb-stat-value">${latestPacing.expected_spend?.toFixed(2)}</p>
          </div>
          <div className="bb-card bb-stat-tile">
            <p className="bb-section-meta">Pace</p>
            <p className="bb-stat-value">{((latestPacing.pace_ratio || 1) * 100).toFixed(1)}%</p>
          </div>
          <div className="bb-card bb-stat-tile">
            <p className="bb-section-meta">Recommended Daily</p>
            <p className="bb-stat-value">${latestPacing.recommended_daily_budget?.toFixed(2)}</p>
          </div>
        </div>
      )}

      {/* Spend chart */}
      {history.length > 0 && (
        <div className="bb-card" style={{ marginBottom: '24px' }}>
          <h2 className="bb-section-title" style={{ marginBottom: '16px' }}>Spend vs. Target</h2>
          <SpendChart history={history} monthlyBudget={campaign.monthly_budget} />
        </div>
      )}

      {/* Campaign settings */}
      <div className="bb-card">
        <h2 className="bb-section-title" style={{ marginBottom: '16px' }}>Campaign Settings</h2>
        <p className="bb-muted" style={{ marginBottom: '16px', fontSize: '13px' }}>
          Note: Monthly budget is normally set by your Google Sheet. Only edit manually if needed.
        </p>

        <div className="bb-form-group">
          <label className="bb-form-label">Monthly Budget ($)</label>
          <input className="bb-input" type="number" step="0.01" min="0" value={monthlyBudget}
            onChange={e => setMonthlyBudget(e.target.value)} style={{ width: '200px' }} />
        </div>

        <div className="bb-form-group">
          <label className="bb-form-label">Flight Type</label>
          <select className="bb-select" value={flightType} onChange={e => setFlightType(e.target.value)} style={{ width: '200px' }}>
            <option value="ALWAYS_ON">Always On</option>
            <option value="LIMITED">Limited Flight</option>
          </select>
        </div>

        {flightType === 'LIMITED' && (
          <div className="bb-row" style={{ gap: '16px' }}>
            <div className="bb-form-group">
              <label className="bb-form-label">Flight Start</label>
              <input className="bb-input" type="date" value={flightStart} onChange={e => setFlightStart(e.target.value)} />
            </div>
            <div className="bb-form-group">
              <label className="bb-form-label">Flight End</label>
              <input className="bb-input" type="date" value={flightEnd} onChange={e => setFlightEnd(e.target.value)} />
            </div>
          </div>
        )}

        <div className="bb-row" style={{ gap: '8px', marginTop: '8px' }}>
          <button className="bb-btn bb-btn-primary" onClick={save} disabled={saving}>
            <Save size={15} /> {saving ? 'Saving…' : 'Save'}
          </button>
          <button className="bb-btn bb-btn-danger" onClick={removeCampaign}>
            Remove from Tracking
          </button>
        </div>
      </div>
    </div>
  );
}
