import { useEffect, useRef, useState } from 'react';

import { api, ApiError } from '../lib/api';
import { useStore } from '../state/store';

/**
 * The password screen.
 *
 * Only ever seen when JARVIS is reachable from somewhere other than the
 * machine it runs on. In local mode the backend issues a session to loopback
 * requests transparently and this never appears.
 *
 * It exists because the backend has always supported a password mode and the
 * interface never did: turning that mode on would have locked the owner out
 * of their own assistant with no way back in short of editing a config file.
 *
 * The session is an httpOnly cookie set by the backend, so there is no token
 * for this component to hold, store or leak -- it posts a password once and
 * lets the store start over with the cookie attached.
 */
export function Login() {
  const signedIn = useStore((s) => s.signedIn);
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => { inputRef.current?.focus(); }, []);

  const submit = async (event: React.FormEvent) => {
    event.preventDefault();
    if (!password || busy) return;

    setBusy(true);
    setError('');
    try {
      await api.post('/auth/login', { password });
      await signedIn();
    } catch (exc) {
      // The backend's own wording is the useful part here: it distinguishes
      // "no password configured" from "wrong password" from "local mode only
      // accepts this machine", and each needs a different thing done about it.
      setError(exc instanceof ApiError ? exc.message : 'Could not sign in.');
      setPassword('');
      inputRef.current?.focus();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <form className="login__box" onSubmit={submit}>
        <div className="login__mark">JARVIS</div>
        <p className="login__hint">
          This assistant is reachable from another device, so it asks for a password.
        </p>

        <input
          ref={inputRef}
          className="login__input"
          type="password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          placeholder="Password"
          aria-label="Password"
          autoComplete="current-password"
          disabled={busy}
        />

        {error && <div className="login__error">{error}</div>}

        <button className="btn login__go" type="submit" disabled={busy || !password}>
          {busy ? 'Checking…' : 'Unlock'}
        </button>
      </form>
    </div>
  );
}
