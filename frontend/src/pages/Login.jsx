import { useState } from 'react';
import { useNavigate, Link } from 'react-router-dom';
import axios from 'axios';
import { useAuth } from '../App';
import Logo from '../components/Logo';

export default function Login() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const { login } = useAuth();
  const navigate = useNavigate();

  const handleSubmit = async (e) => {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      const r = await axios.post('/api/auth/login', { email, password });
      login(r.data.user);
      navigate('/');
    } catch (err) {
      setError(err.response?.data?.error || 'Login failed');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="bb-auth-page">
      <div className="bb-auth-card">
        <div className="bb-auth-brand">
          <Logo size={56} style={{ margin: '0 auto 8px' }} />
          <h1 className="bb-page-title" style={{ marginBottom: 0 }}>Google BudgetBuddy</h1>
        </div>
        <p className="bb-muted" style={{ textAlign: 'center', marginBottom: '24px' }}>Sign in to your account</p>

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
          <button className="bb-btn bb-btn-primary" type="submit" disabled={loading} style={{ width: '100%', marginTop: '8px' }}>
            {loading ? 'Signing in…' : 'Sign in'}
          </button>
        </form>

        <p className="bb-muted" style={{ textAlign: 'center', marginTop: '16px', fontSize: '14px' }}>
          Don't have an account? <Link to="/register" style={{ color: 'var(--color-primary)' }}>Register</Link>
        </p>
      </div>
    </div>
  );
}
