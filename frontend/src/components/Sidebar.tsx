import { useEffect, useState } from 'react';

import { Sparkline } from './Sparkline';
import { api } from '../lib/api';
import { useStore, type Panel } from '../state/store';

/**
 * The right edge, as one thing.
 *
 * System readout and panel navigation used to be two separate fixtures that
 * happened to share an edge: a block of metrics pinned top-right, and a list
 * of panel names floating at 24% opacity beside it. Together they occupied a
 * good strip of the field without ever behaving as a unit.
 *
 * Now it is a single rail that stays out of the way and opens when reached
 * for -- on hover, on keyboard focus, or whenever a panel is open.
 *
 * What it will not do is hide things that matter.
 *
 * A sidebar that needs hovering before it will admit the disk is full is
 * worse than the clutter it replaced, so the collapsed rail is not empty: it
 * keeps a health light, a mark for anything in a warning state, and the
 * badges for unread alerts and failed work. The labels and the numbers are
 * what hovering reveals -- not the fact that something wants attention.
 */

/**
 * The menu, in groups.
 *
 * Seventeen entries in one flat column is a list you read rather than scan,
 * and the thing you want is never where you expect. Grouped, the eye goes
 * to a heading first and four items second.
 *
 * The order is roughly how often a thing gets opened, not alphabetical and
 * not how the code is organised: what JARVIS is doing right now, then what
 * it is making, then what it remembers, then the machinery.
 */
interface Entry { key: Exclude<Panel, null>; label: string }
interface Group { title: string; entries: Entry[] }

const GROUPS: Group[] = [
  {
    title: 'Now',
    entries: [
      { key: 'tasks', label: 'Tasks' },
      { key: 'email', label: 'Email' },
      { key: 'away', label: 'While away' },
      { key: 'agents', label: 'Agents' },
      { key: 'notifications', label: 'Notifications' },
    ],
  },
  {
    title: 'Make',
    entries: [
      { key: 'skills', label: 'Skills' },
      { key: 'workshop', label: 'Workshop' },
      { key: 'selfedit', label: 'Self-edit' },
      { key: 'developer', label: 'Developer' },
      { key: 'research', label: 'Research' },
    ],
  },
  {
    title: 'Remember',
    entries: [
      { key: 'history', label: 'History' },
      { key: 'memory', label: 'Memory' },
      { key: 'documents', label: 'Documents' },
      { key: 'voice', label: 'Heard' },
      { key: 'activity', label: 'Activity' },
    ],
  },
  {
    title: 'Plan',
    entries: [
      { key: 'goals', label: 'Goals' },
      { key: 'calendar', label: 'Calendar' },
      { key: 'countdowns', label: 'Countdowns' },
      { key: 'automations', label: 'Automations' },
    ],
  },
  {
    title: 'System',
    entries: [
      { key: 'system', label: 'System' },
      { key: 'integrations', label: 'Integrations' },
      { key: 'plugins', label: 'Plug-ins' },
      { key: 'settings', label: 'Settings' },
    ],
  },
];

const ENTRIES: Entry[] = GROUPS.flatMap((group) => group.entries);

export function Sidebar() {
  const panel = useStore((s) => s.panel);
  const setPanel = useStore((s) => s.setPanel);
  const tasks = useStore((s) => s.tasks);
  const notifications = useStore((s) => s.notifications);
  const sleep = useStore((s) => s.sleep);
  // Phone only: whether the drawer is showing. Desktop opens on hover and
  // never consults this.
  const [drawer, setDrawer] = useState(false);
  const metrics = useStore((s) => s.metrics);
  const metricHistory = useStore((s) => s.metricHistory);
  const overall = useStore((s) => s.overall);

  const activeTasks = tasks.filter((t) =>
    ['running', 'queued', 'paused', 'waiting'].includes(t.status)).length;
  const failedTasks = tasks.filter((t) => t.status === 'failed').length;
  const unread = notifications.filter((n) => !n.read).length;

  const away = useAwayWatch(panel === 'away');
  const unreadMail = useUnreadMail();

  const badge = (key: string): { count: number | string; warn: boolean; live?: boolean } | null => {
    if (key === 'tasks' && (activeTasks || failedTasks)) {
      return { count: activeTasks || failedTasks, warn: activeTasks === 0 && failedTasks > 0 };
    }
    if (key === 'notifications' && unread) return { count: unread, warn: false };
    if (key === 'email' && unreadMail) return { count: unreadMail > 99 ? '99+' : unreadMail, warn: false };
    if (key === 'away' && away === 'ready') return { count: '✓', warn: false };
    if (key === 'away' && away === 'running') return { count: '•', warn: false, live: true };
    return null;
  };

  useEffect(() => {
    if (!drawer) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setDrawer(false);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [drawer]);

  const series = (key: keyof typeof metrics) =>
    metricHistory.map((m) => m[key] as number | null | undefined);

  const readings: Reading[] = [
    { label: 'CPU', value: `${fmt(metrics.cpu)}%`, tone: tone(metrics.cpu, 85),
      trend: series('cpu') },
    { label: 'MEM', value: `${fmt(metrics.mem)}%`, tone: tone(metrics.mem, 88),
      trend: series('mem') },
  ];
  if (metrics.gpu != null) {
    readings.push({
      label: 'GPU',
      value: `${fmt(metrics.gpu)}%${metrics.gpu_temp != null
        ? ` · ${Math.round(metrics.gpu_temp)}°` : ''}`,
      tone: tone(metrics.gpu, 90),
      trend: series('gpu'),
    });
  }
  readings.push({ label: 'DISK', value: `${fmt(metrics.disk)}%`,
                  tone: tone(metrics.disk, 92), trend: series('disk') });
  if (metrics.battery != null) {
    readings.push({
      label: 'BATT',
      value: `${Math.round(metrics.battery)}%`,
      tone: metrics.battery < 20 ? 'warn' : '',
      trend: series('battery'),
    });
  }

  // What the collapsed rail has to carry: anything the user would be annoyed
  // to discover only after hovering.
  const pressing = readings.filter((r) => r.tone).length;

  return (
    <>
      {/* On a phone there is no hover and no room for a permanent column, so
          the rail becomes a drawer behind an explicit button. Hidden on
          anything wider, where reaching for the edge already works. */}
      <button
        className={`sidebar-toggle ${drawer ? 'sidebar-toggle--on' : ''}`}
        onClick={() => setDrawer((open) => !open)}
        aria-expanded={drawer}
        aria-label={drawer ? 'Close menu' : 'Open menu'}
      >
        <span /><span /><span />
      </button>

      {drawer && (
        <button className="sidebar-scrim" aria-label="Close menu" tabIndex={-1}
                onClick={() => setDrawer(false)} />
      )}

    <aside
      className={`sidebar ${panel ? 'sidebar--open' : ''} ${drawer ? 'sidebar--drawer' : ''}`}
      aria-label="System status and panels"
    >
      {/* ------------------------------------------------- system readout */}
      <div className="sidebar__section">
        <div className="sidebar__rail-marks" aria-hidden="true">
          <span className={`sidebar__health sidebar__health--${healthTone(overall)}`} />
          {pressing > 0 && (
            <span className="sidebar__alert" title={`${pressing} reading(s) over threshold`} />
          )}
        </div>

        <div className="sidebar__body">
          {readings.map((reading) => (
            <div className="sidebar__row" key={reading.label}>
              <span className="sidebar__key">{reading.label}</span>
              <Sparkline
                values={reading.trend}
                className={reading.tone ? `spark--${reading.tone}` : ''}
              />
              <span className={`sidebar__val ${reading.tone ? `sidebar__val--${reading.tone}` : ''}`}>
                {reading.value}
              </span>
            </div>
          ))}
          <div className="sidebar__row">
            <span className="sidebar__key">HEALTH</span>
            <span className={`sidebar__val sidebar__val--${healthTone(overall)}`}>
              {overall.toUpperCase()}
            </span>
          </div>
        </div>
      </div>

      <div className="sidebar__divider" aria-hidden="true" />

      {/* ------------------------------------------------------- the panels */}
      <nav className="sidebar__nav">
        {GROUPS.map((group) => (
          <div className="sidebar__group" key={group.title}>
            <div className="sidebar__group-title">{group.title}</div>
            {group.entries.map((entry) => {
              const marker = badge(entry.key);
              return (
                <button
                  key={entry.key}
                  className={`sidebar__item ${panel === entry.key ? 'sidebar__item--active' : ''}`}
                  onClick={() => { setPanel(entry.key); setDrawer(false); }}
                  aria-pressed={panel === entry.key}
                >
                  <span className="sidebar__marker">
                    {marker
                      ? (
                        <span className={`sidebar__badge ${marker.warn ? 'sidebar__badge--warn' : ''} ${marker.live ? 'sidebar__badge--live' : ''}`}
                              title={entry.key === 'away'
                                ? (away === 'running' ? 'Working while you are away'
                                   : 'Finished — see what it made')
                                : undefined}>
                          {marker.count}
                        </span>
                      )
                      : <span className="sidebar__tick" aria-hidden="true" />}
                  </span>
                  <span className="sidebar__label">{entry.label}</span>
                </button>
              );
            })}
          </div>
        ))}

        <button
          className="sidebar__item"
          onClick={() => { setDrawer(false); void sleep(); }}
          title="Sleep — the display goes black and rendering stops; background services keep running"
        >
          <span className="sidebar__marker">
            <span className="sidebar__tick" aria-hidden="true" />
          </span>
          <span className="sidebar__label">Sleep</span>
        </button>
      </nav>
    </aside>
    </>
  );
}

/**
 * Whether unattended work is going on, or has finished and not been looked at.
 *
 * Before this, a run that finished said so only inside its own panel. With
 * the panel closed -- which is how anyone leaves a screen they have walked
 * away from -- nothing changed when it was done.
 *
 * Now the menu carries it: a pulsing mark while it works, a tick when it
 * has finished, until the While away panel is opened. And at the moment it
 * finishes, a message on screen, and a mark in the browser tab's title if
 * the tab is in the background, removed when you come back to it.
 */
/**
 * Unread email, for the badge. Asked once on load, then every few minutes --
 * the server answers from a one-minute cache, so this is one cheap IMAP
 * STATUS at most. The Email panel announces fresher counts as it learns them.
 * Not connected means no badge and no further asking.
 */
function useUnreadMail(): number {
  const [unread, setUnread] = useState(0);

  useEffect(() => {
    let connected = false;
    let cancelled = false;
    const ask = () => {
      void api.get<{ connected: boolean; unread: number }>('/email/unread')
        .then((data) => {
          if (cancelled) return;
          connected = data.connected;
          setUnread(data.connected ? data.unread : 0);
        })
        .catch(() => { /* unknown is no badge, not a wrong one */ });
    };
    ask();
    const timer = window.setInterval(() => { if (connected) ask(); }, 3 * 60_000);
    const onAnnounce = (event: Event) => {
      const value = (event as CustomEvent<{ unread: number | null }>).detail?.unread;
      connected = value != null;
      setUnread(value ?? 0);
    };
    window.addEventListener('jarvis:email', onAnnounce);
    return () => {
      cancelled = true;
      window.clearInterval(timer);
      window.removeEventListener('jarvis:email', onAnnounce);
    };
  }, []);

  return unread;
}

function useAwayWatch(looking: boolean): 'running' | 'ready' | null {
  const [state, setState] = useState<'running' | 'ready' | null>(null);
  const notify = useStore((s) => s.notify);

  useEffect(() => {
    let cancelled = false;
    // What is going on already, for a page opened mid-run.
    void api.get<{ busy?: boolean }>('/autonomy')
      .then((status) => { if (!cancelled && status?.busy) setState('running'); })
      .catch(() => { /* not available: no mark, which is the truth */ });

    const onProgress = (event: Event) => {
      const detail = (event as CustomEvent<{ running?: boolean; phase?: string }>).detail ?? {};
      if (detail.running) {
        setState('running');
        return;
      }
      if (!['done', 'stopped', 'failed'].includes(detail.phase ?? '')) return;
      setState('ready');
      notify(detail.phase === 'failed'
        ? 'Work while you were away stopped with an error.'
        : 'Finished working while you were away — tap to see what it made.', 'away');
      if (document.hidden && !document.title.includes('✓ ')) {
        document.title = `✓ ${document.title}`;
      }
    };
    // Its own mark, wherever it ended up: unread replies put a count there too.
    const onVisible = () => {
      if (!document.hidden && document.title.includes('✓ ')) {
        document.title = document.title.replace('✓ ', '');
      }
    };
    window.addEventListener('jarvis:autonomy', onProgress);
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      cancelled = true;
      window.removeEventListener('jarvis:autonomy', onProgress);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [notify]);

  // Opening the panel is reading the news.
  useEffect(() => {
    if (looking && state === 'ready') setState(null);
  }, [looking, state]);

  return state;
}

interface Reading {
  label: string;
  value: string;
  tone: string;
  /** Oldest first, for the trend line. */
  trend: (number | null | undefined)[];
}

// ---------------------------------------------------------------------------
function fmt(value?: number | null): string {
  return value == null ? '—' : String(Math.round(value));
}

/** '', 'warn' or 'danger' for a reading against its threshold. */
function tone(value: number | null | undefined, threshold: number): string {
  if (value == null) return '';
  if (value >= threshold + 8) return 'danger';
  if (value >= threshold) return 'warn';
  return '';
}

function healthTone(overall: string): string {
  if (overall === 'online') return 'ok';
  if (overall === 'degraded') return 'warn';
  if (overall === 'error' || overall === 'offline') return 'danger';
  return 'idle';
}
