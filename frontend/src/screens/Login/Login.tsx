import { useState, type FormEvent } from 'react';
import { useNavigate } from 'react-router-dom';
import { useDevice } from '../../components/device';
import { Poster } from '../../components/Poster';
import { ScanFace } from 'lucide-react';
import { useSession } from '../../lib/session';
import './Login.css';

export function Login() {
  const device = useDevice();
  const navigate = useNavigate();
  const { signIn } = useSession();

  const [identifier, setIdentifier] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState('');
  const [submitting, setSubmitting] = useState(false);

  const doSignIn = async () => {
    if (submitting) return;
    setError('');
    setSubmitting(true);
    try {
      await signIn(identifier.trim(), password);
      navigate('/profiles');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Sign in failed. Try again.');
    } finally {
      setSubmitting(false);
    }
  };

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    void doSignIn();
  };

  const loginForm = (
    <form className="login-form" onSubmit={onSubmit}>
      <label className="field">
        <span className="field-label">Email or username</span>
        <input
          className="field-input"
          type="text"
          autoComplete="username"
          placeholder="you@email.com or username"
          value={identifier}
          onChange={(e) => setIdentifier(e.target.value)}
        />
      </label>
      <label className="field">
        <span className="field-label">Password</span>
        <input
          className="field-input"
          type="password"
          autoComplete="current-password"
          placeholder="••••••••"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
        />
      </label>
      {error && <p className="login-error">{error}</p>}
      <button type="button" className="login-forgot">Forgot password?</button>
      <button type="submit" className="pill-btn accent login-submit" disabled={submitting}>
        {submitting ? 'Signing in…' : 'Sign In'}
      </button>
    </form>
  );

  // ---------------- MOBILE: email/username + password + biometrics ----------------
  if (device === 'mobile') {
    return (
      <div className="screen login mobile">
        <div className="login-bg">
          <Poster hueA={220} hueB={285} ratio="2/3" rounded={0} showLabel={false} style={{ height: '100%' }} />
          <div className="login-scrim" />
        </div>

        <div className="login-content">
          <div className="brand-mark login-brand"><span className="brand-dot" /> streamAI</div>
          <h1 className="login-title">Welcome back</h1>
          <p className="login-sub">Sign in to pick up where you left off.</p>

          {loginForm}

          <div className="login-or"><span>or</span></div>

          <button className="biometric" onClick={() => void doSignIn()} disabled={submitting}>
            <ScanFace className="bio-ic" size={22} />
            Use Face ID
          </button>

          {/* Self sign-up is disabled — this is intentionally inert. */}
          <p className="login-signup">New here? <b>Sign up now</b></p>
        </div>
      </div>
    );
  }

  // ---------------- TV/desktop: email/username + password ----------------
  return (
    <div className="screen login tv">
      <div className="login-bg">
        <Poster hueA={220} hueB={285} ratio="16/9" rounded={0} showLabel={false} style={{ height: '100%' }} />
        <div className="login-scrim tv" />
      </div>

      <div className="login-tv-content">
        <div className="brand-mark login-brand"><span className="brand-dot" /> streamAI</div>
        <h1 className="login-title">Sign in to your account</h1>
        <div className="tv-form">{loginForm}</div>
      </div>
    </div>
  );
}
