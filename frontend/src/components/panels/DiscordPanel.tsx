import { useCallback, useEffect, useState } from 'react';

import { api, ApiError } from '../../lib/api';

/**
 * JARVIS on Discord, through your own bot.
 *
 * The token is pasted once and sealed on this PC; it never comes back to the
 * browser. Only the Discord accounts you pair get JARVIS: you DM the bot the
 * code shown here. Everything here can also be said: "pair Discord", "turn
 * off Discord", "unpair Discord", "answer me in Discord servers too".
 */

interface Status {
  token_saved: boolean;
  token_storage: string;
  enabled: boolean;
  connected: boolean;
  bot: string;
  invite_url: string;
  servers: boolean;
  notify: boolean;
  server_count: number;
  owners: { id: string; name: string }[];
  pairing: boolean;
  allowed: boolean;
  error: string;
}

export function DiscordPanel() {
  const [status, setStatus] = useState<Status | null>(null);
  const [token, setToken] = useState('');
  const [code, setCode] = useState<{ code: string; minutes: number; bot: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState('');

  const load = useCallback(async () => {
    try {
      setStatus(await api.get<Status>('/discord'));
    } catch (error) {
      setProblem((error as Error).message);
    }
  }, []);

  useEffect(() => {
    void load();
    // Connecting takes a few seconds after a token is saved.
    const timer = window.setInterval(() => void load(), 4000);
    return () => window.clearInterval(timer);
  }, [load]);

  const run = async (work: () => Promise<unknown>) => {
    setBusy(true);
    setProblem('');
    try {
      const result = await work();
      if (result && typeof result === 'object' && 'connected' in (result as Status)) {
        setStatus(result as Status);
      }
    } catch (error) {
      setProblem(error instanceof ApiError ? error.message : (error as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const saveToken = () => run(async () => {
    const result = await api.post<Status>('/discord/token', { token });
    setToken('');
    return result;
  });

  const pair = () => run(async () => {
    setCode(await api.post<{ code: string; minutes: number; bot: string }>('/discord/pair', {}));
  });

  if (!status) {
    return <div className="panel__section"><div className="control__help">{problem || 'Loading…'}</div></div>;
  }

  return (
    <>
      <div className="panel__section">
        <div className="control__help">
          Chat with JARVIS from Discord: message your bot and the same JARVIS answers, with the
          same tools and the same permissions. Only the accounts you pair get an answer.
          Anything that needs your OK is asked in the chat, and only your “yes” approves it.
        </div>
        {!status.allowed && (
          <div className="control__help control__help--warn">
            “Chat through Discord” is switched off in Permissions, so the bot won't answer anyone.
          </div>
        )}
      </div>

      {!status.token_saved ? (
        <div className="panel__section">
          <ol className="mail-steps">
            <li>
              Open the <a href="https://discord.com/developers/applications" target="_blank"
                          rel="noreferrer">Discord Developer Portal</a> and choose
              <strong> New Application</strong>. Name it JARVIS.
            </li>
            <li>Under <strong>Bot</strong>, press <strong>Reset Token</strong> and copy the token.</li>
            <li>Paste it here. It's sealed on this PC and never shown again.</li>
          </ol>
          <label className="control__label" htmlFor="discord-token">Bot token</label>
          <input id="discord-token" className="text-input mail-input" type="password"
                 autoComplete="off" spellCheck={false} value={token}
                 onChange={(event) => setToken(event.target.value)}
                 onKeyDown={(event) => { if (event.key === 'Enter' && token.trim() && !busy) void saveToken(); }} />
          <div className="mail-foot">
            <button className="btn" disabled={!token.trim() || busy} onClick={() => void saveToken()}>
              {busy ? 'Saving…' : 'Save and connect'}
            </button>
          </div>
        </div>
      ) : (
        <>
          <div className="panel__section">
            <div className="control__label">Bot</div>
            <div className="control__help">
              {status.connected
                ? `Online as ${status.bot}, in ${status.server_count} server${status.server_count === 1 ? '' : 's'}.`
                : status.enabled ? (status.error || 'Connecting…') : 'Off.'}
              {' '}Token: {status.token_storage}.
            </div>
            <div className="plugin__switches">
              <label className="plugin__switch">
                <button className={`switch ${status.enabled ? 'switch--on' : ''}`} role="switch"
                        aria-checked={status.enabled} disabled={busy}
                        onClick={() => void run(() => api.post('/discord/enabled', { on: !status.enabled }))} />
                <span>On</span>
              </label>
              <label className="plugin__switch"
                     title="Replies in a server are visible to everyone in that channel.">
                <button className={`switch ${status.servers ? 'switch--on' : ''}`} role="switch"
                        aria-checked={status.servers} disabled={busy}
                        onClick={() => void run(() => api.post('/discord/servers', { on: !status.servers }))} />
                <span>Also answer @mentions in servers</span>
              </label>
              <label className="plugin__switch"
                     title="Reminders, finished research and other notifications, as a DM from your bot. Their text goes through Discord.">
                <button className={`switch ${status.notify ? 'switch--on' : ''}`} role="switch"
                        aria-checked={status.notify} disabled={busy || status.owners.length === 0}
                        onClick={() => void run(() => api.post('/discord/notify', { on: !status.notify }))} />
                <span>Send my notifications here</span>
              </label>
            </div>
            {status.connected && status.server_count === 0 && (
              <div className="control__help">
                Discord only lets you message a bot you share a server with.{' '}
                <a href={status.invite_url} target="_blank" rel="noreferrer">Add it to a server of yours</a>,
                then DM it.
              </div>
            )}
          </div>

          <div className="panel__section">
            <div className="control__label">Paired accounts</div>
            {status.owners.length === 0 && (
              <div className="control__help">None yet: nobody can use JARVIS on Discord.</div>
            )}
            {status.owners.map((owner) => (
              <div className="mail-row" key={owner.id}>
                <span>{owner.name || owner.id}</span>
                <button className="mini-action" disabled={busy}
                        onClick={() => void run(() => api.del(`/discord/owners/${owner.id}`))}>
                  unpair
                </button>
              </div>
            ))}
            {code ? (
              <div className="control__help">
                DM {code.bot}: <strong>pair {code.code}</strong> (works for {code.minutes} minutes).
              </div>
            ) : null}
            <div className="mail-foot">
              <button className="btn" disabled={!status.connected || busy} onClick={() => void pair()}>
                {status.owners.length ? 'Pair another account' : 'Pair my Discord account'}
              </button>
              <button className="mini-action" disabled={busy}
                      onClick={() => void run(() => api.del('/discord/token'))}>
                forget the token
              </button>
            </div>
          </div>
        </>
      )}

      {problem && <div className="panel__section"><div className="control__help control__help--warn">{problem}</div></div>}
    </>
  );
}
