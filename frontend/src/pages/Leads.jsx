import { useState, useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { Download, Search } from 'lucide-react';
import axios from 'axios';
import { useToast } from '../components/Toast';

export default function Leads() {
  const { id } = useParams();
  const { addToast } = useToast();
  const [account, setAccount] = useState(null);
  const [leads, setLeads] = useState([]);
  const [loading, setLoading] = useState(false);
  const [startDate, setStartDate] = useState(() => {
    const d = new Date();
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-01`;
  });
  const [endDate, setEndDate] = useState(() => {
    const d = new Date();
    d.setDate(d.getDate() - 1);
    return d.toISOString().slice(0, 10);
  });

  useEffect(() => {
    axios.get(`/api/accounts/${id}`).then(r => setAccount(r.data.account)).catch(() => {});
  }, [id]);

  const pullLeads = async () => {
    setLoading(true);
    setLeads([]);
    try {
      const r = await axios.get(`/api/leads/${id}/pull`, { params: { start_date: startDate, end_date: endDate } });
      setLeads(r.data.leads || []);
      addToast(`Pulled ${r.data.count} lead(s)`, 'success');
    } catch (e) {
      addToast(e.response?.data?.error || 'Failed to pull leads', 'error');
    } finally {
      setLoading(false);
    }
  };

  const exportCsv = () => {
    const url = `/api/leads/${id}/export?start_date=${startDate}&end_date=${endDate}`;
    window.open(url, '_blank');
  };

  return (
    <div>
      <h1 className="bb-page-title" style={{ marginBottom: '8px' }}>Leads {account ? `— ${account.account_name}` : ''}</h1>
      <p className="bb-muted" style={{ marginBottom: '24px' }}>Pull and export Google Ads lead form submissions.</p>

      <div className="bb-card" style={{ marginBottom: '24px' }}>
        <div className="bb-row" style={{ gap: '16px', alignItems: 'flex-end', flexWrap: 'wrap' }}>
          <div className="bb-form-group" style={{ margin: 0 }}>
            <label className="bb-form-label">Start Date</label>
            <input className="bb-input" type="date" value={startDate} onChange={e => setStartDate(e.target.value)} />
          </div>
          <div className="bb-form-group" style={{ margin: 0 }}>
            <label className="bb-form-label">End Date</label>
            <input className="bb-input" type="date" value={endDate} onChange={e => setEndDate(e.target.value)} />
          </div>
          <button className="bb-btn bb-btn-primary" onClick={pullLeads} disabled={loading}>
            <Search size={15} /> {loading ? 'Pulling…' : 'Pull Leads'}
          </button>
          {leads.length > 0 && (
            <button className="bb-btn bb-btn-secondary" onClick={exportCsv}>
              <Download size={15} /> Export CSV
            </button>
          )}
        </div>
      </div>

      {leads.length > 0 ? (
        <div className="bb-card">
          <p className="bb-section-meta" style={{ marginBottom: '16px' }}>{leads.length} lead(s)</p>
          <table className="bb-table" style={{ width: '100%' }}>
            <thead>
              <tr>
                <th>Submitted</th>
                <th>Name</th>
                <th>Email</th>
                <th>Phone</th>
                <th>City</th>
              </tr>
            </thead>
            <tbody>
              {leads.map((lead, i) => (
                <tr key={i}>
                  <td className="bb-muted" style={{ fontSize: '13px' }}>{lead.submitted_at?.slice(0, 10)}</td>
                  <td>{lead.name || '—'}</td>
                  <td>{lead.email || '—'}</td>
                  <td>{lead.phone || '—'}</td>
                  <td>{lead.city || '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        !loading && <p className="bb-muted">Select a date range and click Pull Leads to view submissions.</p>
      )}
    </div>
  );
}
