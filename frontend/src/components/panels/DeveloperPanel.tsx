import { useCallback, useEffect, useState } from 'react';

import { api, eventStream } from '../../lib/api';
import { useStore } from '../../state/store';
import { chipFor, relative, truncate } from './WorkPanels';

/**
 * The developer workspace: git state, diffs, tests, builds and the activity
 * timeline — all reading the real repository, never a simulation.
 */
export function DeveloperPanel() {
  const [summary, setSummary] = useState<any>(null);
  const [permissions, setPermissions] = useState<any>({});
  const [diff, setDiff] = useState<any>(null);
  const [tests, setTests] = useState<any>(null);
  const [message, setMessage] = useState('');
  const [busy, setBusy] = useState('');
  const [path, setPath] = useState('');

  const notify = useStore((s) => s.notify);

  const load = useCallback(async () => {
    try {
      const workspaces = await api.get<any>('/dev/workspaces');
      setPermissions(workspaces.permissions ?? {});
      if (!workspaces.permissions?.read) return;
      if (!workspaces.workspaces?.length) { setSummary({ ok: false, noWorkspace: true }); return; }
      setSummary(await api.get('/dev'));
    } catch (error) {
      setSummary({ ok: false, message: (error as Error).message });
    }
  }, []);

  useEffect(() => {
    void load();
    return eventStream.subscribe((event) => {
      if (event.type === 'dev.event' || event.type === 'code.indexed') void load();
    });
  }, [load]);

  // ---- gates -------------------------------------------------------------
  if (permissions.read === false) {
    return (
      <div className="panel__section">
        <p className="empty" style={{ textAlign: 'left', lineHeight: 1.7 }}>
          The <strong>Read your code</strong> permission is off. Enable it in
          Settings → Permissions to use the developer workspace.
        </p>
      </div>
    );
  }
  if (!summary) return <p className="empty">Loading…</p>;

  if (summary.noWorkspace || (!summary.ok && !summary.git)) {
    return (
      <div className="panel__section">
        <div className="panel__section-title">Add a workspace</div>
        <p style={{ fontSize: '0.78rem', color: 'var(--ink-ghost)', marginBottom: '0.9rem' }}>
          Point JARVIS at a project folder. It must be inside the folders JARVIS is already
          allowed to read.
        </p>
        <div style={{ display: 'flex', gap: '0.5rem' }}>
          <input className="text-input" style={{ flex: 1, maxWidth: 'none' }}
                 placeholder="C:\\Users\\Mike\\Downloads\\project-jarvis"
                 value={path} onChange={(e) => setPath(e.target.value)} />
          <button className="btn btn--primary" onClick={async () => {
            try {
              const w = await api.post<any>('/dev/workspaces', { path });
              await api.post(`/dev/workspaces/${w.id}/index`);
              notify(`Indexed ${w.name}.`);
              setPath(''); await load();
            } catch (error) { notify((error as Error).message); }
          }}>Add & index</button>
        </div>
        {summary.message && <p className="row__sub" style={{ marginTop: '0.8rem' }}>
          {summary.message}</p>}
      </div>
    );
  }

  const git = summary.git ?? {};
  const runners = summary.runners ?? {};

  return (
    <>
      {/* ---------------- repository ---------------- */}
      <div className="panel__section">
        <div className="panel__section-title">
          {summary.workspace?.name}
          <span className="row__meta" style={{ float: 'right' }}>
            {summary.workspace?.file_count} files · {summary.workspace?.symbol_count} symbols
          </span>
        </div>

        {git.repository === false ? (
          <p className="row__sub">{git.message}</p>
        ) : (
          <>
            <div className="control">
              <div className="control__label">
                branch <strong style={{ color: 'var(--accent)' }}>{git.branch}</strong>
              </div>
              <span className="row__meta">
                {git.ahead ? `↑${git.ahead} ` : ''}{git.behind ? `↓${git.behind} ` : ''}
                {git.clean ? 'clean' : `${git.changed_files?.length} changed`}
              </span>
            </div>
            {git.last_commit?.sha && (
              <div className="row__sub">
                last: {git.last_commit.sha} {truncate(git.last_commit.subject, 60)} —{' '}
                {git.last_commit.when}
              </div>
            )}
          </>
        )}

        <div className="row__actions" style={{ marginTop: '0.7rem' }}>
          <button className="mini-action" onClick={async () => {
            setBusy('index');
            await api.post(`/dev/workspaces/${summary.workspace.id}/index`);
            setBusy(''); notify('Re-indexed.'); await load();
          }}>{busy === 'index' ? 'Indexing…' : 'Re-index'}</button>
          <button className="mini-action" onClick={async () => {
            setBusy('diff');
            setDiff(await api.get(`/dev/git/diff`));
            setBusy('');
          }}>Show diff</button>
          <button className="mini-action" onClick={async () => {
            setBusy('review'); notify('Reviewing the diff…');
            const r = await api.post<any>('/dev/git/review');
            setDiff({ ...(diff ?? {}), review: r.review });
            setBusy('');
          }}>{busy === 'review' ? 'Reviewing…' : 'Review diff'}</button>
        </div>
      </div>

      {/* ---------------- changed files ---------------- */}
      {!git.clean && git.changed_files?.length > 0 && (
        <div className="panel__section">
          <div className="panel__section-title">Changed files</div>
          {git.changed_files.slice(0, 30).map((file: any) => (
            <div className="control" key={file.path}>
              <div className="control__label" style={{ fontSize: '0.8rem',
                                                       fontFamily: 'var(--mono)' }}>
                {file.path}
              </div>
              <span className={`status-chip status-chip--${
                file.untracked ? 'queued' : file.staged ? 'completed' : 'paused'}`}>
                {file.untracked ? 'new' : file.staged ? 'staged' : file.status}
              </span>
            </div>
          ))}
        </div>
      )}

      {/* ---------------- diff / review ---------------- */}
      {diff && (
        <div className="panel__section">
          <div className="panel__section-title">
            Diff {diff.lines_added !== undefined
              && `— +${diff.lines_added}/−${diff.lines_removed}`}
          </div>
          {diff.review && (
            <div style={{ fontSize: '0.84rem', lineHeight: 1.65, marginBottom: '0.7rem',
                          whiteSpace: 'pre-wrap' }}>{diff.review}</div>
          )}
          {diff.diff && (
            <pre className="confirm__detail" style={{ maxHeight: '320px' }}>{diff.diff}</pre>
          )}
          {diff.empty && <p className="empty">No changes.</p>}
        </div>
      )}

      {/* ---------------- commit ---------------- */}
      <div className="panel__section">
        <div className="panel__section-title">Commit</div>
        <button className="mini-action" style={{ marginBottom: '0.6rem' }} onClick={async () => {
          setBusy('msg');
          try {
            const r = await api.post<any>('/dev/git/commit-message?staged_only=false');
            if (r.ok) setMessage(r.message); else notify(r.message);
          } catch (error) { notify((error as Error).message); }
          setBusy('');
        }}>{busy === 'msg' ? 'Writing…' : 'Generate message from the diff'}</button>

        <textarea className="text-input" rows={4}
                  style={{ maxWidth: '100%', width: '100%', marginBottom: '0.6rem',
                           fontFamily: 'var(--mono)', fontSize: '0.78rem' }}
                  placeholder="Commit message"
                  value={message} onChange={(e) => setMessage(e.target.value)} />

        {permissions.git_write ? (
          <button className="btn btn--primary" disabled={!message.trim()} onClick={async () => {
            try {
              const r = await api.post<any>('/dev/git/commit',
                                            { message, paths: ['.'], workspace: '' });
              notify(`Committed ${r.sha}. ${r.note}`);
              setMessage(''); setDiff(null); await load();
            } catch (error) { notify((error as Error).message); }
          }}>Stage all & commit</button>
        ) : (
          <p className="row__meta">
            Committing needs the “Write to git” permission (Settings → Permissions).
          </p>
        )}
      </div>

      {/* ---------------- tests & build ---------------- */}
      <div className="panel__section">
        <div className="panel__section-title">
          Tests & build
          <span className="row__meta" style={{ float: 'right' }}>
            {runners.kinds?.join(', ') || 'none detected'}
          </span>
        </div>

        {!permissions.execute ? (
          <p className="row__sub">
            Running tests and builds needs the “Run builds and tests” permission — it executes
            the project’s own scripts.
          </p>
        ) : (
          <div className="row__actions" style={{ marginBottom: '0.6rem' }}>
            <button className="mini-action" disabled={!!busy} onClick={async () => {
              setBusy('tests'); notify('Running tests…');
              try { setTests(await api.post('/dev/tests')); }
              catch (error) { notify((error as Error).message); }
              setBusy('');
            }}>{busy === 'tests' ? 'Running…' : 'Run tests'}</button>
            <button className="mini-action" disabled={!!busy} onClick={async () => {
              setBusy('analyse'); notify('Running tests and analysing failures…');
              try { setTests(await api.post('/dev/tests?analyse=true')); }
              catch (error) { notify((error as Error).message); }
              setBusy('');
            }}>Run & explain failures</button>
            {runners.build && (
              <button className="mini-action" disabled={!!busy} onClick={async () => {
                setBusy('build');
                try { setTests(await api.post('/dev/build')); }
                catch (error) { notify((error as Error).message); }
                setBusy('');
              }}>Build</button>
            )}
          </div>
        )}

        {tests && (
          <>
            <div className="control">
              <div className="control__label">
                {tests.passed !== undefined
                  ? `${tests.passed} passed, ${tests.failed} failed`
                  : tests.ok ? 'Build succeeded' : `Build failed (${tests.error_count} errors)`}
              </div>
              <span className={`status-chip status-chip--${tests.ok ? 'completed' : 'failed'}`}>
                {tests.ok ? 'pass' : 'fail'}
              </span>
            </div>
            {tests.failures?.slice(0, 8).map((failure: any, i: number) => (
              <div className="row__sub" key={i} style={{ color: 'var(--danger)',
                                                         fontFamily: 'var(--mono)',
                                                         fontSize: '0.74rem' }}>
                ✕ {truncate(failure.name, 90)}
              </div>
            ))}
            {tests.errors?.slice(0, 8).map((error: any, i: number) => (
              <div className="row__sub" key={i} style={{ color: 'var(--danger)',
                                                         fontFamily: 'var(--mono)',
                                                         fontSize: '0.74rem' }}>
                {error.file}:{error.line} {truncate(error.message, 70)}
              </div>
            ))}
            {Array.isArray(tests.analysis) && tests.analysis.map((item: any, i: number) => (
              <details key={i} style={{ marginTop: '0.5rem' }}>
                <summary className="mini-action">{truncate(item.test, 70)}</summary>
                <div style={{ fontSize: '0.8rem', lineHeight: 1.6, marginTop: '0.4rem',
                              whiteSpace: 'pre-wrap' }}>{item.explanation}</div>
              </details>
            ))}
            {tests.output && !tests.ok && (
              <details style={{ marginTop: '0.6rem' }}>
                <summary className="mini-action">Raw output</summary>
                <pre className="confirm__detail" style={{ marginTop: '0.4rem',
                                                          maxHeight: '260px' }}>
                  {tests.output}
                </pre>
              </details>
            )}
          </>
        )}
      </div>

      {/* ---------------- timeline ---------------- */}
      <div className="panel__section">
        <div className="panel__section-title">Development timeline</div>
        {(summary.timeline ?? []).length === 0 && <p className="empty">Nothing recorded yet.</p>}
        {(summary.timeline ?? []).slice(0, 25).map((event: any, i: number) => (
          <div className="row" key={i}>
            <div className="row__head">
              <span className="row__title" style={{ fontSize: '0.83rem',
                                                    color: event.ok ? 'var(--ink)'
                                                                    : 'var(--danger)' }}>
                {truncate(event.summary, 74)}
              </span>
              <span className="row__meta">{event.kind}</span>
            </div>
            <div className="row__meta">{relative(event.ts)}</div>
          </div>
        ))}
      </div>
    </>
  );
}
