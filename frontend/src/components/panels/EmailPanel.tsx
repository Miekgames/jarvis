import { useCallback, useEffect, useState } from 'react';

import { api, ApiError } from '../../lib/api';
import { useStore } from '../../state/store';

/**
 * Your email, inside JARVIS.
 *
 * Connected with an app password -- the separate password Gmail, iCloud and
 * Yahoo let you make for one app -- over IMAP and SMTP. Looking never changes
 * the mailbox: folders are opened read-only and nothing is marked as read.
 * The password is sealed on this PC and only ever goes to the provider.
 *
 * Sending from here is your own click (with a final "are you sure"). When
 * JARVIS sends on your behalf from chat, the confirmation dialog shows you the
 * exact message first, and nothing is ever sent while you're away.
 */

interface Provider {
  label: string;
  imap_host: string;
  imap_port: number;
  imap_security: string;
  smtp_host: string;
  smtp_port: number;
  smtp_security: string;
  password_url: string;
  web: string;
  app_password: boolean;
  caveat?: string;
}

interface Status {
  connected: boolean;
  address?: string;
  provider?: string;
  provider_label?: string;
  imap_host?: string;
  smtp_host?: string;
  web?: string;
  can_send?: boolean;
  send_problem?: string;
  password_storage?: string;
  providers?: Record<string, Provider>;
}

interface Summary {
  id: string;
  from: string;
  from_address: string;
  subject: string;
  date: string;
  unread: boolean;
  starred: boolean;
  attachments: boolean;
  snippet: string;
}

interface Full {
  id: string;
  from: string;
  from_address: string;
  reply_to: string;
  to: string[];
  cc: string[];
  subject: string;
  date: string;
  body: string;
  attachments: string[];
  web: string;
}

interface Draft {
  to: string;
  cc: string;
  subject: string;
  body: string;
  reply_to: string;
}

const ORDER = ['gmail', 'icloud', 'yahoo', 'outlook', 'other'];
const DOMAINS: Record<string, string> = {
  'gmail.com': 'gmail', 'googlemail.com': 'gmail',
  'icloud.com': 'icloud', 'me.com': 'icloud', 'mac.com': 'icloud',
  'yahoo.com': 'yahoo', 'yahoo.co.uk': 'yahoo', 'ymail.com': 'yahoo', 'rocketmail.com': 'yahoo',
  'outlook.com': 'outlook', 'hotmail.com': 'outlook', 'hotmail.co.uk': 'outlook',
  'live.com': 'outlook', 'msn.com': 'outlook',
};

/** Tell the sidebar how many are unread, wherever we learned it. */
export function announceUnread(unread: number | null) {
  window.dispatchEvent(new CustomEvent('jarvis:email', { detail: { unread } }));
}

function errorText(exc: unknown) {
  return exc instanceof ApiError ? exc.message : String((exc as Error)?.message ?? exc);
}

export function EmailPanel() {
  const [status, setStatus] = useState<Status | null>(null);
  const [error, setError] = useState('');

  const load = useCallback(async () => {
    try {
      setStatus(await api.get<Status>('/email'));
    } catch (exc) {
      setError(errorText(exc));
    }
  }, []);

  useEffect(() => { void load(); }, [load]);

  if (!status) {
    return error
      ? <div className="panel__section"><div className="control__help control__help--bad">{error}</div></div>
      : <div className="panel__section"><p className="empty">Checking…</p></div>;
  }
  if (!status.connected) {
    return <ConnectForm providers={status.providers ?? {}} onConnected={setStatus} />;
  }
  return <Mailbox status={status} onDisconnected={() => { announceUnread(null); void load(); }} />;
}

// ===========================================================================
function ConnectForm({ providers, onConnected }: {
  providers: Record<string, Provider>;
  onConnected: (status: Status) => void;
}) {
  const [provider, setProvider] = useState('gmail');
  const [picked, setPicked] = useState(false);
  const [address, setAddress] = useState('');
  const [password, setPassword] = useState('');
  const [custom, setCustom] = useState({ imap_host: '', imap_port: '993', smtp_host: '', smtp_port: '465' });
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const preset = providers[provider];

  const onAddress = (value: string) => {
    setAddress(value);
    // Follow the address until the owner picks a provider themselves.
    const guess = DOMAINS[value.split('@')[1]?.trim().toLowerCase() ?? ''];
    if (!picked && guess) setProvider(guess);
  };

  const connect = async () => {
    setBusy(true);
    setError('');
    try {
      const body: Record<string, unknown> = { address, password, provider };
      if (provider === 'other') {
        body.imap_host = custom.imap_host;
        body.imap_port = Number(custom.imap_port) || 993;
        body.smtp_host = custom.smtp_host;
        body.smtp_port = Number(custom.smtp_port) || 465;
        body.smtp_security = Number(custom.smtp_port) === 587 ? 'starttls' : 'ssl';
      }
      const status = await api.post<Status>('/email/connect', body);
      setPassword('');
      onConnected(status);
    } catch (exc) {
      setError(errorText(exc));
    } finally {
      setBusy(false);
    }
  };

  const ready = address.includes('@') && password.trim().length > 0
    && (provider !== 'other' || custom.imap_host.trim().length > 0);

  return (
    <>
      <div className="panel__section">
        <div className="control__help">
          Connect your mailbox and JARVIS can tell you what's new, find things in your email,
          read messages to you, and — only after you approve each one — send email for you.
          Looking never changes your mailbox: nothing is marked as read.
        </div>
      </div>

      <div className="panel__section">
        <div className="mail-form">
          <label className="control__label" htmlFor="mail-provider">Provider</label>
          <select id="mail-provider" className="select" value={provider}
                  onChange={(event) => { setProvider(event.target.value); setPicked(true); }}>
            {ORDER.filter((key) => providers[key]).map((key) => (
              <option key={key} value={key}>{providers[key].label}</option>
            ))}
          </select>

          <label className="control__label" htmlFor="mail-address">Email address</label>
          <input id="mail-address" className="text-input mail-input" type="email"
                 autoComplete="email" placeholder="you@gmail.com" value={address}
                 onChange={(event) => onAddress(event.target.value)} />

          {provider === 'gmail' && (
            <ol className="mail-steps">
              <li>
                Gmail needs <a href="https://myaccount.google.com/signinoptions/twosv"
                               target="_blank" rel="noreferrer">2-Step Verification</a> switched
                on for app passwords.
              </li>
              <li>
                Open <a href="https://myaccount.google.com/apppasswords" target="_blank"
                        rel="noreferrer">myaccount.google.com/apppasswords</a>, type
                <strong> JARVIS</strong> as the name, and press Create.
              </li>
              <li>Paste the 16-letter password below — the spaces don't matter.</li>
            </ol>
          )}
          {provider !== 'gmail' && preset?.password_url && (
            <div className="control__help">
              Use an app password, not your normal one:{' '}
              <a href={preset.password_url} target="_blank" rel="noreferrer">create one here</a>.
            </div>
          )}
          {preset?.caveat && <div className="control__help control__help--warn">{preset.caveat}</div>}

          {provider === 'other' && (
            <div className="mail-servers">
              <input className="text-input" placeholder="IMAP server, e.g. imap.example.com"
                     value={custom.imap_host} aria-label="IMAP server"
                     onChange={(event) => setCustom({ ...custom, imap_host: event.target.value })} />
              <input className="text-input mail-port" inputMode="numeric" value={custom.imap_port}
                     aria-label="IMAP port"
                     onChange={(event) => setCustom({ ...custom, imap_port: event.target.value })} />
              <input className="text-input" placeholder="SMTP server, e.g. smtp.example.com"
                     value={custom.smtp_host} aria-label="SMTP server"
                     onChange={(event) => setCustom({ ...custom, smtp_host: event.target.value })} />
              <input className="text-input mail-port" inputMode="numeric" value={custom.smtp_port}
                     aria-label="SMTP port"
                     onChange={(event) => setCustom({ ...custom, smtp_port: event.target.value })} />
            </div>
          )}

          <label className="control__label" htmlFor="mail-password">
            {provider === 'other' ? 'Password' : 'App password'}
          </label>
          <input id="mail-password" className="text-input mail-input" type="password"
                 autoComplete="off" spellCheck={false} value={password}
                 placeholder={provider === 'gmail' ? 'xxxx xxxx xxxx xxxx' : ''}
                 onChange={(event) => setPassword(event.target.value)}
                 onKeyDown={(event) => { if (event.key === 'Enter' && ready && !busy) void connect(); }} />

          <button className="btn btn--primary" disabled={!ready || busy} onClick={() => void connect()}>
            {busy ? `Signing in to ${preset?.label ?? 'the server'}…` : 'Connect'}
          </button>
          {error && <div className="control__help control__help--bad">{error}</div>}

          <div className="control__help">
            JARVIS signs in for real before saving anything. The password is encrypted with
            Windows on this PC, never shown again, and only ever sent to
            {provider === 'other' ? ' the servers above' : ` ${preset?.imap_host} and ${preset?.smtp_host}`}.
            Remove it any time with Disconnect{provider === 'gmail'
              ? ', or revoke it on Google’s app passwords page' : ''}.
          </div>
        </div>
      </div>
    </>
  );
}

// ===========================================================================
function Mailbox({ status, onDisconnected }: { status: Status; onDisconnected: () => void }) {
  const notify = useStore((s) => s.notify);
  const send = useStore((s) => s.send);
  const setPanel = useStore((s) => s.setPanel);

  const [unreadOnly, setUnreadOnly] = useState(false);
  const [query, setQuery] = useState('');
  const [searched, setSearched] = useState('');
  const [messages, setMessages] = useState<Summary[] | null>(null);
  const [matched, setMatched] = useState(0);
  const [unread, setUnread] = useState<number | null>(null);
  const [open, setOpen] = useState<Full | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const gmail = status.provider === 'gmail';

  const list = useCallback(async () => {
    setBusy(true);
    setError('');
    try {
      const data = searched
        ? await api.get<{ messages: Summary[]; matched: number }>(
          `/email/search?q=${encodeURIComponent(searched)}&limit=25`)
        : await api.get<{ messages: Summary[]; matched: number }>(
          `/email/messages?unread=${unreadOnly}&limit=25`);
      setMessages(data.messages);
      setMatched(data.matched);
      if (!searched && unreadOnly) {
        setUnread(data.matched);
        announceUnread(data.matched);
      }
    } catch (exc) {
      setError(errorText(exc));
    } finally {
      setBusy(false);
    }
  }, [searched, unreadOnly]);

  useEffect(() => { void list(); }, [list]);

  useEffect(() => {
    void api.get<{ unread: number }>('/email/unread')
      .then((data) => { setUnread(data.unread); announceUnread(data.unread); })
      .catch(() => { /* the list shows the error */ });
  }, []);

  const openMessage = async (id: string) => {
    setBusy(true);
    setError('');
    try {
      setOpen(await api.get<Full>(`/email/messages/${encodeURIComponent(id)}`));
    } catch (exc) {
      setError(errorText(exc));
    } finally {
      setBusy(false);
    }
  };

  const disconnect = async () => {
    if (!window.confirm(`Disconnect ${status.address}? JARVIS deletes the saved password and `
      + 'can no longer read or send your email. Your mailbox itself is not touched.')) return;
    await api.del('/email');
    onDisconnected();
  };

  const askJarvis = (message: Full) => {
    setPanel(null);
    void send(`Read my email "${message.subject}" from ${message.from} (id ${message.id}) `
      + 'and tell me what it says and whether it needs a reply.');
  };

  if (draft) {
    return (
      <Compose draft={draft} from={status.address ?? ''} canSend={!!status.can_send}
               problem={status.send_problem ?? ''}
               onCancel={() => setDraft(null)}
               onSent={(to) => { setDraft(null); notify(`Email sent to ${to}.`, 'email'); }} />
    );
  }

  if (open) {
    return (
      <div className="panel__section">
        <button className="mini-action" onClick={() => setOpen(null)}>← Back</button>
        <div className="mail-read">
          <h3 className="mail-read__subject">{open.subject}</h3>
          <div className="mail-read__meta">
            <div><span className="mail-read__key">From</span>{open.from}
              {open.from !== open.from_address && <span className="mail-read__addr"> &lt;{open.from_address}&gt;</span>}
            </div>
            {open.to.length > 0 && <div><span className="mail-read__key">To</span>{open.to.join(', ')}</div>}
            {open.cc.length > 0 && <div><span className="mail-read__key">Cc</span>{open.cc.join(', ')}</div>}
            <div><span className="mail-read__key">Date</span>{longDate(open.date)}</div>
            {open.attachments.length > 0 && (
              <div><span className="mail-read__key">Files</span>{open.attachments.join(', ')}</div>
            )}
          </div>
          <div className="mail-read__actions">
            <button className="btn btn--small" onClick={() => setDraft({
              to: open.reply_to, cc: '', reply_to: open.id, body: '',
              subject: /^re:/i.test(open.subject) ? open.subject : `Re: ${open.subject}`,
            })}>Reply</button>
            <button className="btn btn--small" onClick={() => askJarvis(open)}>Ask JARVIS about it</button>
            {open.web && <a className="mini-action" href={open.web} target="_blank" rel="noreferrer">
              Open {status.provider_label}</a>}
          </div>
          <pre className="mail-read__body">{open.body || '(This email has no text.)'}</pre>
        </div>
      </div>
    );
  }

  return (
    <>
      <div className="panel__section">
        <div className="mail-head">
          <div className="mail-head__who">
            <div className="mail-head__address">{status.address}</div>
            <div className="control__help">
              {status.provider_label}
              {unread != null && ` · ${unread} unread`}
              {status.password_storage && ` · password ${status.password_storage}`}
            </div>
          </div>
          <button className="btn btn--small" onClick={() => setDraft({ to: '', cc: '', subject: '', body: '', reply_to: '' })}>
            Write
          </button>
        </div>
        {!status.can_send && (
          <div className="control__help control__help--warn">
            Reading works; sending doesn't. {status.send_problem}
          </div>
        )}

        <div className="mail-bar">
          <div className="mail-tabs" role="tablist">
            {[false, true].map((onlyUnread) => (
              <button key={String(onlyUnread)} role="tab" aria-selected={!searched && unreadOnly === onlyUnread}
                      className={`mail-tab ${!searched && unreadOnly === onlyUnread ? 'mail-tab--on' : ''}`}
                      onClick={() => { setSearched(''); setQuery(''); setUnreadOnly(onlyUnread); }}>
                {onlyUnread ? 'Unread' : 'Inbox'}
              </button>
            ))}
          </div>
          <button className="mini-action" disabled={busy} onClick={() => void list()}>
            {busy ? 'Loading…' : 'Refresh'}
          </button>
        </div>

        <div className="control" style={{ gap: '0.5rem' }}>
          <input className="text-input" style={{ flex: 1, maxWidth: 'none' }}
                 placeholder={gmail ? 'Search all mail — e.g. from:amazon newer_than:7d'
                   : 'Search sender, subject or text'}
                 value={query} aria-label="Search your email"
                 onChange={(event) => setQuery(event.target.value)}
                 onKeyDown={(event) => { if (event.key === 'Enter') setSearched(query.trim()); }} />
          <button className="btn btn--small" disabled={!query.trim()}
                  onClick={() => setSearched(query.trim())}>Search</button>
        </div>
        {searched && (
          <div className="control__help">
            {matched} match{matched === 1 ? '' : 'es'} for “{searched}”
            {matched > (messages?.length ?? 0) && ` — showing the newest ${messages?.length}`}
            {' · '}<button className="mini-action" onClick={() => { setSearched(''); setQuery(''); }}>clear</button>
          </div>
        )}
        {error && <div className="control__help control__help--bad">{error}</div>}
      </div>

      <div className="panel__section mail-list">
        {messages === null && !error && <p className="empty">Fetching your mail…</p>}
        {messages?.length === 0 && (
          <p className="empty">{searched ? 'Nothing matched.' : unreadOnly ? 'No unread email.' : 'The inbox is empty.'}</p>
        )}
        {messages?.map((message) => (
          <button key={message.id} className={`mail-row ${message.unread ? 'mail-row--unread' : ''}`}
                  onClick={() => void openMessage(message.id)}>
            <div className="mail-row__top">
              {message.unread && <span className="mail-row__dot" aria-label="unread" />}
              <span className="mail-row__from" title={message.from_address}>{message.from}</span>
              {message.attachments && <ClipIcon />}
              {message.starred && <span className="mail-row__star" aria-label="starred">★</span>}
              <span className="mail-row__when">{shortDate(message.date)}</span>
            </div>
            <div className="mail-row__subject">{message.subject}</div>
            {message.snippet && <div className="mail-row__snippet">{message.snippet}</div>}
          </button>
        ))}
      </div>

      <div className="panel__section">
        <div className="mail-foot">
          {status.web && <a className="mini-action" href={status.web} target="_blank" rel="noreferrer">
            Open {status.provider_label}</a>}
          <span style={{ flex: 1 }} />
          <button className="mini-action mini-action--danger" onClick={() => void disconnect()}>
            Disconnect
          </button>
        </div>
        <div className="control__help">
          In chat, try “any new emails?”, “find the email about my flight”, or “reply to Sam
          saying Friday works” — JARVIS shows you any email before it is sent.
        </div>
      </div>
    </>
  );
}

// ===========================================================================
function Compose({ draft, from, canSend, problem, onCancel, onSent }: {
  draft: Draft;
  from: string;
  canSend: boolean;
  problem: string;
  onCancel: () => void;
  onSent: (to: string) => void;
}) {
  const [value, setValue] = useState<Draft>(draft);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  const ready = value.to.includes('@') && value.body.trim().length > 0;

  const submit = async () => {
    if (!window.confirm(`Send this email from ${from} to ${value.to}?`)) return;
    setBusy(true);
    setError('');
    try {
      await api.post('/email/send', value);
      onSent(value.to);
    } catch (exc) {
      setError(errorText(exc));
    } finally {
      setBusy(false);
    }
  };

  const field = (key: keyof Draft, label: string, placeholder = '') => (
    <label className="mail-field">
      <span className="mail-field__label">{label}</span>
      <input className="text-input mail-input" value={value[key]} placeholder={placeholder}
             onChange={(event) => setValue({ ...value, [key]: event.target.value })} />
    </label>
  );

  return (
    <div className="panel__section">
      <button className="mini-action" onClick={onCancel}>← Back</button>
      <div className="mail-compose">
        <div className="control__help">From {from}</div>
        {field('to', 'To', 'name@example.com')}
        {field('cc', 'Cc', 'optional')}
        {field('subject', 'Subject')}
        <textarea className="away__prompt mail-compose__body" rows={10} value={value.body}
                  aria-label="Message" placeholder="Write your message"
                  onChange={(event) => setValue({ ...value, body: event.target.value })} />
        {!canSend && <div className="control__help control__help--warn">This mailbox can't send: {problem}</div>}
        {error && <div className="control__help control__help--bad">{error}</div>}
        <div className="mail-read__actions">
          <button className="btn btn--primary" disabled={!ready || busy || !canSend}
                  onClick={() => void submit()}>{busy ? 'Sending…' : 'Send'}</button>
          <button className="btn" onClick={onCancel}>Discard</button>
        </div>
      </div>
    </div>
  );
}

// ===========================================================================
function shortDate(iso: string) {
  if (!iso) return '';
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return '';
  const now = new Date();
  if (when.toDateString() === now.toDateString()) {
    return when.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }
  const days = (now.getTime() - when.getTime()) / 86_400_000;
  if (days < 6) return when.toLocaleDateString([], { weekday: 'short' });
  return when.toLocaleDateString([], { day: 'numeric', month: 'short',
    ...(when.getFullYear() !== now.getFullYear() ? { year: 'numeric' } : {}) });
}

function longDate(iso: string) {
  if (!iso) return 'unknown';
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? iso
    : when.toLocaleString([], { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric',
      hour: '2-digit', minute: '2-digit' });
}

const ClipIcon = () => (
  <svg className="mail-row__clip" width="11" height="11" viewBox="0 0 24 24" fill="none"
       stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"
       aria-label="has attachments">
    <path d="m21.44 11.05-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48" />
  </svg>
);
