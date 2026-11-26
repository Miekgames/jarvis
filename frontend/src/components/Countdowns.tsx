import { useCallback, useEffect, useRef, useState } from 'react';

import { api, ApiError } from '../lib/api';
import {
  daysSince, inOrder, left, partsOf, spoken, targetParts, when, type Countdown, type Left,
} from '../lib/countdown';
import { useStore } from '../state/store';
import { CountdownForm, longDay } from './CountdownForm';

/**
 * The countdowns, under the clock.
 *
 * Type, not boxes, like the clock above them: up to three, soonest first,
 * ticking in months, days, hours, minutes and seconds. Clicking one drops
 * down the latest news about it, with where its date came from and the means
 * to change or remove it.
 *
 * With none set there is nothing on screen at all.
 */

/** The countdowns, kept current: on load, on change, and on reconnecting. */
export function useCountdowns() {
  const connected = useStore((s) => s.connected);
  const [items, setItems] = useState<Countdown[]>([]);
  const [limit, setLimit] = useState(3);

  const load = useCallback(async () => {
    try {
      const data = await api.get<{ items: Countdown[]; limit: number }>('/countdowns');
      setItems(data.items);
      setLimit(data.limit);
    } catch { /* shown as they were until the next load */ }
  }, []);

  useEffect(() => { void load(); }, [load, connected]);
  useEffect(() => {
    const onChange = () => void load();
    window.addEventListener('jarvis:countdowns', onChange);
    return () => window.removeEventListener('jarvis:countdowns', onChange);
  }, [load]);

  return { items, limit, load };
}

/** The time, on the second. */
export function useNow(active: boolean) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    if (!active) return;
    const tick = () => setNow(new Date());
    tick();
    let interval: number | undefined;
    const timeout = window.setTimeout(() => {
      tick();
      interval = window.setInterval(tick, 1000);
    }, 1000 - (Date.now() % 1000));
    return () => { window.clearTimeout(timeout); if (interval) window.clearInterval(interval); };
  }, [active]);
  return now;
}

const two = (n: number) => String(n).padStart(2, '0');

export function Units({ l }: { l: Left }) {
  const units: [string, string][] = [
    ...(l.months ? [[String(l.months), l.months === 1 ? 'month' : 'months'] as [string, string]] : []),
    [String(l.days), l.days === 1 ? 'day' : 'days'],
    [two(l.hours), 'hrs'], [two(l.minutes), 'min'], [two(l.seconds), 'sec'],
  ];
  return (
    <span className="countdown__units">
      {units.map(([value, name]) => (
        <span className="countdown__unit" key={name}>
          <span className="countdown__value">{value}</span>
          <span className="countdown__name">{name}</span>
        </span>
      ))}
    </span>
  );
}

export function Arrived({ item, now }: { item: Countdown; now: Date }) {
  const days = daysSince(partsOf(now), targetParts(item));
  return (
    <span className="countdown__here">
      {days === 0 ? 'It’s here' : days === 1 ? 'Yesterday' : `${days} days ago`}
    </span>
  );
}

export function Countdowns() {
  const sleeping = useStore((s) => s.sleeping);
  const { items } = useCountdowns();
  const now = useNow(!sleeping && items.length > 0);
  const [openId, setOpenId] = useState<string | null>(null);
  const root = useRef<HTMLDivElement>(null);

  const open = items.find((i) => i.id === openId) ?? null;

  // Close on Escape or a click elsewhere -- but a confirmation, the palette
  // or the setup screen on top has first claim on Escape.
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key !== 'Escape' || document.querySelector('.confirm-layer, .palette')) return;
      event.stopPropagation();
      setOpenId(null);
    };
    const onPointer = (event: PointerEvent) => {
      if (root.current && !root.current.contains(event.target as Node)) setOpenId(null);
    };
    window.addEventListener('keydown', onKey, true);
    document.addEventListener('pointerdown', onPointer);
    return () => {
      window.removeEventListener('keydown', onKey, true);
      document.removeEventListener('pointerdown', onPointer);
    };
  }, [open]);

  if (sleeping || items.length === 0) return null;

  const here = partsOf(now);
  return (
    <div className="countdowns" ref={root}>
      <div className={`countdowns__row countdowns__row--${items.length}`}>
        {inOrder(items, here).map((item) => {
          const l = left(here, targetParts(item));
          return (
            <button key={item.id} type="button"
                    className={`countdown ${item.id === openId ? 'countdown--open' : ''} ${l ? '' : 'countdown--here'}`}
                    aria-expanded={item.id === openId} aria-label={`${spoken(item, l)} Show the latest news.`}
                    onClick={() => setOpenId(item.id === openId ? null : item.id)}>
              <span className="countdown__title">{item.title}</span>
              {l ? <Units l={l} /> : <Arrived item={item} now={now} />}
            </button>
          );
        })}
      </div>
      {open && <NewsBox key={open.id} item={open} now={now} onClose={() => setOpenId(null)} />}
    </div>
  );
}

// ===========================================================================
interface Article { title: string; url: string; source: string; date: string; snippet: string }
interface News { topic: string; articles: Article[]; fetched_at: string | null; error: string }

const NEWS_EVERY = 10 * 60 * 1000;

function NewsBox({ item, now, onClose }: { item: Countdown; now: Date; onClose: () => void }) {
  const [news, setNews] = useState<News | null>(null);
  const [busy, setBusy] = useState(false);
  const [editing, setEditing] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [checking, setChecking] = useState(false);
  const [note, setNote] = useState('');

  const load = useCallback(async (refresh = false) => {
    setBusy(true);
    try {
      setNews(await api.get<News>(`/countdowns/${item.id}/news${refresh ? '?refresh=true' : ''}`));
    } catch (exc) {
      setNews((old) => ({
        topic: old?.topic ?? item.topic, articles: old?.articles ?? [], fetched_at: old?.fetched_at ?? null,
        error: exc instanceof ApiError ? exc.message : 'The news couldn’t be reached just now.',
      }));
    } finally {
      setBusy(false);
    }
  }, [item.id, item.topic]);

  useEffect(() => {
    void load();
    const timer = window.setInterval(() => void load(), NEWS_EVERY);
    return () => window.clearInterval(timer);
  }, [load]);

  const recheck = async () => {
    setChecking(true);
    setNote('');
    try {
      const result = await api.post<{ message: string }>(`/countdowns/${item.id}/recheck`);
      setNote(result.message);
    } catch (exc) {
      setNote(exc instanceof ApiError ? exc.message : 'The date could not be checked.');
    } finally {
      setChecking(false);
    }
  };

  const remove = async () => {
    try {
      await api.del(`/countdowns/${item.id}`);
      onClose();
    } catch (exc) {
      setNote(exc instanceof ApiError ? exc.message : 'It could not be removed.');
    }
  };

  return (
    <section className="newsbox" aria-label={`${item.title}: latest news`}>
      <header className="newsbox__head">
        <div className="newsbox__what">
          <h2 className="newsbox__title">{item.title}</h2>
          <p className="newsbox__date">
            {longDay(item.day)}{item.clock && ` at ${item.clock}`}
          </p>
          <p className="newsbox__from">
            {item.origin === 'web'
              ? <>Date from {item.source_url
                ? <a href={item.source_url} target="_blank" rel="noopener noreferrer">{item.source_name}</a>
                : item.source_name}{item.sources > 1 && ` and ${item.sources - 1} more`}
                {item.checked_at && `, checked ${when(item.checked_at, now)}`}</>
              : 'Date set by you'}
          </p>
        </div>
        <button type="button" className="newsbox__close" aria-label="Close" onClick={onClose}>×</button>
      </header>

      <div className="newsbox__tools">
        <button type="button" className="mini-action" onClick={() => { setEditing(!editing); setRemoving(false); }}>
          {editing ? 'Stop editing' : 'Edit date, time, name'}
        </button>
        {item.origin === 'web' && item.source_url && (
          <button type="button" className="mini-action" disabled={checking} onClick={() => void recheck()}>
            {checking ? 'Checking…' : 'Check the date again'}
          </button>
        )}
        {removing ? (
          <>
            <button type="button" className="mini-action mini-action--danger" onClick={() => void remove()}>Yes, remove it</button>
            <button type="button" className="mini-action" onClick={() => setRemoving(false)}>Keep</button>
          </>
        ) : (
          <button type="button" className="mini-action" onClick={() => setRemoving(true)}>Remove</button>
        )}
      </div>
      {note && <p className="newsbox__note" role="status">{note}</p>}

      {editing ? (
        <CountdownForm item={item} onCancel={() => setEditing(false)}
                       onDone={() => { setEditing(false); void load(true); }} />
      ) : (
        <>
          <div className="newsbox__bar">
            <span>Latest news</span>
            {news?.fetched_at && <span className="newsbox__age">updated {when(news.fetched_at, now)}</span>}
            {item.topic && (
              <button type="button" className="mini-action" disabled={busy} onClick={() => void load(true)}>
                {busy ? 'Looking…' : 'Refresh'}
              </button>
            )}
          </div>
          {news?.error && <p className="newsbox__note">{news.error}</p>}
          {!news && <p className="newsbox__note">Looking for the latest news…</p>}
          {news && !item.topic && (
            <p className="newsbox__note">
              No news topic is set for this one. Add one under “Edit” to see news here.
            </p>
          )}
          {news && item.topic && !news.error && news.articles.length === 0 && (
            <p className="newsbox__note">No recent news about {item.topic}.</p>
          )}
          <ol className="newsbox__list">
            {news?.articles.map((a) => (
              <li key={a.url}>
                <a className="newsbox__item" href={a.url} target="_blank" rel="noopener noreferrer">
                  <span className="newsbox__headline">{a.title}</span>
                  <span className="newsbox__meta">{[a.source, when(a.date, now)].filter(Boolean).join(' · ')}</span>
                  {a.snippet && <span className="newsbox__snippet">{a.snippet}</span>}
                </a>
              </li>
            ))}
          </ol>
        </>
      )}
    </section>
  );
}
