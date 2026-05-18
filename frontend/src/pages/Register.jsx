import { useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import { Activity } from 'lucide-react';
import axios from 'axios';
import { useAuth } from '../App';

export default function Register() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirm, setConfirm] = useState('');
  const [inviteCode, setInviteCode] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const { login } = useAuth();
  const navigate = useNavigate();

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    if (password !== confirm) { setError('Passwords do not match'); return; }
    if (password.length < 8) { setError('Password must be at least 8 characters'); return; }
    setLoading(true);
    try {
      const r = await axios.post('/api/auth/register', { email, password, invite_code: inviteCode });
      login(r.data.user);
      navigate('/');
    } catch (err) {
      setError(err.response?.data?.error || 'Registration failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="bb-auth-page">
      <div className="bb-auth-card">
        <div className="bb-auth-brand">
          <span className="bb-brand-pill"><Activity size={20} /></span>
          <h1 className="bb-page-title" style={{ marginBottom: 0 }}>Google BudgetBuddy</h1>
        </div>
        <p className="bb-muted" style={{ textAlign: 'center', marginBottom: '24px' }}>Create your account</p>

        {error && <div className="bb-alert bb-alert-error">{error}</div>}

        <form onSubmit={handleSubmit}>
          <div className="bb-form-group">
            <label className="bb-form-label">Email</label>
            <input className="bb-input" type="email" value={email} onChange={e => setEmail(e.target.value)} required autoFocus />
          </div>
          <div className="bb-form-group">
            <label className="bb-form-label">Password</label>
            <input className="bb-input" type="password" value={password} onChange={e => setPassword(e.target.value)} required />
          </div>
          <div className="bb-form-group">
            <label className="bb-form-label">Confirm Password</label>
            <input className="bb-input" type="password" value={confirm} onChange={e => setConfirm(e.target.value)} required />
          </div>
          <div className="bb-form-group">
            <label className="bb-form-label">Invite Code</label>
            <input className="bb-input" type="text" value={inviteCode} onChange={e => setInviteCode(e.target.value)} placeholder="Ask a teammate for the code" />
          </div>
          <button className="bb-btn bb-btn-primary" type="submit" disabled={loading} style={{ width: '100%', marginTop: '8px' }}>
            {loading ? 'Creating account…' : 'Create account'}
          </button>
        </form>

        <p className="bb-muted" style={{ textAlign: 'center', marginTop: '16px', fontSize: '14px' }}>
          Already have an account? <Link to="/login" style={{ color: 'var(--color-primary)' }}>Sign in</Link>
        </p>
      </div>
    </div>
  );
}
