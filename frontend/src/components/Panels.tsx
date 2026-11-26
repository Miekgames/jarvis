import { useCallback, useEffect, useState } from 'react';

import { api } from '../lib/api';
import { speakerLease } from '../lib/leader';
import { voice } from '../lib/voice';
import { matchWakeWord } from '../lib/wakeword';
import { DeveloperPanel } from './panels/DeveloperPanel';
import { DesktopNotifications } from './panels/DesktopNotifications';
import { VoicePanel } from './panels/VoicePanel';
import { AwayPanel } from './panels/AwayPanel';
import { SkillsPanel } from './panels/SkillsPanel';
import { PluginsPanel } from './panels/PluginsPanel';
import { CountdownsPanel } from './panels/CountdownsPanel';
import { EverydayPanel } from './panels/EverydayPanel';
import { DocumentsPanel } from './panels/DocumentsPanel';
import { EmailPanel } from './panels/EmailPanel';
import { WorkshopPanel } from './panels/WorkshopPanel';
import { AgentsPanel, GoalsPanel, ResearchPanel, SelfEditPanel } from './panels/WorkPanels';
import { useStore } from '../state/store';

/**
 * Slide-over panels. Nothing here is permanently on screen — panels appear
 * when asked for and disappear again, keeping the field as the interface.
 */
export function Panels() {
  const panel = useStore((s) => s.panel);
  const setPanel = useStore((s) => s.setPanel);

  useEffect(() => {
    if (!panel) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setPanel(null);
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [panel, setPanel]);

  if (!panel) return null;

  const titles: Record<string, string> = {
    tasks: 'Task manager',
    notifications: 'Notifications',
    activity: 'Activity log',
    calendar: 'Calendar',
    automations: 'Automations',
    memory: 'Memory',
    system: 'System',
    integrations: 'Integrations',
    history: 'Conversation history',
    settings: 'Settings',
    goals: 'Goals',
    agents: 'Agents',
    research: 'Research',
    developer: 'Developer',
    selfedit: 'Self-editing',
    voice: 'What it heard',
    workshop: 'Workshop',
    away: 'While you’re away',
    skills: 'Skills',
    plugins: 'Plug-ins',
    documents: 'Documents',
    email: 'Email',
    countdowns: 'Countdowns',
    everyday: 'Everyday lookups',
  };

  const bodies: Record<string, JSX.Element> = {
    tasks: <TasksPanel />,
    notifications: <NotificationsPanel />,
    activity: <ActivityPanel />,
    calendar: <CalendarPanel />,
    automations: <AutomationsPanel />,
    memory: <MemoryPanel />,
    system: <SystemPanel />,
    integrations: <IntegrationsPanel />,
    history: <HistoryPanel />,
    settings: <SettingsPanel />,
    goals: <GoalsPanel />,
    agents: <AgentsPanel />,
    research: <ResearchPanel />,
    developer: <DeveloperPanel />,
    voice: <VoicePanel />,
    selfedit: <SelfEditPanel />,
    workshop: <WorkshopPanel />,
    away: <AwayPanel />,
    skills: <SkillsPanel />,
    plugins: <PluginsPanel />,
    documents: <DocumentsPanel />,
    email: <EmailPanel />,
    countdowns: <CountdownsPanel />,
    everyday: <EverydayPanel />,
  };

  return (
    <aside className="panel" role="complementary" aria-label={titles[panel]}>
      <header className="panel__head">
        <h2 className="panel__title">{titles[panel]}</h2>
        <button className="panel__close" onClick={() => setPanel(null)} aria-label="Close panel">×</button>
      </header>
      <div className="panel__body">{bodies[panel]}</div>
    </aside>
  );
}

// ===========================================================================
function TasksPanel() {
  const tasks = useStore((s) => s.tasks);
  const refresh = useStore((s) => s.refreshTasks);
  const stopAll = useStore((s) => s.stopAll);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [logs, setLogs] = useState<Record<string, any[]>>({});

  const control = async (id: string, action: string) => {
    try {
      await api.post(`/tasks/${id}/${action}`);
      await refresh();
    } catch (error) {
      useStore.getState().notify((error as Error).message);
    }
  };

  const openLogs = async (id: string) => {
    if (expanded === id) { setExpanded(null); return; }
    setExpanded(id);
    try {
      const result = await api.get<{ logs: any[] }>(`/tasks/${id}`);
      setLogs((current) => ({ ...current, [id]: result.logs }));
    } catch { /* transient */ }
  };

  const active = tasks.filter((t) => ['running', 'queued', 'paused', 'waiting'].includes(t.status));
  const finished = tasks.filter((t) => !['running', 'queued', 'paused', 'waiting'].includes(t.status));

  return (
    <>
      {active.length > 0 && (
        <div className="panel__section">
          <div className="panel__section-title">
            Active — {active.length}
            <button className="mini-action mini-action--danger" style={{ float: 'right' }}
                    onClick={() => void stopAll()}>
              Stop all
            </button>
          </div>
          {active.map((task) => (
            <div className="row" key={task.id}>
              <div className="row__head">
                <span className="row__title">{task.name}</span>
                <span className={`status-chip status-chip--${task.status}`}>{task.status}</span>
              </div>
              <div className="row__sub">
                {task.current_step || task.type}
                {task.eta && task.status === 'running' && ` · ETA ${relative(task.eta)}`}
              </div>
              <div className="progress">
                <div
                  className={`progress__bar ${task.status === 'running' && task.progress === 0
                    ? 'progress__bar--indeterminate' : ''}`}
                  style={{ width: `${Math.max(2, (task.progress ?? 0) * 100)}%` }}
                />
              </div>
              <div className="row__actions">
                {task.status === 'running' && (
                  <button className="mini-action" onClick={() => void control(task.id, 'pause')}>Pause</button>
                )}
                {task.status === 'paused' && (
                  <button className="mini-action" onClick={() => void control(task.id, 'resume')}>Resume</button>
                )}
                <button className="mini-action mini-action--danger"
                        onClick={() => void control(task.id, 'cancel')}>Cancel</button>
                <button className="mini-action" onClick={() => void openLogs(task.id)}>
                  {expanded === task.id ? 'Hide log' : 'Log'}
                </button>
              </div>
              {expanded === task.id && (
                <pre className="confirm__detail" style={{ marginTop: '0.7rem' }}>
                  {(logs[task.id] ?? []).map((l) => `${l.ts.slice(11, 19)}  ${l.message}`).join('\n')
                    || 'No log entries yet.'}
                </pre>
              )}
            </div>
          ))}
        </div>
      )}

      <div className="panel__section">
        <div className="panel__section-title">Recent</div>
        {finished.length === 0 && active.length === 0 && (
          <p className="empty">No tasks yet. Ask JARVIS to research something and it will run here.</p>
        )}
        {finished.slice(0, 25).map((task) => (
          <div className="row" key={task.id}>
            <div className="row__head">
              <span className="row__title">{task.name}</span>
              <span className={`status-chip status-chip--${task.status}`}>{task.status}</span>
            </div>
            {task.error && <div className="row__sub" style={{ color: 'var(--danger)' }}>{task.error}</div>}
            {!task.error && <div className="row__sub">{relative(task.created_at)} · {task.type}</div>}
            <div className="row__actions">
              {task.status === 'failed' && (
                <button className="mini-action" onClick={() => void control(task.id, 'retry')}>Retry</button>
              )}
              <button className="mini-action" onClick={() => void openLogs(task.id)}>
                {expanded === task.id ? 'Hide log' : 'Log'}
              </button>
            </div>
            {expanded === task.id && (
              <pre className="confirm__detail" style={{ marginTop: '0.7rem' }}>
                {(logs[task.id] ?? []).map((l) => `${l.ts.slice(11, 19)}  ${l.message}`).join('\n')
                  || 'No log entries.'}
              </pre>
            )}
          </div>
        ))}
      </div>
    </>
  );
}

// ===========================================================================
/**
 * Notifications, from both directions.
 *
 * This computer's own -- read from the Windows Action Center -- come first,
 * because they are the ones a person means when they ask what they missed.
 * JARVIS's own alerts follow underneath. They used to be a separate "Alerts"
 * entry in the sidebar, which is one entry too many for two lists of the
 * same kind of thing.
 */
function NotificationsPanel() {
  return (
    <>
      <div className="panel__section">
        <div className="panel__section-title">This computer</div>
        <DesktopNotifications />
      </div>

      <div className="panel__section">
        <div className="panel__section-title">From JARVIS</div>
        <JarvisNotifications />
      </div>
    </>
  );
}

function JarvisNotifications() {
  const notifications = useStore((s) => s.notifications);
  const refresh = useStore((s) => s.refreshNotifications);

  const act = async (id: string, action: string) => {
    await api.post(`/notifications/${id}/${action}`);
    await refresh();
  };

  if (!notifications.length) return <p className="empty">Nothing pending.</p>;

  return (
    <>
      <div className="panel__section-title">
        {notifications.length} total
        <button className="mini-action" style={{ float: 'right' }}
                onClick={async () => { await api.post('/notifications/read-all'); await refresh(); }}>
          Mark all read
        </button>
      </div>
      {notifications.map((item) => (
        <div className="row" key={item.id} style={{ opacity: item.read ? 0.55 : 1 }}>
          <div className="row__head">
            <span className="row__title">{item.title}</span>
            <span className={`risk-tag risk-tag--${riskOf(item.priority)}`}>{item.priority}</span>
          </div>
          {item.body && <div className="row__sub">{item.body}</div>}
          <div className="row__actions">
            <span className="row__meta">{relative(item.ts)} · {item.source}</span>
            {!item.read && <button className="mini-action" onClick={() => void act(item.id, 'read')}>Read</button>}
            <button className="mini-action" onClick={() => void act(item.id, 'snooze')}>Snooze</button>
            <button className="mini-action mini-action--danger"
                    onClick={() => void act(item.id, 'dismiss')}>Dismiss</button>
          </div>
        </div>
      ))}
    </>
  );
}

// ===========================================================================
function ActivityPanel() {
  const activity = useStore((s) => s.activity);
  const [filter, setFilter] = useState('');

  const filtered = filter
    ? activity.filter((a) => a.message.toLowerCase().includes(filter.toLowerCase()))
    : activity;

  return (
    <>
      <input
        className="text-input"
        style={{ maxWidth: '100%', width: '100%', marginBottom: '1.2rem' }}
        placeholder="Filter the activity log…"
        value={filter}
        onChange={(event) => setFilter(event.target.value)}
      />
      {filtered.length === 0 && <p className="empty">Nothing logged yet.</p>}
      {filtered.map((entry, index) => (
        <div className="row" key={index}>
          <div className="row__head">
            <span className="row__title" style={{
              fontSize: '0.84rem',
              color: entry.level === 'error' ? 'var(--danger)'
                : entry.level === 'warn' ? 'var(--warn)' : 'var(--ink)',
            }}>
              {entry.message}
            </span>
          </div>
          <div className="row__sub">{relative(entry.ts)} · {entry.source}</div>
        </div>
      ))}
    </>
  );
}

// ===========================================================================
function CalendarPanel() {
  const [events, setEvents] = useState<any[]>([]);
  const [title, setTitle] = useState('');
  const [start, setStart] = useState('');

  const load = useCallback(async () => {
    try {
      const result = await api.get<{ events: any[] }>('/calendar?range=week');
      setEvents(result.events);
    } catch { /* transient */ }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const create = async () => {
    if (!title.trim() || !start) return;
    try {
      await api.post('/calendar', { title, start_at: new Date(start).toISOString() });
      setTitle(''); setStart('');
      await load();
    } catch (error) {
      useStore.getState().notify((error as Error).message);
    }
  };

  return (
    <>
      <div className="panel__section">
        <div className="panel__section-title">Next 7 days</div>
        {events.length === 0 && <p className="empty">Nothing scheduled.</p>}
        {events.map((event) => (
          <div className="row" key={event.id}>
            <div className="row__head">
              <span className="row__title">{event.title}</span>
              <span className="row__meta">{formatWhen(event.start_at)}</span>
            </div>
            {(event.location || event.link) && (
              <div className="row__sub">
                {event.link
                  ? <a href={event.link} target="_blank" rel="noreferrer">{event.link}</a>
                  : event.location}
              </div>
            )}
            <div className="row__actions">
              <button className="mini-action mini-action--danger"
                      onClick={async () => { await api.del(`/calendar/${event.id}`); await load(); }}>
                Cancel
              </button>
            </div>
          </div>
        ))}
      </div>

      <div className="panel__section">
        <div className="panel__section-title">Add an event</div>
        <input className="text-input" style={{ maxWidth: '100%', width: '100%', marginBottom: '0.6rem' }}
               placeholder="Title" value={title} onChange={(e) => setTitle(e.target.value)} />
        <input className="text-input" style={{ maxWidth: '100%', width: '100%', marginBottom: '0.8rem' }}
               type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} />
        <button className="btn btn--primary" onClick={() => void create()}>Add</button>
      </div>
    </>
  );
}

// ===========================================================================
function AutomationsPanel() {
  const [automations, setAutomations] = useState<any[]>([]);

  const load = useCallback(async () => {
    try {
      const result = await api.get<{ automations: any[] }>('/automations');
      setAutomations(result.automations);
    } catch { /* transient */ }
  }, []);

  useEffect(() => { void load(); }, [load]);

  const toggle = async (automation: any) => {
    await api.patch(`/automations/${automation.id}`, { enabled: !automation.enabled });
    await load();
  };

  return (
    <>
      {automations.length === 0 && (
        <p className="empty">
          No automations yet. Try telling JARVIS: &ldquo;when a task fails, notify me and
          say it out loud&rdquo; — it will build the automation and show you exactly what it
          will do before enabling it.
        </p>
      )}
      {automations.map((automation) => (
        <div className="row" key={automation.id}>
          <div className="row__head">
            <span className="row__title">{automation.name}</span>
            <button className={`switch ${automation.enabled ? 'switch--on' : ''}`}
                    onClick={() => void toggle(automation)}
                    aria-label={`${automation.enabled ? 'Disable' : 'Enable'} ${automation.name}`} />
          </div>
          <div className="row__sub">{automation.preview?.summary}</div>
          <div className="row__actions">
            <span className="row__meta">
              fired {automation.fire_count}× {automation.last_fired && `· ${relative(automation.last_fired)}`}
            </span>
            <button className="mini-action"
                    onClick={async () => { await api.post(`/automations/${automation.id}/run`); await load(); }}>
              Run now
            </button>
            <button className="mini-action mini-action--danger"
                    onClick={async () => { await api.del(`/automations/${automation.id}`); await load(); }}>
              Delete
            </button>
          </div>
        </div>
      ))}
    </>
  );
}

// ===========================================================================
function MemoryPanel() {
  const [memories, setMemories] = useState<any[]>([]);
  const [stats, setStats] = useState<any>({});
  const [draft, setDraft] = useState('');

  const load = useCallback(async () => {
    try {
      const result = await api.get<{ memories: any[]; stats: any }>('/memory');
      setMemories(result.memories);
      setStats(result.stats);
    } catch { /* transient */ }
  }, []);

  useEffect(() => { void load(); }, [load]);

  return (
    <>
      <div className="panel__section">
        <div className="panel__section-title">
          {stats.total ?? 0} stored · memory {stats.enabled ? 'on' : 'off'}
        </div>
        <p style={{ fontSize: '0.78rem', color: 'var(--ink-ghost)', marginBottom: '1rem' }}>
          Everything JARVIS remembers about you is listed here. Nothing is hidden, and you can
          delete any of it.
        </p>
        <div style={{ display: 'flex', gap: '0.5rem', marginBottom: '1.2rem' }}>
          <input className="text-input" style={{ flex: 1, maxWidth: 'none' }}
                 placeholder="Add something to remember…"
                 value={draft} onChange={(e) => setDraft(e.target.value)} />
          <button className="btn btn--primary" onClick={async () => {
            if (!draft.trim()) return;
            await api.post('/memory', { value: draft, category: 'general' });
            setDraft('');
            await load();
          }}>Add</button>
        </div>
      </div>

      {memories.length === 0 && <p className="empty">Nothing remembered yet.</p>}
      {memories.map((memory) => (
        <div className="row" key={memory.id}>
          <div className="row__head">
            <span className="row__title" style={{ fontSize: '0.86rem' }}>{memory.value}</span>
            <span className="row__meta">{memory.category}</span>
          </div>
          <div className="row__actions">
            <span className="row__meta">
              {memory.source === 'learned' && (
                <span title="Picked up from something you said, without being asked to remember it">
                  learned ·{' '}
                </span>
              )}
              importance {memory.importance} · {relative(memory.updated_at)}
            </span>
            <button className="mini-action mini-action--danger" onClick={async () => {
              await api.del(`/memory/${memory.id}`);
              await load();
            }}>Forget</button>
          </div>
        </div>
      ))}
    </>
  );
}

// ===========================================================================
function SystemPanel() {
  const metrics = useStore((s) => s.metrics);
  const health = useStore((s) => s.health);
  const [info, setInfo] = useState<any>({});
  const [processes, setProcesses] = useState<any[]>([]);
  // Measuring CPU per process means watching for a moment -- a real couple of
  // seconds, not a decorative spinner. An empty list in the meantime reads as
  // "nothing is running", which is worse than saying what is happening.
  const [measuring, setMeasuring] = useState(true);
  const [processError, setProcessError] = useState('');

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const detail = await api.get('/system/info');
        if (!cancelled) setInfo(detail);
      } catch { /* the live metrics above do not depend on this */ }
      try {
        const result = await api.get<{ processes: any[] }>('/system/processes?limit=12');
        if (!cancelled) setProcesses(result.processes);
      } catch (error) {
        if (!cancelled) setProcessError((error as Error).message);
      } finally {
        if (!cancelled) setMeasuring(false);
      }
    })();
    return () => { cancelled = true; };
  }, []);

  return (
    <>
      <div className="panel__section">
        <div className="panel__section-title">Live</div>
        <Metric label="CPU" value={`${metrics.cpu ?? '—'}%`} />
        <Metric label="Memory" value={`${metrics.mem ?? '—'}% · ${metrics.mem_used_gb ?? '—'} / ${metrics.mem_total_gb ?? '—'} GB`} />
        <Metric label="Disk" value={`${metrics.disk ?? '—'}% · ${metrics.disk_free_gb ?? '—'} GB free`} />
        {metrics.gpu != null && (
          <Metric label="GPU" value={`${metrics.gpu}%${metrics.gpu_temp ? ` · ${metrics.gpu_temp}°C` : ''}`} />
        )}
        <Metric label="Network" value={`↑ ${kb(metrics.net_up)} ↓ ${kb(metrics.net_down)}`} />
        <Metric label="Processes" value={String(metrics.processes ?? '—')} />
        <Metric label="Uptime" value={duration(metrics.uptime_seconds)} />
      </div>

      <Efficiency />

      <div className="panel__section">
        <div className="panel__section-title">Subsystem health</div>
        {Object.entries(health).map(([name, check]) => (
          <div className="control" key={name}>
            <div>
              <div className="control__label">{name}</div>
              <div className="control__help">{check.detail}</div>
            </div>
            <span className={`status-chip status-chip--${check.status === 'online' ? 'completed'
              : check.status === 'degraded' ? 'paused' : 'failed'}`}>
              {check.status}
            </span>
          </div>
        ))}
      </div>

      <div className="panel__section">
        <div className="panel__section-title">Machine</div>
        <Metric label="OS" value={info.os ?? '—'} />
        <Metric label="CPU" value={`${info.processor ?? '—'} (${info.cpu_cores_logical ?? '?'} threads)`} />
        {info.gpu && <Metric label="GPU" value={info.gpu} />}
        <Metric label="Memory" value={`${info.memory_total_gb ?? '—'} GB`} />
      </div>

      <div className="panel__section">
        <div className="panel__section-title">Top processes</div>
        {measuring && (
          <div className="control__help">Measuring CPU use over a short window…</div>
        )}
        {processError && (
          <div className="control__help control__help--bad">
            Could not read the process list: {processError}
          </div>
        )}
        {!measuring && !processError && processes.length === 0 && (
          <div className="control__help">No processes could be read.</div>
        )}
        {processes.map((process) => (
          <div className="control" key={process.pid}>
            <div className="control__label">{process.name}</div>
            <span className="row__meta">{process.cpu}% · {process.memory_mb} MB</span>
          </div>
        ))}
      </div>
    </>
  );
}

/**
 * What running locally costs and saves, added up from every reply.
 *
 * Energy is the GPU's own counter over each reply; the cloud figure is what
 * the same tokens would have cost on a hosted model, at the stated price.
 * Ported idea: OpenJarvis's energy dashboard and local-vs-cloud cost
 * comparison ("intelligence per watt").
 */
function Efficiency() {
  const [hours, setHours] = useState(24);
  const [data, setData] = useState<any>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    setError('');
    api.get(`/ai/efficiency?hours=${hours}`)
      .then((result) => { if (!cancelled) setData(result); })
      .catch((exc) => { if (!cancelled) setError((exc as Error).message); });
    return () => { cancelled = true; };
  }, [hours]);

  const spans = [{ label: 'Day', hours: 24 }, { label: 'Week', hours: 168 },
                 { label: 'Month', hours: 720 }];

  return (
    <div className="panel__section">
      <div className="panel__section-title">Efficiency</div>
      <div className="away__spans">
        {spans.map((span) => (
          <button key={span.hours}
                  className={`away__span ${hours === span.hours ? 'away__span--on' : ''}`}
                  onClick={() => setHours(span.hours)}>
            {span.label}
          </button>
        ))}
      </div>
      {error && <div className="control__help control__help--bad">{error}</div>}
      {data && data.replies === 0 && (
        <div className="control__help">
          No replies in this period yet. Every reply from now on is measured.
        </div>
      )}
      {data && data.replies > 0 && (
        <>
          <Metric label="Replies" value={`${data.replies} · ${data.tokens_out.toLocaleString()} tokens written`} />
          {data.avg_tokens_per_sec != null && (
            <Metric label="Speed" value={`${data.avg_tokens_per_sec} tokens a second on average`} />
          )}
          {data.avg_ttft_ms != null && (
            <Metric label="First words" value={`after ${(data.avg_ttft_ms / 1000).toFixed(1)} s on average`} />
          )}
          {data.energy_j > 0 && (
            <Metric label="GPU energy"
                    value={`${data.energy_wh} Wh${data.tokens_per_joule ? ` · ${data.tokens_per_joule} tokens per joule` : ''}`} />
          )}
          <Metric label="Cloud cost avoided"
                  value={`$${data.cloud_cost_avoided_usd.toFixed(2)}`} />
          <div className="control__help">
            Energy is measured from {data.gpu || 'the GPU'}'s own counter while each reply is
            written. The cloud figure is what the same tokens would cost at {data.cloud_reference}.
          </div>
        </>
      )}
    </div>
  );
}

/**
 * Which model does the thinking, and where it runs.
 *
 * JARVIS talks to Ollama directly. Any other local server that speaks the
 * OpenAI API -- LM Studio, llama.cpp, vLLM, Jan -- can be put in front of
 * it; Ollama then stays as the fallback. Below that, every model Ollama has,
 * measured against the GPU's memory: one that does not fit runs several
 * times slower because part of it spills into system memory.
 *
 * Ported idea: OpenJarvis's engine discovery and model-to-hardware fit.
 */
function ModelServers({ current, onChoose }: {
  current: { enabled?: boolean; base_url?: string; model?: string; label?: string };
  onChoose: (server: { enabled: boolean; base_url: string; model: string; label: string }) => void;
}) {
  const [data, setData] = useState<any>(null);
  const [testing, setTesting] = useState('');
  const [result, setResult] = useState<Record<string, string>>({});
  const [manual, setManual] = useState({ base_url: '', model: '' });

  const load = useCallback(() => {
    api.get('/ai/engines').then(setData).catch(() => setData({ engines: [], ollama_models: [] }));
  }, []);
  useEffect(() => { load(); }, [load]);

  const test = async (base_url: string, model: string, key: string) => {
    setTesting(key);
    try {
      const reply = await api.post<any>('/ai/engines/test', { base_url, model });
      setResult((r) => ({ ...r, [key]: reply.ok
        ? `Answered “${reply.reply}” in ${(reply.latency_ms / 1000).toFixed(1)} s`
        : `Failed: ${reply.error}` }));
      return reply.ok as boolean;
    } finally {
      setTesting('');
    }
  };

  const use = async (label: string, base_url: string, model: string) => {
    const key = `${base_url}|${model}`;
    if (await test(base_url, model, key)) onChoose({ enabled: true, base_url, model, label });
  };

  const others = (data?.engines ?? []).filter((e: any) => !e.native);
  const using = current.enabled && current.base_url;
  const manualKey = `${manual.base_url.trim()}|${manual.model.trim()}`;

  return (
    <div className="panel__section">
      <div className="panel__section-title">Model servers</div>
      <div className="control__help">
        {using
          ? <>Using <strong>{current.label || 'a local server'}</strong> ({current.model || 'its loaded model'})
              at {current.base_url}, with Ollama as the fallback.</>
          : <>Using Ollama directly. Any other local server that speaks the OpenAI API — LM Studio,
              llama.cpp, vLLM, Jan — can be used instead; start it and it shows up here.</>}
      </div>
      {using && (
        <button className="btn btn--small" onClick={() => onChoose({
          enabled: false, base_url: current.base_url ?? '', model: current.model ?? '',
          label: current.label ?? '' })}>Go back to Ollama</button>
      )}

      {data && others.length === 0 && (
        <div className="control__help">No other model server is running on the usual ports.</div>
      )}
      {others.map((engine: any) => (
        <div className="plugin" key={engine.base_url}>
          <div className="plugin__head">
            <span className="wake-dot wake-dot--ok" />
            <span className="plugin__name">{engine.name}</span>
            <span className="row__meta">{engine.base_url}</span>
          </div>
          {engine.models.length === 0 && <div className="control__help">No model loaded.</div>}
          {engine.models.slice(0, 8).map((model: string) => {
            const key = `${engine.base_url}|${model}`;
            const chosen = using && current.base_url === engine.base_url && current.model === model;
            return (
              <div className="control" key={model}>
                <div>
                  <div className="control__label" style={{ fontFamily: 'var(--mono)', fontSize: '0.72rem' }}>{model}</div>
                  {result[key] && <div className="control__help">{result[key]}</div>}
                </div>
                <button className="mini-action" disabled={!!testing || !!chosen}
                        onClick={() => void use(engine.name, engine.base_url, model)}>
                  {chosen ? 'in use' : testing === key ? 'testing…' : 'use'}
                </button>
              </div>
            );
          })}
        </div>
      ))}

      <details className="control__help">
        <summary>Another address</summary>
        <div className="control" style={{ gap: '0.4rem', marginTop: '0.4rem' }}>
          <input className="text-input" style={{ flex: 2, maxWidth: 'none' }} placeholder="http://127.0.0.1:1234/v1"
                 value={manual.base_url} onChange={(e) => setManual({ ...manual, base_url: e.target.value })}
                 aria-label="Server address" />
          <input className="text-input" style={{ flex: 1, maxWidth: 'none' }} placeholder="model (optional)"
                 value={manual.model} onChange={(e) => setManual({ ...manual, model: e.target.value })}
                 aria-label="Model name" />
          <button className="mini-action" disabled={!manual.base_url.trim() || !!testing}
                  onClick={() => void use('Local server', manual.base_url.trim(), manual.model.trim())}>
            {testing === manualKey ? 'testing…' : 'use'}
          </button>
        </div>
        {result[manualKey] && <div>{result[manualKey]}</div>}
      </details>

      {data?.ollama_models?.length > 0 && (
        <>
          <div className="control__label control__label--small" style={{ marginTop: '0.8rem' }}>
            Your Ollama models{data.gpu_memory ? ` on a ${data.gpu_memory.total} GB GPU` : ''}
          </div>
          {data.ollama_models.map((model: any) => (
            <div className="control" key={model.name}>
              <div className="control__label" style={{ fontFamily: 'var(--mono)', fontSize: '0.72rem' }}>
                {model.name}
              </div>
              <span className={`row__meta fit fit--${model.fit}`}
                    title={model.fit === 'spills'
                      ? 'Too big for the GPU: part of it runs from system memory, several times slower.'
                      : model.fit === 'tight' ? 'Only just fits: a long conversation may slow it down.'
                      : 'Fits in GPU memory with room for its working cache.'}>
                {model.parameters} · {model.size_gb} GB · {model.fit === 'fits' ? 'fits'
                  : model.fit === 'tight' ? 'tight fit' : model.fit === 'spills' ? 'too big for the GPU' : '—'}
              </span>
            </div>
          ))}
        </>
      )}
    </div>
  );
}

// ===========================================================================
function IntegrationsPanel() {
  const [integrations, setIntegrations] = useState<any[]>([]);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const result = await api.get<{ integrations: any[] }>('/integrations');
      setIntegrations(result.integrations);
    } catch { /* transient */ }
  }, []);

  useEffect(() => { void load(); }, [load]);

  return (
    <>
      <div className="panel__section-title">
        {integrations.filter((i) => i.connected).length} connected
        <button className="mini-action" style={{ float: 'right' }} disabled={busy}
                onClick={async () => {
                  setBusy(true);
                  await api.post('/integrations/refresh');
                  await load();
                  setBusy(false);
                }}>
          {busy ? 'Checking…' : 'Re-check all'}
        </button>
      </div>

      {integrations.map((integration) => (
        <div className="row" key={integration.key}>
          <div className="row__head">
            <span className="row__title">{integration.name}</span>
            <span className={`status-chip status-chip--${
              integration.status === 'connected' ? 'completed'
                : integration.status === 'error' ? 'failed'
                : integration.status === 'not_implemented' ? 'cancelled' : 'queued'}`}>
              {String(integration.status).replace('_', ' ')}
            </span>
          </div>
          <div className="row__sub">{integration.description}</div>
          {integration.message && (
            <div className="row__sub" style={{
              color: integration.status === 'connected' ? 'var(--ok)' : 'var(--ink-ghost)',
            }}>
              {integration.message}
            </div>
          )}
          {integration.missing_env?.length > 0 && (
            <div className="row__sub" style={{ fontFamily: 'var(--mono)', fontSize: '0.7rem' }}>
              Add to .env: {integration.missing_env.join(', ')}
              {integration.docs_url && (
                <> · <a href={integration.docs_url} target="_blank" rel="noreferrer">get credentials</a></>
              )}
            </div>
          )}
          <div className="row__actions">
            <button className="mini-action" onClick={async () => {
              await api.post(`/integrations/${integration.key}/refresh`);
              await load();
            }}>Re-check</button>
            {integration.connected && (
              <button className="mini-action mini-action--danger" onClick={async () => {
                await api.post(`/integrations/${integration.key}/disconnect`);
                await load();
              }}>Disconnect</button>
            )}
          </div>
        </div>
      ))}
    </>
  );
}

// ===========================================================================
function HistoryPanel() {
  const [conversations, setConversations] = useState<any[]>([]);
  const [messages, setMessages] = useState<any[]>([]);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    void (async () => {
      try {
        const result = await api.get<{ conversations: any[] }>('/ai/conversations');
        setConversations(result.conversations);
      } catch { /* transient */ }
    })();
  }, []);

  const open = async (id: string) => {
    setSelected(id);
    const result = await api.get<{ messages: any[] }>(`/ai/conversations/${id}`);
    setMessages(result.messages.filter((m) => m.text));
  };

  if (selected) {
    return (
      <>
        <button className="mini-action" onClick={() => setSelected(null)} style={{ marginBottom: '1.2rem' }}>
          ← Back to all conversations
        </button>
        {messages.map((message) => (
          <div className="row" key={message.id}>
            <div className="utterance__who">{message.role === 'assistant' ? 'JARVIS' : 'YOU'}</div>
            <div style={{ fontSize: '0.86rem', whiteSpace: 'pre-wrap' }}>{message.text}</div>
            <div className="row__meta" style={{ marginTop: '0.35rem' }}>
              {relative(message.created_at)}{message.model && ` · ${message.model}`}
            </div>
          </div>
        ))}
      </>
    );
  }

  return (
    <>
      {conversations.length === 0 && <p className="empty">No conversations yet.</p>}
      {conversations.map((conversation) => (
        <div className="row" key={conversation.id}>
          <div className="row__head">
            <button className="row__title" style={{ textAlign: 'left' }}
                    onClick={() => void open(conversation.id)}>
              {conversation.title}
            </button>
            <span className="row__meta">{conversation.message_count} msg</span>
          </div>
          <div className="row__actions">
            <span className="row__meta">{relative(conversation.updated_at)}</span>
            <button className="mini-action mini-action--danger" onClick={async () => {
              await api.del(`/ai/conversations/${conversation.id}`);
              setConversations((current) => current.filter((c) => c.id !== conversation.id));
            }}>Delete</button>
          </div>
        </div>
      ))}
    </>
  );
}

// The names the setup screen shows, for the Settings summary line.
const PROVIDER_NAMES: Record<string, string> = {
  anthropic: 'Anthropic', openai: 'OpenAI', gemini: 'Google Gemini', groq: 'Groq',
  mistral: 'Mistral', xai: 'xAI', deepseek: 'DeepSeek', openrouter: 'OpenRouter',
  together: 'Together AI', custom: 'your OpenAI-compatible server',
};

// ===========================================================================
function SettingsPanel() {
  const settings = useStore((s) => s.settings);
  const update = useStore((s) => s.updateSetting);
  const [permissions, setPermissions] = useState<any[]>([]);
  const [modes, setModes] = useState<any[]>([]);
  const [themes, setThemes] = useState<any[]>([]);
  const [micReport, setMicReport] = useState<any>(null);
  const [activeTheme, setActiveTheme] = useState('arc_reactor');

  useEffect(() => {
    void (async () => {
      try {
        const [permissionResult, modeResult, themeResult] = await Promise.all([
          api.get<{ permissions: any[] }>('/permissions'),
          api.get<{ modes: any[] }>('/modes'),
          api.get<{ themes: any[]; active: string }>('/themes'),
        ]);
        setPermissions(permissionResult.permissions);
        setModes(modeResult.modes);
        setThemes(themeResult.themes);
        setActiveTheme(themeResult.active);
      } catch { /* transient */ }
    })();
  }, []);

  const patch = (key: string, changes: Record<string, any>) =>
    update(key, { ...(settings[key] ?? {}), ...changes });

  const voiceSettings = settings.voice ?? {};
  const particles = settings.particles ?? {};
  const theme = settings.theme ?? {};
  const personality = settings.personality ?? {};
  const privacy = settings.privacy ?? {};
  const aiSettings = settings.ai ?? {};
  const awareness = settings.awareness ?? {};
  const gaming = settings.gaming ?? {};

  return (
    <>
      <div className="panel__section">
        <div className="panel__section-title">Mode</div>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.45rem' }}>
          {modes.map((mode) => (
            <button
              key={mode.key}
              className={`btn ${mode.active ? 'btn--primary' : ''}`}
              style={{ padding: '0.35rem 0.8rem', fontSize: '0.76rem' }}
              onClick={async () => {
                await api.post(`/modes/${mode.key}/activate`);
                setModes((current) => current.map((m) => ({ ...m, active: m.key === mode.key })));
              }}
            >
              {mode.name}
            </button>
          ))}
        </div>
      </div>

      <div className="panel__section">
        <div className="panel__section-title">Voice</div>
        <Toggle label="Voice enabled" help="Speech output and recognition"
                value={voiceSettings.enabled !== false}
                onChange={(v) => patch('voice', { enabled: v })} />
        <Toggle label="Wake word" help={`Say "${settings.wake_word ?? 'jarvis'}" to talk without clicking`}
                value={!!voiceSettings.wake_word_enabled}
                onChange={(v) => patch('voice', { wake_word_enabled: v })} />
        <WakeWordStatus />
        <MicrophonePicker onChoose={(index) => patch('voice', { wake_device: index })} />
        <WakeWordTest />
        <VoiceInUse engine={voiceSettings.tts_engine ?? 'edge'}
                    configured={voiceSettings.voice ?? ''} />
        <div className="control">
          <div>
            <div className="control__label">Speech engine</div>
            <div className="control__help">Server voice sounds better; browser voice has zero latency</div>
          </div>
          <select className="select" value={voiceSettings.tts_engine ?? 'edge'}
                  onChange={(e) => patch('voice', { tts_engine: e.target.value })}>
            <option value="edge">Server (edge-tts)</option>
            <option value="browser">Browser</option>
          </select>
        </div>
        <Range label="Speaking rate" min={0.5} max={1.8} step={0.1}
               value={voiceSettings.rate ?? 1}
               onChange={(v) => patch('voice', { rate: v })} />

        <div className="control">
          <div>
            <div className="control__label">Browser microphone access</div>
            <div className="control__help">
              {micReport
                ? micReport.microphone_ok
                  ? `Working — ${micReport.input_devices} input device(s) detected`
                  : micReport.microphone_detail
                : 'Whether this page can record — used for commands, not the wake word'}
            </div>
          </div>
          <button className="mini-action" onClick={async () => {
            const report = await voice.diagnose();
            setMicReport(report);
            if (!report.recognition_supported) {
              useStore.getState().notify(report.browser_note);
            } else if (!report.microphone_ok) {
              useStore.getState().notify(report.microphone_detail);
            } else if (!report.online) {
              useStore.getState().notify(report.network_note);
            } else {
              useStore.getState().notify('Microphone and speech recognition are working.');
            }
          }}>Test</button>
        </div>

        {micReport && (
          <div className="confirm__detail" style={{ marginTop: '0.5rem' }}>
            {[
              `recognition   ${micReport.recognition_supported ? 'available' : 'NOT SUPPORTED — needs Chrome or Edge'}`,
              `secure page   ${micReport.secure_context ? 'yes' : 'NO — use http://localhost:3000'}`,
              `microphone    ${micReport.microphone_ok ? 'accessible' : 'blocked'}`,
              `input devices ${micReport.input_devices}`,
              `network       ${micReport.online ? 'online' : 'OFFLINE — recognition needs internet'}`,
            ].join('\n')}
          </div>
        )}
      </div>

      <VoiceTrace />

      <div className="panel__section">
        <div className="panel__section-title">Theme</div>
        <p style={{ fontSize: '0.76rem', color: 'var(--ink-ghost)', marginBottom: '0.9rem' }}>
          Themes retint the particle field itself, not just the accents.
        </p>
        <div className="theme-grid">
          {themes.map((preset) => (
            <button
              key={preset.key}
              className={`theme-swatch ${activeTheme === preset.key ? 'theme-swatch--active' : ''}`}
              title={preset.description}
              aria-pressed={activeTheme === preset.key}
              onClick={async () => {
                const result = await api.post<{ theme: any }>(`/themes/${preset.key}/apply`);
                setActiveTheme(preset.key);
                await update('theme', result.theme);
              }}
            >
              <span
                className="theme-swatch__preview"
                style={{
                  background: `radial-gradient(circle at 34% 34%, ${preset.accent} 0%, ` +
                    `${preset.particle_near} 38%, ${preset.particle_far} 68%, ${preset.backdrop} 100%)`,
                }}
              />
              <span className="theme-swatch__name">{preset.name}</span>
            </button>
          ))}
        </div>
        <Toggle label="High contrast" help="Brighter text throughout"
                value={!!theme.high_contrast}
                onChange={(v) => patch('theme', { high_contrast: v })} />
        <Range label="Text size" min={0.85} max={1.4} step={0.05}
               value={theme.font_scale ?? 1}
               onChange={(v) => patch('theme', { font_scale: v })} />
      </div>

      <div className="panel__section">
        <div className="panel__section-title">Particle field</div>
        <div className="control">
          <div>
            <div className="control__label">Quality</div>
            <div className="control__help">Auto scales to your display and CPU</div>
          </div>
          <select className="select" value={particles.quality ?? 'auto'}
                  onChange={(e) => patch('particles', { quality: e.target.value })}>
            <option value="auto">Auto</option>
            <option value="high">High</option>
            <option value="balanced">Balanced</option>
            <option value="low">Low</option>
          </select>
        </div>
        <Toggle label="Connections" help="Draw links between nearby particles"
                value={particles.connections !== false}
                onChange={(v) => patch('particles', { connections: v })} />
        <Toggle label="Reduced motion" help="Calmer movement throughout"
                value={!!theme.reduced_motion}
                onChange={(v) => patch('theme', { reduced_motion: v })} />
        <div className="control">
          <div>
            <div className="control__label">Custom accent</div>
            <div className="control__help">Overrides the theme&rsquo;s core colour</div>
          </div>
          <input type="color" className="text-input" style={{ padding: '2px', width: '52px' }}
                 value={theme.accent ?? '#5ad2ff'}
                 aria-label="Custom accent colour"
                 onChange={(e) => patch('theme', { accent: e.target.value })} />
        </div>
      </div>

      <div className="panel__section">
        <div className="panel__section-title">Personality</div>
        <div className="control">
          <div className="control__label">Tone</div>
          <select className="select" value={personality.tone ?? 'composed'}
                  onChange={(e) => patch('personality', { tone: e.target.value })}>
            <option value="composed">Composed</option>
            <option value="warm">Warm</option>
            <option value="terse">Terse</option>
            <option value="playful">Playful</option>
          </select>
        </div>
        <div className="control">
          <div className="control__label">Response length</div>
          <select className="select" value={personality.response_length ?? 'concise'}
                  onChange={(e) => patch('personality', { response_length: e.target.value })}>
            <option value="concise">Concise</option>
            <option value="balanced">Balanced</option>
            <option value="detailed">Detailed</option>
          </select>
        </div>
        <div className="control" style={{ flexDirection: 'column', alignItems: 'stretch' }}>
          <div className="control__label" style={{ marginBottom: '0.4rem' }}>Standing instructions</div>
          <textarea className="text-input" rows={3} style={{ maxWidth: '100%', width: '100%' }}
                    defaultValue={personality.custom_instructions ?? ''}
                    onBlur={(e) => patch('personality', { custom_instructions: e.target.value })}
                    placeholder="e.g. Always give me the short answer first." />
        </div>
      </div>

      <div className="panel__section">
        <div className="panel__section-title">Awareness</div>
        <p style={{ fontSize: '0.76rem', color: 'var(--ink-ghost)', marginBottom: '0.9rem' }}>
          What JARVIS notices about how you use this computer. All of it stays on this machine.
        </p>
        <Toggle label="Ambient awareness"
                help="Track the focused app and idle time, so JARVIS understands 'this' and 'here'"
                value={awareness.enabled !== false}
                onChange={(v) => patch('awareness', { enabled: v })} />
        <Toggle label="Learn my habits"
                help="Remember which commands you use and when, to predict what you'll want"
                value={awareness.learning !== false}
                onChange={(v) => patch('awareness', { learning: v })} />
        <Toggle label="Proactive suggestions"
                help="Offer things unprompted. Always an offer with a button — never an action already taken"
                value={!!awareness.proactive}
                onChange={(v) => patch('awareness', { proactive: v })} />
        {awareness.proactive && (
          <Range label="Max suggestions per hour" min={1} max={8} step={1}
                 value={awareness.max_suggestions_per_hour ?? 3}
                 onChange={(v) => patch('awareness', { max_suggestions_per_hour: v })} />
        )}
        <Toggle label="Screen vision"
                help="Let JARVIS look at your screen when asked. Still needs the screenshot permission and confirms each time"
                value={awareness.vision !== false}
                onChange={(v) => patch('awareness', { vision: v })} />
        <div className="control">
          <div>
            <div className="control__label">Learned habits</div>
            <div className="control__help">Everything JARVIS has inferred about your routine</div>
          </div>
          <button className="mini-action mini-action--danger" onClick={async () => {
            if (!window.confirm('Delete everything JARVIS has learned about your habits?')) return;
            await api.del('/patterns');
            useStore.getState().notify('Learned habits deleted.');
          }}>Clear</button>
        </div>
        <div className="control">
          <div>
            <div className="control__label">Stored screenshots</div>
            <div className="control__help">Images captured for vision questions</div>
          </div>
          <button className="mini-action mini-action--danger" onClick={async () => {
            if (!window.confirm('Delete all stored screenshots?')) return;
            const result = await api.del<{ files_deleted: number }>(
              '/vision/observations?keep_days=0');
            useStore.getState().notify(`Deleted ${result.files_deleted} screenshot(s).`);
          }}>Delete all</button>
        </div>
      </div>

      <div className="panel__section">
        <div className="panel__section-title">Gaming</div>
        <Toggle label="Detect games"
                help="Recognise when a game is running and track playtime"
                value={gaming.enabled !== false}
                onChange={(v) => patch('gaming', { enabled: v })} />
        <Toggle label="Auto gaming mode"
                help="Switch to gaming mode automatically, and back again afterwards"
                value={!!gaming.auto_mode}
                onChange={(v) => patch('gaming', { auto_mode: v })} />
      </div>

      <ModelServers current={aiSettings.local_server ?? {}}
                    onChoose={(server) => patch('ai', { local_server: server })} />

      <div className="panel__section">
        <div className="panel__section-title">Privacy</div>
        <Toggle label="Privacy mode" help="Local model only, memory paused, no external integrations"
                value={!!privacy.privacy_mode}
                onChange={(v) => patch('privacy', { privacy_mode: v })} />
        <Toggle label="Memory" help="Let JARVIS remember things about you"
                value={privacy.memory !== false}
                onChange={(v) => patch('privacy', { memory: v })} />
        <Toggle label="Learn about me automatically"
                help="Keep lasting facts you mention — likes, projects, people, routines — without being asked. Only from what you say, never secrets; each is marked “learned” in Memory and can be deleted."
                value={aiSettings.learn_automatically !== false}
                onChange={(v) => patch('ai', { learn_automatically: v })} />
        <Toggle label="Cloud AI" help="When off, only the local Ollama model is used"
                value={privacy.cloud_ai !== false}
                onChange={(v) => patch('privacy', { cloud_ai: v })} />
      </div>

      <div className="panel__section">
        <div className="panel__section-title">AI provider</div>
        <div className="control control--stack">
          <div className="control__help">
            {aiSettings.cloud?.provider && aiSettings.cloud.provider !== 'ollama'
              ? <>Using <strong>{PROVIDER_NAMES[aiSettings.cloud.provider] ?? aiSettings.cloud.provider}</strong> · {aiSettings.cloud.model || 'no model chosen'}</>
              : 'Using local models (Ollama).'}
          </div>
          <div>
            <button className="btn btn--small"
                    onClick={() => useStore.getState().setSetupOpen(true)}>
              Change AI, key or name
            </button>
          </div>
        </div>
      </div>

      <div className="panel__section">
        <div className="panel__section-title">Capabilities</div>
        <p style={{ fontSize: '0.76rem', color: 'var(--ink-ghost)', marginBottom: '0.9rem' }}>
          What JARVIS may do on this computer. Risky actions still ask before they
          run, and unattended work holds anything it would have asked about rather
          than deciding alone.
        </p>
        {permissions.map((permission) => (
          <Toggle key={permission.key}
                  label={permission.label}
                  help={`${permission.description} (${permission.risk} risk)`}
                  value={!!permission.enabled}
                  onChange={async (v) => {
                    try {
                      await api.put(`/permissions/${permission.key}`, { enabled: v });
                      setPermissions((list) => list.map((p) =>
                        p.key === permission.key ? { ...p, enabled: v } : p));
                    } catch (exc) {
                      useStore.getState().notify(
                        exc instanceof Error ? exc.message : 'Could not change that permission.');
                    }
                  }} />
        ))}
        <div style={{ marginTop: '0.8rem' }}>
          <button className="btn btn--small btn--danger" onClick={async () => {
            if (!window.confirm('Switch every capability off now?')) return;
            await api.post('/permissions/revoke-all');
            const result = await api.get<{ permissions: any[] }>('/permissions');
            setPermissions(result.permissions);
            useStore.getState().notify('Every capability is now off.');
          }}>Switch everything off</button>
        </div>
      </div>

      <div className="panel__section">
        <div className="panel__section-title">Your data</div>
        <div style={{ display: 'flex', gap: '0.6rem', flexWrap: 'wrap' }}>
          <a className="btn" href="/api/export" download>Export everything</a>
          <button className="btn" onClick={async () => {
            const result = await api.post<{ task_id: string }>('/backups');
            useStore.getState().notify(`Backup started (task ${result.task_id}).`);
          }}>Back up now</button>
          <button className="btn btn--danger" onClick={async () => {
            if (!window.confirm('Delete all stored memory? This cannot be undone.')) return;
            await api.del('/memory');
            useStore.getState().notify('Memory deleted.');
          }}>Delete memory</button>
        </div>
      </div>
    </>
  );
}

// ===========================================================================
// small shared pieces
// ===========================================================================
/**
 * Live state of the wake-word loop.
 *
 * Without this the feature can only be debugged by guessing: an armed loop that
 * is mishearing you looks exactly like a loop that never started. `heard` shows
 * the recogniser's raw output, so you can see whether the audio is arriving and
 * what it is being turned into.
 */
/**
 * Which voice is actually speaking, and whether this tab is the one doing it.
 *
 * Worth stating plainly: with several tabs open JARVIS used to answer in all
 * of them at once, each having resolved a different browser voice, which
 * sounded like a room full of assistants. One tab holds the speakers now, and
 * the voice is resolved once rather than per utterance -- but "it should be
 * one voice" is a claim the interface ought to be able to back up.
 */
function VoiceInUse({ engine, configured }: { engine: string; configured: string }) {
  const [browserVoice, setBrowserVoice] = useState(voice.currentBrowserVoice);
  const [owns, setOwns] = useState(speakerLease.shouldSpeak);
  const [fallback, setFallback] = useState(voice.engineFallback);

  useEffect(() => {
    const timer = window.setInterval(() => {
      setBrowserVoice(voice.currentBrowserVoice);
      setOwns(speakerLease.shouldSpeak);
      setFallback(voice.engineFallback);
    }, 2000);
    return () => window.clearInterval(timer);
  }, []);

  const name = engine === 'browser'
    ? (browserVoice || 'the browser default (not yet resolved)')
    : (configured || 'the server default');

  return (
    <div className="control control--stack">
      <div className="control__label">Voice in use</div>
      <div className="control__help control__help--mono">{name}</div>
      <div className="control__help">
        {owns
          ? 'This tab is the one that speaks.'
          : 'Another JARVIS tab currently holds the speakers, so this one stays quiet.'}
      </div>
      {engine !== 'browser' && fallback && (
        <div className="control__help control__help--bad">
          The server voice is unavailable ({fallback}), so the browser voice is being
          used instead — which is why it sounds different.
        </div>
      )}
    </div>
  );
}

function WakeWordStatus() {
  const status = useStore((s) => s.wakeStatus);

  const label = {
    off: 'Off', starting: 'Starting…', armed: 'Listening',
    triggered: 'Heard you', silent: 'Hearing nothing', failed: 'Not working',
  }[status.phase];

  const tone = status.phase === 'armed' || status.phase === 'triggered' ? 'ok'
             : status.phase === 'failed' ? 'bad'
             : status.phase === 'silent' ? 'warn' : 'idle';

  const engine = status.engine === 'local' ? 'local keyword spotter'
               : status.engine === 'browser' ? 'browser speech recognition'
               : '';

  const detail = status.detail
    || (status.heard ? `Last heard: "${status.heard}"` : '')
    || (status.phase === 'armed' ? 'Listening — nothing heard yet.' : '');

  return (
    <div className="control control--stack">
      <div className="control__label">
        Wake word <span className={`wake-dot wake-dot--${tone}`} /> {label}
        {status.lastTrigger && (
          <span className="control__meta">
            {' · fired '}
            {Math.max(0, Math.round((Date.now() - status.lastTrigger) / 1000))}s ago
          </span>
        )}
      </div>

      {detail && <div className="control__help">{detail}</div>}

      {/* The live level is the whole point. A moving bar proves audio is
          arriving; a flat one says the microphone is the problem, which is
          exactly what could not be seen before. */}
      {(status.phase === 'armed' || status.phase === 'silent'
        || status.phase === 'triggered') && (
        <>
          <div className="level">
            <div className="level__fill" style={{ width: `${Math.min(100, status.level * 100)}%` }} />
            {status.score > 0.02 && (
              <div className="level__score" style={{ left: `${Math.min(98, status.score * 100)}%` }} />
            )}
          </div>
          <div className="control__help control__help--mono">
            {engine}
            {status.device ? ` · ${status.device}` : ''}
            {` · level ${(status.level * 100).toFixed(0)}%`}
            {status.score > 0.02 ? ` · match ${(status.score * 100).toFixed(0)}%` : ''}
          </div>
        </>
      )}
    </div>
  );
}

/**
 * Every microphone, opened and measured.
 *
 * A list of device names would be useless here. On the machine this was
 * written for, 25 of 26 inputs open successfully and return digital silence —
 * the Windows default among them — which is why the wake word appeared to run
 * while being completely deaf. `signal` is the column that matters.
 */
function MicrophonePicker({ onChoose }: { onChoose: (index: number) => void }) {
  const [devices, setDevices] = useState<any[] | null>(null);
  const [note, setNote] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const hotword = useStore((s) => s.hotword);
  const startHotword = useStore((s) => s.startHotword);

  const scan = async () => {
    setBusy(true);
    setError('');
    try {
      const result = await api.get<{ devices: any[]; note: string }>('/voice/devices');
      setDevices(result.devices);
      setNote(result.note);
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const choose = async (index: number) => {
    onChoose(index);
    await startHotword({ device: index });
  };

  const current = hotword?.device?.index;

  return (
    <div className="control control--stack">
      <div className="control__label">Microphone</div>
      <div className="control__help">
        Each input is opened and measured, because a device that opens and
        returns silence looks exactly like a room with nobody in it.
      </div>

      <button className="btn btn--small" onClick={() => void scan()} disabled={busy}>
        {busy ? 'Testing each input…' : devices ? 'Test again' : 'Test microphones'}
      </button>

      {error && <div className="control__help control__help--bad">{error}</div>}
      {note && <div className="control__help">{note}</div>}

      {devices && (
        <div className="devices">
          {devices.map((device) => (
            <button
              key={device.index}
              className={`devices__row ${device.index === current ? 'devices__row--active' : ''}`}
              onClick={() => void choose(device.index)}
              disabled={!device.opens}
              title={device.detail}
            >
              <span className={`wake-dot wake-dot--${device.signal ? 'ok' : device.opens ? 'warn' : 'bad'}`} />
              <span className="devices__name">{device.name}</span>
              <span className="devices__meta">
                {device.default ? 'default · ' : ''}
                {device.opens ? (device.signal ? `signal ${device.rms.toFixed(0)}` : 'silent') : 'cannot open'}
              </span>
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * Run the real wake-word pipeline for ten seconds and report what happened.
 *
 * Deliberately the same code path as the live loop — microphone check, then
 * recognition — so a pass here means the feature works, not that a mock
 * passed. It reports what the recogniser actually returned, which is the one
 * piece of information you need when it hears you but does not match.
 */
/**
 * The recognition lifecycle, as it actually happened.
 *
 * A dropped voice session used to leave nothing behind: 'aborted' and
 * 'no-speech' are normal codes that are not worth a toast, so a session that
 * opened and died a second later was indistinguishable from one that never
 * opened. Every start, result, error and end is recorded, and this shows them
 * with timings so the cause is readable rather than guessed at.
 */
function VoiceTrace() {
  const [lines, setLines] = useState<string[]>([]);
  const [live, setLive] = useState(false);

  useEffect(() => {
    if (!live) return;
    const timer = window.setInterval(() => setLines(voice.traceLines()), 500);
    return () => window.clearInterval(timer);
  }, [live]);

  return (
    <div className="panel__section">
      <div className="panel__section-title">Voice log</div>
      <div className="control">
        <div>
          <div className="control__label">Recognition events</div>
          <div className="control__help">
            Every start, result, error and end, with timings. Turn this on, use the
            microphone, and the reason a session ended is in the list.
          </div>
        </div>
        <button className="mini-action" onClick={() => {
          setLines(voice.traceLines());
          setLive((on) => !on);
        }}>
          {live ? 'Stop' : 'Watch'}
        </button>
      </div>
      {lines.length > 0 && (
        <pre className="voice-trace">{lines.join(String.fromCharCode(10))}</pre>
      )}
    </div>
  );
}

function WakeWordTest() {
  const [running, setRunning] = useState(false);
  const [report, setReport] = useState<string>('');
  const wakeWord = useStore((s) => s.settings?.wake_word ?? 'jarvis');
  const hotword = useStore((s) => s.hotword);

  /**
   * Watch the local keyword spotter for ten seconds and report what it saw.
   *
   * It has to test the engine that is actually running. Testing the browser's
   * speech recognition while the backend spotter does the listening would
   * report failure for a working wake word and success for an unused path --
   * a test that tells you about something other than the thing you asked
   * about is worse than no test.
   */
  const runLocal = async () => {
    setRunning(true);
    const refresh = useStore.getState().refreshHotword;
    const before = (await refresh())?.detections ?? 0;

    setReport(`Listening for 10 seconds — say "hey ${wakeWord}" now.`);
    let peakLevel = 0;
    let peakScore = 0;

    const timer = window.setInterval(async () => {
      const status = await refresh();
      if (!status) return;
      peakLevel = Math.max(peakLevel, status.level);
      peakScore = Math.max(peakScore, status.score);
    }, 400);

    window.setTimeout(async () => {
      window.clearInterval(timer);
      const status = await refresh();
      setRunning(false);

      const fired = (status?.detections ?? before) - before;
      if (fired > 0) {
        setReport(`Heard it. ${fired} detection${fired > 1 ? 's' : ''}, `
                + `best confidence ${(peakScore * 100).toFixed(0)}%.`);
      } else if (peakLevel < 0.08) {
        setReport('No sound reached the microphone at all — peak level was '
                + `${(peakLevel * 100).toFixed(0)}%. The input is muted, unplugged, or `
                + 'virtual. Use "Test microphones" above to pick one that carries audio.');
      } else if (peakScore > 0.15) {
        setReport(`Audio arrived (peak level ${(peakLevel * 100).toFixed(0)}%) and the best `
                + `match reached ${(peakScore * 100).toFixed(0)}%, short of the `
                + `${((status?.threshold ?? 0.5) * 100).toFixed(0)}% threshold. Say it a little `
                + 'more clearly, or lower the sensitivity.');
      } else {
        setReport(`Audio arrived (peak level ${(peakLevel * 100).toFixed(0)}%) but nothing `
                + `resembling the wake word. Say "hey ${wakeWord}" as one phrase.`);
      }
    }, 10000);
  };

  const run = async () => {
    // The local spotter is the engine when it is running; only fall through to
    // the browser path when that is genuinely what is listening.
    if (hotword?.running && hotword.phase !== 'failed') {
      await runLocal();
      return;
    }

    setRunning(true);
    setReport('Checking the microphone…');

    const microphone = await voice.checkMicrophone();
    if (!microphone.ok) {
      setReport(microphone.detail ?? 'The microphone is unavailable.');
      setRunning(false);
      return;
    }
    if (!voice.recognitionSupported) {
      setReport('This browser has no speech recognition. Chrome or Edge is required.');
      setRunning(false);
      return;
    }

    setReport(`Listening for 10 seconds — say "${wakeWord}" now.`);
    const heard: string[] = [];
    let matched = false;

    const started = voice.startListening(
      (transcript, final) => {
        if (final) heard.push(transcript);
        if (matchWakeWord(transcript, wakeWord).hit) matched = true;
      },
      undefined, true, 'command',
      (message) => { setReport(message); },
    );

    if (!started) {
      setReport(voice.lastError ?? 'Recognition would not start.');
      setRunning(false);
      return;
    }

    window.setTimeout(() => {
      voice.stopListening();
      setRunning(false);
      if (matched) {
        setReport(`Matched. Heard: "${heard.join(' ') || '(interim only)'}"`);
      } else if (heard.length) {
        setReport(`Heard "${heard.join(' ')}" but that did not match "${wakeWord}". `
                + 'Try speaking the wake word on its own, more clearly.');
      } else {
        setReport('No speech was recognised at all. Check the microphone is the default '
                + 'input device and that nothing else is using it.');
      }
    }, 10000);
  };

  return (
    <div className="control">
      <div>
        <div className="control__label">Test the wake word</div>
        <div className="control__help">
          {report
            || (hotword?.running
              ? 'Watches the local keyword spotter for ten seconds and reports what it heard.'
              : 'Runs the browser speech fallback for ten seconds and reports what it heard.')}
        </div>
      </div>
      <button className="mini-action" onClick={() => void run()} disabled={running}>
        {running ? 'Listening…' : 'Test'}
      </button>
    </div>
  );
}

function Toggle({ label, help, value, onChange }: {
  label: string; help?: string; value: boolean; onChange: (value: boolean) => void;
}) {
  return (
    <div className="control">
      <div>
        <div className="control__label">{label}</div>
        {help && <div className="control__help">{help}</div>}
      </div>
      <button className={`switch ${value ? 'switch--on' : ''}`} aria-label={label} aria-pressed={value}
              onClick={() => onChange(!value)} />
    </div>
  );
}

function Range({ label, value, onChange, min, max, step }: {
  label: string; value: number; onChange: (value: number) => void;
  min: number; max: number; step: number;
}) {
  return (
    <div className="control">
      <div className="control__label">{label}</div>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.7rem' }}>
        <input className="slider" type="range" min={min} max={max} step={step} value={value}
               aria-label={label}
               onChange={(event) => onChange(Number(event.target.value))} />
        <span className="row__meta">{value.toFixed(1)}</span>
      </div>
    </div>
  );
}

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="control">
      <div className="control__label">{label}</div>
      <span className="row__meta metric__value">{value}</span>
    </div>
  );
}

// ---------------------------------------------------------------------------
function relative(iso?: string | null): string {
  if (!iso) return '';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 0) return `in ${duration(-seconds)}`;
  if (seconds < 45) return 'just now';
  return `${duration(seconds)} ago`;
}

function duration(seconds?: number): string {
  if (seconds == null) return '—';
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

function formatWhen(iso: string): string {
  const date = new Date(iso);
  const today = new Date();
  const sameDay = date.toDateString() === today.toDateString();
  const time = date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  if (sameDay) return `today ${time}`;
  return `${date.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' })} ${time}`;
}

function kb(value?: number): string {
  if (!value) return '0 KB/s';
  if (value > 1_048_576) return `${(value / 1_048_576).toFixed(1)} MB/s`;
  return `${(value / 1024).toFixed(0)} KB/s`;
}

function riskOf(priority: string): string {
  return priority === 'critical' ? 'critical' : priority === 'high' ? 'high'
    : priority === 'normal' ? 'medium' : 'low';
}
