import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import axios from 'axios';
import { useToast } from '../components/Toast';

export default function History() {
  const { id } = useParams();
  const { addToast } = useToast();
  const [account, setAccount] = useState(null);
  const [runs, setRuns] = useState([]);
  const [adjustments, setAdjustments] = useState([]);
  const [tab, setTab] = useState('runs');
  const [loading, setLoading] = useState(true);

  useEffect(() => {
    Promise.all([
      axios.get(`/api/accounts/${id}`),
      axios.get(`/api/history/${id}/pacing-runs`),
      axios.get(`/api/history/${id}/adjustments`),
    ]).then(([accR, runsR, adjR]) => {
      setAccount(accR.data.account);
      setRuns(runsR.data.runs || []);
      setAdjustments(adjR.data.adjustments || []);
    }).catch(() => addToast('Failed to load history', 'error'))
      .finally(() => setLoading(false));
  }, [id]);

  if (loading) return <p className="bb-muted">Loading…</p>;

  return (
    <div>
      <h1 className="bb-page-title" style={{ marginBottom: '8px' }}>History {account ? `— ${account.account_name}` : ''}</h1>

      <div className="bb-tabs" style={{ marginBottom: '20px' }}>
        <button className={`bb-tab-btn${tab === 'runs' ? ' is-active' : ''}`} onClick={() => setTab('runs')}>
          Pacing Runs ({runs.length})
        </button>
        <button className={`bb-tab-btn${tab === 'adjustments' ? ' is-active' : ''}`} onClick={() => setTab('adjustments')}>
          Budget Adjustments ({adjustments.length})
        </button>
      </div>

      {tab === 'runs' && (
        <div className="bb-card">
          {runs.length === 0 ? <p className="bb-muted">No pacing runs yet.</p> : (
            <table className="bb-table" style={{ width: '100%' }}>
              <thead>
                <tr><th>Date</th><th>Type</th><th>Campaigns</th><th>Adjustments</th><th>Status</th><th>Triggered By</th></tr>
              </thead>
              <tbody>
                {runs.map(r => (
                  <tr key={r.id}>
                    <td className="bb-muted" style={{ fontSize: '13px' }}>{r.run_at?.slice(0, 16).replace('T', ' ')}</td>
                    <td><span className="bb-pill bb-pill-muted" style={{ fontSize: '11px' }}>{r.run_type}</span></td>
                    <td>{r.campaigns_processed ?? '—'}</td>
                    <td>{r.adjustments_made ?? '—'}</td>
                    <td>
                      <span className={`bb-pill ${r.status === 'COMPLETED' ? 'bb-pill-on' : 'bb-pill-down'}`} style={{ fontSize: '11px' }}>
                        {r.status}
                      </span>
                    </td>
                    <td className="bb-muted" style={{ fontSize: '13px' }}>{r.triggered_by || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}

      {tab === 'adjustments' && (
        <div className="bb-card">
          {adjustments.length === 0 ? <p className="bb-muted">No budget adjustments yet.</p> : (
            <table className="bb-table" style={{ width: '100%' }}>
              <thead>
                <tr><th>Date</th><th>Campaign</th><th>Old Daily</th><th>New Daily</th><th>Change</th><th>Applied By</th></tr>
              </thead>
              <tbody>
                {adjustments.map(a => (
                  <tr key={a.id}>
                    <td className="bb-muted" style={{ fontSize: '13px' }}>{a.applied_at?.slice(0, 16).replace('T', ' ')}</td>
                    <td>{a.campaign_id}</td>
                    <td>${a.old_budget?.toFixed(2)}</td>
                    <td style={{ fontWeight: 600 }}>${a.new_budget?.toFixed(2)}</td>
                    <td style={{ color: a.change_percent > 0 ? 'var(--color-warning)' : 'var(--color-danger)' }}>
                      {a.change_percent > 0 ? '+' : ''}{a.change_percent?.toFixed(1)}%
                    </td>
                    <td className="bb-muted" style={{ fontSize: '13px' }}>{a.applied_by || '—'}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      )}
    </div>
  );
}
