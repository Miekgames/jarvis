import { useCallback, useEffect, useState } from 'react';

import { api, eventStream } from '../../lib/api';
import { useStore } from '../../state/store';

/**
 * Panels for the machinery that previously had an API but no surface: goals,
 * agents, research and self-editing.
 *
 * Same flowing style as the rest — rows separated by hairlines, no cards, no
 * grid. Everything here reflects real backend state; nothing is mocked.
 */

// ===========================================================================
export function GoalsPanel() {
  const [goals, setGoals] = useState<any[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [detail, setDetail] = useState<Record<string, any>>({});
  const [objective, setObjective] = useState('');
  const [deadline, setDeadline] = useState('');
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const result = await api.get<{ goals: any[] }>('/planner/goals');
      setGoals(result.goals);
    } catch { /* transient */ }
  }, []);

  useEffect(() => {
    void load();
    return eventStream.subscribe((event) => {
      if (event.type.startsWith('goal.')) void load();
    });
  }, [load]);

  const inspect = async (id: string) => {
    if (open === id) { setOpen(null); return; }
    setOpen(id);
    try {
      const state = await api.get(`/planner/goals/${id}`);
      setDetail((current) => ({ ...current, [id]: state }));
    } catch { /* transient */ }
  };

  const create = async () => {
    if (!objective.trim()) return;
    setBusy(true);
    try {
      await api.post('/planner/goals', {
        objective,
        deadline: deadline ? new Date(deadline).toISOString() : null,
        start_now: false,
      });
      setObjective(''); setDeadline('');
      await load();
    } catch (error) {
      useStore.getState().notify((error as Error).message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <div className="panel__section">
        <div className="panel__section-title">Set a goal</div>
        <p style={{ fontSize: '0.76rem', color: 'var(--ink-ghost)', marginBottom: '0.8rem' }}>
          An objective, not a command. JARVIS estimates the size, asks anything genuinely
          blocking, then breaks it into milestones with dates.
        </p>
        <input className="text-input" style={{ maxWidth: '100%', width: '100%',
                                               marginBottom: '0.5rem' }}
               placeholder="e.g. Get the landing page ready to ship"
               value={objective} onChange={(e) => setObjective(e.target.value)} />
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <input className="text-input" type="datetime-local" style={{ flex: 1, maxWidth: 'none' }}
                 value={deadline} onChange={(e) => setDeadline(e.target.value)} />
          <button className="btn btn--primary" disabled={busy} onClick={() => void create()}>
            {busy ? 'Planning…' : 'Plan it'}
          </button>
        </div>
      </div>

      <div className="panel__section">
        <div className="panel__section-title">Goals</div>
        {goals.length === 0 && <p className="empty">No goals yet.</p>}
        {goals.map((goal) => {
          const state = detail[goal.id];
          return (
            <div className="row" key={goal.id}>
              <div className="row__head">
                <button className="row__title" style={{ textAlign: 'left' }}
                        onClick={() => void inspect(goal.id)}>
                  {truncate(goal.objective, 70)}
                </button>
                <span className={`status-chip status-chip--${chipFor(goal.status)}`}>
                  {goal.status.replace('_', ' ')}
                </span>
              </div>
              {goal.complexity?.size && (
                <div className="row__sub">
                  {goal.complexity.size}, ~{goal.complexity.estimated_hours}h
                  {goal.deadline && ` · due ${formatWhen(goal.deadline)}`}
                </div>
              )}
              {goal.status === 'needs_input' && goal.questions?.length > 0 && (
                <div className="row__sub" style={{ color: 'var(--warn)' }}>
                  Waiting on: {goal.questions.join(' · ')}
                </div>
              )}

              {open === goal.id && state && (
                <>
                  <div className="progress" style={{ marginTop: '0.6rem' }}>
                    <div className="progress__bar" style={{ width: `${state.percent}%` }} />
                  </div>
                  <div className="row__sub" style={{ marginTop: '0.4rem' }}>
                    {state.completed_milestones}/{state.total_milestones} milestones
                  </div>
                  {state.milestones?.map((m: any) => (
                    <div key={m.id} style={{ margin: '0.55rem 0 0 0.7rem',
                                             borderLeft: '1px solid var(--surface-line)',
                                             paddingLeft: '0.7rem' }}>
                      <div className="row__head">
                        <span style={{ fontSize: '0.82rem',
                                       color: m.status === 'complete' ? 'var(--ok)'
                                            : m.overdue ? 'var(--danger)' : 'var(--ink-soft)' }}>
                          {m.n}. {m.title}
                        </span>
                        <span className="row__meta">{m.status}</span>
                      </div>
                      {m.due_at && (
                        <div className="row__meta">
                          due {formatWhen(m.due_at)}{m.overdue && ' — overdue'}
                        </div>
                      )}
                      {m.tasks?.map((t: any) => (
                        <div className="row__meta" key={t.id} style={{ marginTop: '0.2rem' }}>
                          · {t.name} ({t.status}{t.progress ? ` ${t.progress}%` : ''})
                        </div>
                      ))}
                    </div>
                  ))}
                  {state.report && (
                    <div className="confirm__detail" style={{ marginTop: '0.7rem' }}>
                      {state.report}
                    </div>
                  )}
                </>
              )}

              <div className="row__actions">
                {['planned', 'awaiting_approval'].includes(goal.status) && (
                  <button className="mini-action" onClick={async () => {
                    await api.post(`/planner/goals/${goal.id}/start`); await load();
                  }}>Start</button>
                )}
                {goal.status === 'running' && (
                  <button className="mini-action" onClick={async () => {
                    const r = await api.post<any>(`/planner/goals/${goal.id}/adapt`);
                    useStore.getState().notify(r.advice ?? r.message ?? 'Adapted.');
                  }}>Adapt</button>
                )}
                {goal.status === 'completed' && (
                  <button className="mini-action" onClick={async () => {
                    const r = await api.post<any>(`/planner/goals/${goal.id}/report`);
                    useStore.getState().notify(r.report?.slice(0, 300) ?? 'No report.');
                  }}>Report</button>
                )}
                <button className="mini-action" onClick={() => void inspect(goal.id)}>
                  {open === goal.id ? 'Collapse' : 'Milestones'}
                </button>
                <button className="mini-action mini-action--danger" onClick={async () => {
                  await api.post(`/planner/goals/${goal.id}/cancel`); await load();
                }}>Cancel</button>
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}

// ===========================================================================
export function AgentsPanel() {
  const [data, setData] = useState<any>(null);

  const load = useCallback(async () => {
    try { setData(await api.get('/agents')); } catch { /* transient */ }
  }, []);

  useEffect(() => {
    void load();
    return eventStream.subscribe((event) => {
      if (event.type.startsWith('agent.')) void load();
    });
  }, [load]);

  if (!data) return <p className="empty">Loading…</p>;
  const active = data.active ?? [];

  return (
    <>
      <div className="panel__section">
        <div className="panel__section-title">
          Running — {active.length}
          {active.length > 0 && (
            <button className="mini-action mini-action--danger" style={{ float: 'right' }}
                    onClick={async () => { await api.post('/agents/stop-all'); await load(); }}>
              Stop all
            </button>
          )}
        </div>
        {active.length === 0 && <p className="empty">No agents running.</p>}
        {active.map((run: any) => (
          <div className="row" key={run.id}>
            <div className="row__head">
              <span className="row__title">
                <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: 4,
                               background: run.colour, marginRight: 8 }} />
                {run.label}
              </span>
              <span className="status-chip status-chip--running">{run.status}</span>
            </div>
            <div className="row__sub">{truncate(run.objective, 90)}</div>
            <div className="progress">
              <div className="progress__bar" style={{ width: `${Math.max(2, run.progress * 100)}%`,
                                                      background: run.colour }} />
            </div>
            <div className="row__actions">
              <span className="row__meta">{run.current_step || 'starting'}</span>
              <button className="mini-action mini-action--danger" onClick={async () => {
                await api.post(`/agents/${run.id}/stop`); await load();
              }}>Stop this one</button>
            </div>
          </div>
        ))}
      </div>

      <div className="panel__section">
        <div className="panel__section-title">Specialists and their boundaries</div>
        {data.catalogue?.map((agent: any) => (
          <div className="row" key={agent.key}>
            <div className="row__head">
              <span className="row__title">
                <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: 4,
                               background: agent.colour, marginRight: 8 }} />
                {agent.label}
              </span>
              <span className="row__meta">{agent.tool_count} tools · {agent.time_limit}</span>
            </div>
            <div className="row__sub">{agent.description}</div>
            <div className="row__meta" style={{ marginTop: '0.3rem' }}>
              cannot: {agent.denied_permissions.slice(0, 5).join(', ')}
              {agent.denied_permissions.length > 5 && ` +${agent.denied_permissions.length - 5}`}
            </div>
          </div>
        ))}
      </div>

      {data.recent?.length > 0 && (
        <div className="panel__section">
          <div className="panel__section-title">Recent</div>
          {data.recent.slice(0, 12).map((run: any) => (
            <div className="row" key={run.id}>
              <div className="row__head">
                <span className="row__title" style={{ fontSize: '0.85rem' }}>{run.label}</span>
                <span className={`status-chip status-chip--${chipFor(run.status)}`}>
                  {run.status.replace('_', ' ')}
                </span>
              </div>
              <div className="row__sub">{truncate(run.objective, 80)}</div>
              {run.error && <div className="row__sub" style={{ color: 'var(--danger)' }}>
                {run.error}</div>}
            </div>
          ))}
        </div>
      )}
    </>
  );
}

// ===========================================================================
export function ResearchPanel() {
  const [projects, setProjects] = useState<any[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [detail, setDetail] = useState<Record<string, any>>({});
  const [question, setQuestion] = useState('');
  const [depth, setDepth] = useState('standard');

  const load = useCallback(async () => {
    try {
      const result = await api.get<{ projects: any[] }>('/research');
      setProjects(result.projects);
    } catch { /* transient */ }
  }, []);

  useEffect(() => {
    void load();
    return eventStream.subscribe((event) => {
      if (event.type.startsWith('research.')) void load();
    });
  }, [load]);

  const inspect = async (id: string) => {
    if (open === id) { setOpen(null); return; }
    setOpen(id);
    try {
      const full = await api.get(`/research/${id}`);
      setDetail((current) => ({ ...current, [id]: full }));
    } catch { /* transient */ }
  };

  return (
    <>
      <div className="panel__section">
        <div className="panel__section-title">New research</div>
        <input className="text-input" style={{ maxWidth: '100%', width: '100%',
                                               marginBottom: '0.5rem' }}
               placeholder="What do you want to know?"
               value={question} onChange={(e) => setQuestion(e.target.value)} />
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <select className="select" value={depth} onChange={(e) => setDepth(e.target.value)}>
            <option value="quick">Quick — 4 sources</option>
            <option value="standard">Standard — 9 sources</option>
            <option value="deep">Deep — 18 sources</option>
          </select>
          <button className="btn btn--primary" onClick={async () => {
            if (!question.trim()) return;
            await api.post('/research', { question, depth, background: true });
            setQuestion('');
            useStore.getState().notify('Research started in the background.');
            await load();
          }}>Research</button>
        </div>
      </div>

      <div className="panel__section">
        <div className="panel__section-title">Projects</div>
        {projects.length === 0 && <p className="empty">No research projects yet.</p>}
        {projects.map((project) => {
          const full = detail[project.id];
          return (
            <div className="row" key={project.id}>
              <div className="row__head">
                <button className="row__title" style={{ textAlign: 'left' }}
                        onClick={() => void inspect(project.id)}>
                  {truncate(project.question, 70)}
                </button>
                <span className={`status-chip status-chip--${
                  project.status === 'complete' ? 'completed' : 'running'}`}>
                  {project.status}
                </span>
              </div>
              <div className="row__sub">
                {project.source_count ?? 0} sources · {project.depth} · {relative(project.updated_at)}
              </div>

              {open === project.id && full && (
                <>
                  {full.summary && (
                    <div style={{ margin: '0.7rem 0', fontSize: '0.86rem', lineHeight: 1.6 }}>
                      {full.summary}
                    </div>
                  )}
                  {full.conflicts?.length > 0 && (
                    <div className="row__sub" style={{ color: 'var(--warn)' }}>
                      {full.conflicts.length} source disagreement(s):
                      {full.conflicts.slice(0, 2).map((c: any, i: number) => (
                        <div key={i} style={{ marginTop: '0.25rem' }}>· {truncate(c.claim, 110)}</div>
                      ))}
                    </div>
                  )}
                  {full.citations?.length > 0 && (
                    <div className="confirm__detail" style={{ marginTop: '0.6rem' }}>
                      {full.citations.map((c: any) =>
                        `${c.text}${c.outdated ? '  [possibly outdated]' : ''}`).join('\n')}
                    </div>
                  )}
                  {full.report && (
                    <details style={{ marginTop: '0.6rem' }}>
                      <summary className="mini-action">Full report</summary>
                      <div style={{ fontSize: '0.82rem', lineHeight: 1.65, marginTop: '0.5rem',
                                    whiteSpace: 'pre-wrap' }}>{full.report}</div>
                    </details>
                  )}
                </>
              )}

              <div className="row__actions">
                <button className="mini-action" onClick={() => void inspect(project.id)}>
                  {open === project.id ? 'Collapse' : 'Open'}
                </button>
                <button className="mini-action" onClick={async () => {
                  useStore.getState().notify('Continuing research…');
                  await api.post(`/research/${project.id}/continue`);
                  await load();
                }}>Continue</button>
                <button className="mini-action mini-action--danger" onClick={async () => {
                  await api.del(`/research/${project.id}`); await load();
                }}>Delete</button>
              </div>
            </div>
          );
        })}
      </div>
    </>
  );
}

// ===========================================================================
export function SelfEditPanel() {
  const [status, setStatus] = useState<any>(null);
  const [path, setPath] = useState('');
  const [request, setRequest] = useState('');
  const [proposal, setProposal] = useState<any>(null);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try { setStatus(await api.get('/selfedit')); } catch { /* transient */ }
  }, []);

  useEffect(() => { void load(); }, [load]);

  if (!status) return <p className="empty">Loading…</p>;

  if (!status.permission_granted) {
    return (
      <div className="panel__section">
        <p className="empty" style={{ textAlign: 'left', lineHeight: 1.7 }}>
          The <strong>Edit its own code</strong> permission is off — JARVIS cannot modify its
          own source.
          <br /><br />
          Turn it on in Settings → Permissions if you want this. Every change is written by a
          separate code model, diffed, syntax-checked, confirmed and backed up before it lands.
        </p>
      </div>
    );
  }

  return (
    <>
      <div className="panel__section">
        <div className="panel__section-title">Code model</div>
        <div className="control">
          <div className="control__label">{status.model.provider} / {status.model.model}</div>
          <span className="row__meta">separate from conversation</span>
        </div>
        <div className="control">
          <div className="control__label">Protected files</div>
          <span className="row__meta">{status.protected_files.length}</span>
        </div>
        <div className="control">
          <div className="control__label">Backups</div>
          <span className="row__meta">{status.backups}</span>
        </div>
      </div>

      <div className="panel__section">
        <div className="panel__section-title">Propose a change</div>
        <input className="text-input" style={{ maxWidth: '100%', width: '100%',
                                               marginBottom: '0.5rem' }}
               placeholder="backend/ai/orchestrator.py"
               value={path} onChange={(e) => setPath(e.target.value)} />
        <textarea className="text-input" rows={3}
                  style={{ maxWidth: '100%', width: '100%', marginBottom: '0.6rem' }}
                  placeholder="What should change? Be specific about behaviour."
                  value={request} onChange={(e) => setRequest(e.target.value)} />
        <button className="btn btn--primary" disabled={busy || !path || !request}
                onClick={async () => {
                  setBusy(true); setProposal(null);
                  try {
                    setProposal(await api.post('/selfedit/propose', { path, request }));
                  } catch (error) {
                    useStore.getState().notify((error as Error).message);
                  } finally { setBusy(false); }
                }}>
          {busy ? 'Writing…' : 'Propose'}
        </button>
      </div>

      {proposal?.ok && (
        <div className="panel__section">
          <div className="panel__section-title">
            Proposed — +{proposal.lines_added}/−{proposal.lines_removed}
          </div>
          <div className="row__sub">
            {proposal.path} · syntax{' '}
            <span style={{ color: proposal.validation.valid ? 'var(--ok)' : 'var(--danger)' }}>
              {proposal.validation.valid ? 'valid' : `invalid — ${proposal.validation.error}`}
            </span>
            {proposal.protected && (
              <span style={{ color: 'var(--danger)' }}> · PROTECTED FILE</span>
            )}
          </div>
          <pre className="confirm__detail" style={{ marginTop: '0.6rem', maxHeight: '340px' }}>
            {proposal.diff}
          </pre>
          <div className="row__actions">
            <button className="mini-action" disabled={!proposal.validation.valid}
                    onClick={async () => {
                      try {
                        const r = await api.post<any>('/selfedit/apply', {
                          proposal_id: proposal.proposal_id,
                          allow_protected: proposal.protected,
                        });
                        useStore.getState().notify(r.message);
                        setProposal(null); await load();
                      } catch (error) {
                        useStore.getState().notify((error as Error).message);
                      }
                    }}>
              Apply
            </button>
            <button className="mini-action mini-action--danger"
                    onClick={() => setProposal(null)}>Discard</button>
          </div>
        </div>
      )}

      <div className="panel__section">
        <div className="panel__section-title">Backups</div>
        <BackupList onRestore={load} />
      </div>
    </>
  );
}

function BackupList({ onRestore }: { onRestore: () => void }) {
  const [backups, setBackups] = useState<any[]>([]);

  useEffect(() => {
    void (async () => {
      try {
        const r = await api.get<{ backups: any[] }>('/selfedit/backups');
        setBackups(r.backups);
      } catch { /* transient */ }
    })();
  }, []);

  if (!backups.length) return <p className="empty">No source backups yet.</p>;
  return (
    <>
      {backups.slice(0, 15).map((backup) => (
        <div className="row" key={backup.backup}>
          <div className="row__head">
            <span className="row__title" style={{ fontSize: '0.83rem' }}>{backup.path}</span>
            <span className="row__meta">{relative(backup.taken_at)}</span>
          </div>
          <div className="row__actions">
            <button className="mini-action" onClick={async () => {
              const r = await api.post<any>(
                `/selfedit/rollback?backup=${encodeURIComponent(backup.backup)}`);
              useStore.getState().notify(
                `Restored ${r.path}.${r.restart_required ? ' Restart the backend.' : ''}`);
              onRestore();
            }}>Restore</button>
          </div>
        </div>
      ))}
    </>
  );
}

// ===========================================================================
// shared helpers
// ===========================================================================
export function truncate(text: string | undefined, limit: number): string {
  if (!text) return '';
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}

export function relative(iso?: string | null): string {
  if (!iso) return '';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const seconds = Math.round((Date.now() - then) / 1000);
  if (seconds < 0) return `in ${duration(-seconds)}`;
  if (seconds < 45) return 'just now';
  return `${duration(seconds)} ago`;
}

export function duration(seconds: number): string {
  if (seconds < 60) return `${Math.round(seconds)}s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h`;
  return `${Math.round(seconds / 86400)}d`;
}

export function formatWhen(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleDateString([], { weekday: 'short', day: 'numeric', month: 'short' })
    + ' ' + date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}

export function chipFor(status: string): string {
  return {
    running: 'running', completed: 'completed', complete: 'completed',
    failed: 'failed', timed_out: 'failed', blocked: 'failed',
    cancelled: 'cancelled', paused: 'paused', needs_input: 'paused',
    awaiting_approval: 'paused', planned: 'queued', planning: 'queued',
  }[status] ?? 'queued';
}
