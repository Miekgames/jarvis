import { useCallback, useEffect, useRef, useState } from 'react';

import { api, ApiError } from '../../lib/api';
import { useStore } from '../../state/store';
import { copyText } from '../Conversation';

/**
 * Work while you are out.
 *
 * Two clocks, and the gap between them is the feature. **Away** is when you
 * will be back; **work** is how long JARVIS should be busy, and can be
 * shorter — the last slice of it is spent finishing what is started and
 * writing up, so you come home to a result rather than to surgery.
 *
 * When the request is to build something, this screen is organised around
 * the thing that got built: a picture of it, a button that opens it, and a
 * list of the changes that are actually in it. That list comes from the
 * versions that passed a browser check, not from what the model said about
 * itself — the earlier screen showed nineteen "finished" features beside a
 * file that had not changed, and the difference is the whole point.
 *
 * And the **held** list, because nobody is watching while it runs: an
 * unattended run that wanted to do something outside its workspace did not
 * do it — it wrote down what it wanted and why it stopped.
 */

interface Job {
  title: string;
  state: 'pending' | 'doing' | 'done' | 'abandoned';
  source: string;
  note: string;
}

interface Held {
  at: string;
  what: string;
  reason: string;
  verdict: string;
}

interface Step {
  at: string;
  title: string;
  kept: boolean;
  detail: string;
  source?: string;
  added?: string[];
  fixed?: string[];
}

/** What happened when the finished app was used the way a person would. */
interface Trial {
  ran: boolean;
  saved?: boolean | null;
  problems?: string[];
  detail?: string;
}

interface AppInfo {
  file?: string;
  kind?: string;
  lines?: number;
  bytes?: number;
  max_bytes?: number;
  version?: number;
  checked?: 'browser' | 'syntax' | 'untested' | 'none';
  test_detail?: string;
  full?: boolean;
  trial?: Trial;
}

/** One line of a run's journal. */
interface Entry {
  at: string;
  kind: string;
  message: string;
}

interface Run {
  phase: string;
  running: boolean;
  prompt: string;
  workspace: string;
  stamp: string;
  mode: 'task' | 'build';
  artifact: string;
  preview: string;
  app: AppInfo;
  steps: Step[];
  journal?: Entry[];
  away_minutes: number;
  work_minutes: number;
  wrap_minutes: number;
  elapsed_minutes: number;
  remaining_minutes: number;
  should_wrap_up: boolean;
  jobs: Job[];
  jobs_done: number;
  jobs_total: number;
  held: Held[];
  held_count: number;
  report: string;
}

interface Produced {
  workspace: string;
  stamp: string;
  files: { path: string; name: string; bytes: number; openable: boolean; main: boolean }[];
  detail: string;
  mode?: 'task' | 'build';
  prompt?: string;
  artifact?: string;
  preview?: string;
  app?: AppInfo;
  steps?: Step[];
  report?: string;
  running?: boolean;
}

interface Past {
  stamp: string;
  prompt: string;
  mode: string;
  phase: string;
  artifact: string;
  files: number;
  kept: number;
  dropped: number;
  jobs_done: number;
  jobs_total: number;
  held_count: number;
  elapsed_minutes: number;
}

interface State {
  available: boolean;
  busy: boolean;
  max_work_minutes: number;
  current: Run | null;
  root: string;
}

// Offered in hours, because nobody thinks about being out in minutes.
const SPANS = [
  { label: '30m', minutes: 30 },
  { label: '1h', minutes: 60 },
  { label: '2h', minutes: 120 },
  { label: '4h', minutes: 240 },
  { label: '8h', minutes: 480 },
  { label: '12h', minutes: 720 },
  { label: '24h', minutes: 1440 },
];

const POLL_MS = 20000;

export function AwayPanel() {
  const [state, setState] = useState<State | null>(null);
  const [prompt, setPrompt] = useState('');
  const [away, setAway] = useState(240);
  const [work, setWork] = useState(180);
  const [shell, setShell] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const notify = useStore((s) => s.notify);
  const [made, setMade] = useState<Produced | null>(null);
  const [past, setPast] = useState<Past[]>([]);
  // Which run's output is on show. Empty means the current or latest one.
  const [stamp, setStamp] = useState('');

  const load = useCallback(async () => {
    try {
      setState(await api.get<State>('/autonomy'));
      // What it actually built, so the run ends in something you can open
      // rather than a path you have to go and find.
      const query = stamp ? `?stamp=${encodeURIComponent(stamp)}` : '';
      setMade(await api.get<Produced>(`/autonomy/produced${query}`));
      // Read from disk, not from memory, so yesterday's app is still here
      // after a restart.
      setPast((await api.get<{ runs: Past[] }>('/autonomy/runs')).runs);
    } catch (exc) {
      setError((exc as Error).message);
    }
  }, [stamp]);

  useEffect(() => {
    void load();
    // Events drive the updates; the interval is only a safety net for a
    // dropped socket, so it can be slow.
    const timer = window.setInterval(() => void load(), POLL_MS);
    const onProgress = () => void load();
    window.addEventListener('jarvis:autonomy', onProgress);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener('jarvis:autonomy', onProgress);
    };
  }, [load]);

  // Put an earlier prompt back in the box, ready to adjust and start.
  const promptRef = useRef<HTMLTextAreaElement>(null);
  const reuse = (text: string) => {
    setPrompt(text);
    const box = promptRef.current;
    if (box) {
      box.scrollIntoView({ block: 'center', behavior: 'smooth' });
      box.focus({ preventScroll: true });
    }
  };

  // Working for longer than you are away defeats the wrap-up, so the two
  // move together rather than letting you set something contradictory.
  const setAwaySpan = (minutes: number) => {
    setAway(minutes);
    if (work > minutes) setWork(minutes);
  };

  const start = async () => {
    setBusy(true);
    setError('');
    try {
      await api.post('/autonomy/start', {
        prompt, away_minutes: away, work_minutes: work, allow_shell: shell,
      });
      setPrompt('');
      setStamp('');
      await load();
    } catch (exc) {
      setError(exc instanceof ApiError ? exc.message : String(exc));
    } finally {
      setBusy(false);
    }
  };

  const imBack = async () => {
    setBusy(true);
    setError('');
    try {
      const reply = await api.post<{ acknowledged: boolean; detail: string }>(
        '/autonomy/back', {});
      notify(reply.detail);
      await load();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const stop = async (hard: boolean) => {
    setBusy(true);
    try {
      await api.post(hard ? '/autonomy/cancel' : '/autonomy/stop', {});
      await load();
    } catch (exc) {
      setError((exc as Error).message);
    } finally {
      setBusy(false);
    }
  };

  if (!state) return <p className="control__help">{error || 'Loading…'}</p>;

  if (!state.available) {
    return <p className="control__help">The AI core isn’t running, so there’s
      nothing to leave working.</p>;
  }

  const run = state.current;
  const building = run?.mode === 'build';
  // Whether the output card is showing the run in the card above it. If it
  // is, the steps and the report are already on screen and are not repeated.
  const sameRun = Boolean(run && made && made.stamp === run.stamp);
  const reserve = Math.round(Math.min(Math.max(work * 0.12, 2), 45, work / 2));

  return (
    <>
      {/* ------------------------------------------------- set one going */}
      {!state.busy && (
        <div className="control control--stack">
          <div className="control__label">Leave it working</div>

          <textarea
            ref={promptRef}
            className="away__prompt"
            rows={4}
            value={prompt}
            placeholder="What should it work on while you're out? e.g. build me a habit tracker app"
            onChange={(event) => setPrompt(event.target.value)}
            aria-label="What to work on"
          />

          <div className="control__label control__label--small">I'll be gone</div>
          <div className="away__spans">
            {SPANS.map((span) => (
              <button key={span.minutes}
                      className={`away__span ${away === span.minutes ? 'away__span--on' : ''}`}
                      onClick={() => setAwaySpan(span.minutes)}>
                {span.label}
              </button>
            ))}
          </div>

          <div className="control__label control__label--small">Work for</div>
          <div className="away__spans">
            {SPANS.filter((span) => span.minutes <= away).map((span) => (
              <button key={span.minutes}
                      className={`away__span ${work === span.minutes ? 'away__span--on' : ''}`}
                      onClick={() => setWork(span.minutes)}>
                {span.label}
              </button>
            ))}
          </div>

          {/* The two spans as times on a clock, which is how "I'll be back
              around six" is actually thought about. */}
          <div className="away__times">
            <span>Starts working now</span>
            <span>Done by <strong>{clockIn(work)}</strong></span>
            <span>You're back around <strong>{clockIn(away)}</strong></span>
          </div>

          <div className="control__help">
            It will stop starting new work with about {reserve} minutes left
            and spend that finishing up and writing a report. Ask it to build
            an app and every change is tested in a real browser before it is
            kept — so what you come back to opens and runs.
          </div>

          <label className="away__shell">
            <input type="checkbox" checked={shell}
                   onChange={(event) => setShell(event.target.checked)} />
            <span>
              Allow shell commands. Off by default — it's the one capability
              that can do anything at all, and nobody will be here to stop it.
            </span>
          </label>

          {error && <div className="control__help control__help--bad">{error}</div>}

          <button className="btn btn--primary" disabled={busy || !prompt.trim()}
                  onClick={() => void start()}>
            {busy ? 'Starting…' : 'Start working'}
          </button>

          <div className="control__help">
            It gets its own folder under {state.root} and can do what it likes
            in there. Anything it wants to change outside it is held for you.
          </div>
        </div>
      )}

      {/* ----------------------------------------------------- in flight */}
      {run && (
        <div className="control control--stack">
          <div className="control__label">
            <span className={`wake-dot wake-dot--${run.running ? 'ok' : 'idle'}`} />
            {phaseLabel(run.phase, building)}
          </div>
          <div className="control__help">{run.prompt}</div>

          <div className="progress">
            <div className="progress__fill"
                 style={{ width: `${Math.min(100, (run.elapsed_minutes / Math.max(run.work_minutes, 1)) * 100)}%` }} />
          </div>
          <div className="control__help">
            {Math.round(run.elapsed_minutes)} of {Math.round(run.work_minutes)} min
            {building
              ? ` · ${run.steps.filter((step) => step.kept).length} change(s) in the app`
              : ` · ${run.jobs_done}/${run.jobs_total} jobs`}
            {run.should_wrap_up && run.running && ' · wrapping up'}
            {run.running && run.remaining_minutes > 0
              && ` · done by ${clockIn(run.remaining_minutes)}`}
          </div>

          {run.running && <RightNow journal={run.journal ?? []} />}

          {run.held_count > 0 && (
            <div className="control__stack">
              <div className="control__label control__label--small">
                Held for you ({run.held_count})
              </div>
              {run.held.map((item, index) => (
                <div className="draft" key={`${item.at}-${index}`}>
                  <div className="draft__title">{item.what}</div>
                  <div className="draft__note">{item.reason}</div>
                </div>
              ))}
            </div>
          )}

          {building ? (
            <Steps steps={run.steps}
                   doing={run.jobs.find((job) => job.state === 'doing')?.title}
                   waiting={run.jobs.filter((job) => job.state === 'pending')
                     .map((job) => job.title)} />
          ) : run.jobs.length > 0 && (
            <div className="away__jobs">
              {run.jobs.map((job, index) => (
                <div className={`away__job away__job--${job.state}`} key={index}>
                  <span className="away__tick">{tick(job.state)}</span>
                  <span>{job.title}</span>
                </div>
              ))}
            </div>
          )}

          {run.report && (
            <textarea className="pack__copy" readOnly rows={10}
                      value={run.report} aria-label="Report" />
          )}

          {run.running ? (
            <div className="control__buttons">
              <button className="btn btn--small" disabled={busy}
                      onClick={() => void stop(false)}>
                Wrap up now
              </button>
              <button className="btn btn--small btn--danger" disabled={busy}
                      onClick={() => void stop(true)}>
                Stop immediately
              </button>
            </div>
          ) : (
            <button className="btn btn--primary" disabled={busy}
                    onClick={() => void imBack()}>
              {busy ? 'One moment…' : 'I’m back'}
            </button>
          )}
        </div>
      )}

      {/* -------------------------------------------------- what it made */}
      {made && made.files.length > 0 && (
        <div className="control control--stack">
          <div className="control__label">
            {made.artifact ? 'The app' : 'What it made'}
          </div>
          {!sameRun && made.prompt && (
            <div className="control__help">{made.prompt}</div>
          )}

          {made.artifact && (
            <AppCard made={made} live={sameRun && Boolean(run?.running)} />
          )}

          {!sameRun && made.steps && made.steps.length > 0 && (
            <Steps steps={made.steps} />
          )}
          {!sameRun && made.report && (
            <textarea className="pack__copy" readOnly rows={10}
                      value={made.report} aria-label="Report" />
          )}

          <div className="made">
            {made.files.filter((file) => !file.main).map((file) => (
              <div className="made__row" key={file.path}>
                <span className="made__name" title={file.path}>{file.name}</span>
                <span className="made__size">{size(file.bytes)}</span>
                {(file.openable || isImage(file.name)) && (
                  <a className="made__open" href={fileUrl(file.path, made.stamp)}
                     target="_blank" rel="noreferrer">open</a>
                )}
                <a className="made__open"
                   href={fileUrl(file.path, made.stamp, true)}>save</a>
              </div>
            ))}
          </div>

          <div className="control__buttons">
            <a className="btn btn--small"
               href={`/api/autonomy/download?stamp=${encodeURIComponent(made.stamp)}`}
               download>
              Download everything
            </a>
            {stamp && (
              <button className="btn btn--small" onClick={() => setStamp('')}>
                Back to the latest run
              </button>
            )}
          </div>
          <div className="control__help">{made.workspace}</div>
        </div>
      )}

      {/* ------------------------------------------------------- earlier */}
      {past.filter((item) => item.stamp !== made?.stamp).length > 0 && (
        <div className="control control--stack">
          <div className="control__label">Earlier runs</div>
          {past.filter((item) => item.stamp !== made?.stamp).map((item) => (
            <div className="draft" key={item.stamp}>
              <div className="draft__where">
                {when(item.stamp)}
                {item.mode === 'build'
                  ? ` · ${item.kept} change(s) kept`
                  : item.jobs_total > 0
                    ? ` · ${item.jobs_done}/${item.jobs_total} done`
                    : ` · ${item.files} file(s)`}
                {item.held_count > 0 && ` · ${item.held_count} held`}
              </div>
              <div className="draft__title">
                {item.prompt || item.artifact || item.stamp}
              </div>
              <div className="control__buttons">
                {item.artifact && isPage(item.artifact) && (
                  <a className="btn btn--small" target="_blank" rel="noreferrer"
                     href={fileUrl(item.artifact, item.stamp)}>
                    Open the app
                  </a>
                )}
                <button className="btn btn--small"
                        onClick={() => setStamp(item.stamp)}>
                  Show
                </button>
                {/* Puts it back in the box -- it does not start anything.
                    Only offered when the box is there to put it in. */}
                {item.prompt && !state.busy && (
                  <button className="btn btn--small" onClick={() => reuse(item.prompt)}>
                    Use this prompt
                  </button>
                )}
              </div>
            </div>
          ))}
        </div>
      )}
    </>
  );
}

// ---------------------------------------------------------------------------
/** The thing that was built: a picture of it, and a way to open it. */
function AppCard({ made, live }: { made: Produced; live: boolean }) {
  const app = made.app ?? {};
  const file = made.artifact ?? '';
  const page = isPage(file);

  return (
    <div className="app">
      {made.preview && (
        <a href={page ? fileUrl(file, made.stamp) : undefined}
           target="_blank" rel="noreferrer" className="app__shot">
          {/* The version in the address is only there to defeat the cache:
              the picture is retaken every time a change is kept. */}
          <img src={`${fileUrl(made.preview, made.stamp)}&v=${app.version ?? 0}`}
               alt={`What ${file} looks like when it opens`} />
        </a>
      )}

      <div className="app__name">{file}</div>
      <div className="app__stats">
        {app.lines ? `${app.lines} lines · ` : ''}
        {app.bytes ? `${size(app.bytes)} · ` : ''}
        {app.version ? `version ${app.version}` : ''}
        {live && ' · still being worked on'}
      </div>
      <div className={`app__check app__check--${app.checked ?? 'none'}`}>
        {checkLabel(app)}
      </div>

      {/* Running and working are different claims. This one is about
          working, and when the answer is "not entirely" it says what,
          right above the button that opens the thing. */}
      {app.trial?.ran && (app.trial.problems?.length ?? 0) > 0 && (
        <div className="app__wrong">
          <div>Still wrong when it was last used:</div>
          {app.trial.problems!.map((problem) => (
            <div className="app__wrong-item" key={problem}>{problem}</div>
          ))}
        </div>
      )}
      {app.trial?.ran && (app.trial.problems?.length ?? 0) === 0 && (
        <div className="app__check app__check--browser">
          {app.trial.saved
            ? 'Used like a person would — typed into, buttons pressed, page reloaded: what was added was still there, and nothing went wrong.'
            : 'Used like a person would — buttons pressed with its fields filled in: nothing went wrong.'}
        </div>
      )}

      <div className="control__buttons">
        {page && (
          <a className="btn btn--primary" target="_blank" rel="noreferrer"
             href={fileUrl(file, made.stamp)}>
            Open the app
          </a>
        )}
        <a className={`btn ${page ? 'btn--small' : 'btn--primary'}`}
           href={fileUrl(file, made.stamp, true)}>
          Save {file}
        </a>
      </div>

      <SourceCode file={file} stamp={made.stamp} version={app.version ?? 0} />
    </div>
  );
}

/**
 * The code itself, on the screen, ready to copy.
 *
 * A download is a file somewhere in a phone's Files app; this is the text,
 * here, with a button that puts it on the clipboard. It is the file exactly
 * as built -- fetched the same way "Save" gets it, so nothing JARVIS adds
 * when it opens the app in a browser is in it.
 *
 * Shown in a read-only text box rather than as formatted code: on a phone
 * a long-press in a text box selects all of it, which is the fallback when
 * the clipboard refuses.
 */
function SourceCode({ file, stamp, version }: {
  file: string; stamp: string; version: number;
}) {
  const [open, setOpen] = useState(false);
  const [code, setCode] = useState('');
  const [error, setError] = useState('');
  const [copied, setCopied] = useState('');
  const box = useRef<HTMLTextAreaElement>(null);
  const notify = useStore((s) => s.notify);

  useEffect(() => {
    if (!open) return;
    let cancelled = false;
    setError('');
    void (async () => {
      try {
        const response = await api.raw(
          `/autonomy/file?path=${encodeURIComponent(file)}`
          + `&stamp=${encodeURIComponent(stamp)}&download=true`);
        if (!response.ok) throw new Error(`${response.status} ${response.statusText}`);
        const text = await response.text();
        if (!cancelled) setCode(text);
      } catch (exc) {
        if (!cancelled) setError(`Could not load the code: ${(exc as Error).message}`);
      }
    })();
    return () => { cancelled = true; };
    // Re-read when a new version is kept, so a run still in progress shows
    // the code that is actually on disk now.
  }, [open, file, stamp, version]);

  const copy = async () => {
    if (await copyText(code)) {
      setCopied('Copied');
      window.setTimeout(() => setCopied(''), 2000);
      return;
    }
    // The clipboard refused. Select it all so it can be copied by hand.
    box.current?.focus();
    box.current?.setSelectionRange(0, code.length);
    notify('Couldn’t copy automatically — the code is selected, copy it from the menu.');
  };

  // Counted the way the line count above it is, so the two agree: a final
  // newline ends the last line rather than starting another.
  const lines = code ? code.replace(/\n$/, '').split('\n').length : 0;

  return (
    <div className="source">
      <button className="btn btn--small" onClick={() => setOpen(!open)}>
        {open ? 'Hide the code' : 'Show the code'}
      </button>
      {open && (
        <>
          {error && <div className="control__help control__help--bad">{error}</div>}
          {!error && !code && <div className="control__help">Loading…</div>}
          {code && (
            <>
              <div className="source__bar">
                <span>{file} · {lines} lines</span>
                <button className="source__copy" onClick={() => void copy()}>
                  {copied || 'Copy code'}
                </button>
              </div>
              <textarea ref={box} className="source__code" readOnly
                        spellCheck={false} wrap="off" value={code}
                        aria-label={`The code of ${file}`} />
            </>
          )}
        </>
      )}
    </div>
  );
}

/**
 * Every change that was tried, and what became of it.
 *
 * Kept means it is in the file: the version containing it passed the checks
 * and replaced the one before. Not kept means the previous version is still
 * what is on disk, and the line says why.
 */
function Steps({ steps, doing, waiting }: {
  steps: Step[]; doing?: string; waiting?: string[];
}) {
  if (steps.length === 0 && !doing) return null;
  return (
    <div className="steps">
      {steps.map((step, index) => (
        <div className={`step step--${step.kept ? 'kept' : 'dropped'}`}
             key={`${step.at}-${index}`}>
          <span className="away__tick">{step.kept ? '✓' : '✕'}</span>
          <div className="step__body">
            <div className="step__title">{step.title}</div>
            <div className="step__detail">
              {step.kept ? step.detail : `Not kept — ${step.detail}`}
              {step.kept && step.source === 'research'
                && ' · found by looking at comparable apps'}
            </div>
            {step.kept && step.added && step.added.length > 0 && (
              <div className="step__detail">
                New on the page: {step.added.map((name) => `“${name}”`).join(', ')}
              </div>
            )}
            {step.kept && step.fixed && step.fixed.length > 0 && (
              <div className="step__detail">
                Seen before this change and not after it: {step.fixed.join('; ')}
              </div>
            )}
          </div>
        </div>
      ))}
      {doing && (
        <div className="step step--doing">
          <span className="away__tick">▸</span>
          <div className="step__body">
            <div className="step__title">{doing}</div>
            <div className="step__detail">Writing it and testing it now…</div>
          </div>
        </div>
      )}
      {waiting && waiting.length > 0 && (
        <div className="step step--waiting">
          <span className="away__tick">·</span>
          <div className="step__body">
            <div className="step__detail">
              Next: {waiting.slice(0, 6).join(' · ')}
              {waiting.length > 6 && ` · and ${waiting.length - 6} more`}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

/**
 * What it is doing this minute, in its own words from the journal.
 *
 * The step list says which change is being made; this says what is
 * happening inside it -- "read 'Best habit tracker apps'", "used the app:
 * what was added was gone after reloading" -- which is the part worth
 * glancing at from a phone. The time beside it is how long ago, so a run
 * that has gone quiet for ten minutes looks like one.
 */
function RightNow({ journal }: { journal: Entry[] }) {
  const [, tick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => tick((n) => n + 1), 15000);
    return () => window.clearInterval(timer);
  }, []);

  // Phase changes and notes say what is going on; individual tool calls
  // and "starting:" lines repeat what the step list already shows.
  const recent = journal
    .filter((entry) => entry.kind === 'note' || entry.kind === 'phase'
      || (entry.kind === 'step' && !entry.message.startsWith('starting:')))
    .slice(-3)
    .reverse();
  if (!recent.length) return null;

  return (
    <div className="right-now">
      <div className="control__label control__label--small">Right now</div>
      {recent.map((entry, index) => (
        <div className={`right-now__line ${index === 0 ? 'right-now__line--latest' : ''}`}
             key={`${entry.at}-${index}`}>
          <span className="right-now__ago">{ago(entry.at)}</span>
          <span>{entry.message}</span>
        </div>
      ))}
    </div>
  );
}

function ago(iso: string): string {
  const seconds = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (seconds < 60) return 'just now';
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes} min ago`;
  return `${Math.floor(minutes / 60)} h ${minutes % 60} min ago`;
}

// ---------------------------------------------------------------------------
function fileUrl(path: string, stamp: string, download = false): string {
  const query = `path=${encodeURIComponent(path)}&stamp=${encodeURIComponent(stamp)}`;
  return `/api/autonomy/file?${query}${download ? '&download=true' : ''}`;
}

/** The time on the clock this many minutes from now, with the day if it
 *  is not today -- a 24-hour run ends tomorrow. */
function clockIn(minutes: number): string {
  const then = new Date(Date.now() + minutes * 60_000);
  const time = then.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' });
  if (then.toDateString() === new Date().toDateString()) return time;
  return `${then.toLocaleDateString([], { weekday: 'short' })} ${time}`;
}

function isPage(name: string): boolean {
  return /\.html?$/i.test(name);
}

function isImage(name: string): boolean {
  return /\.(png|jpe?g|gif|webp)$/i.test(name);
}

function checkLabel(app: AppInfo): string {
  switch (app.checked) {
    case 'browser':
      return 'Tested: opened in a real browser with the network off, every button pressed, no errors.';
    case 'syntax':
      return 'Syntax-checked only. It has not been run — nothing is executed while you are out.';
    case 'untested':
      return `Not browser-tested${app.test_detail ? ` (${app.test_detail})` : ''}. Open it and check.`;
    default:
      return 'Not tested — there is no automatic check for this kind of file.';
  }
}

function phaseLabel(phase: string, building: boolean): string {
  switch (phase) {
    case 'planning': return 'Working out what to do';
    case 'working': return building ? 'Building' : 'Working';
    case 'researching': return 'Looking up what similar apps offer';
    case 'wrapping_up': return 'Wrapping up';
    case 'done': return 'Finished';
    case 'stopped': return 'Stopped';
    case 'failed': return 'Failed';
    default: return phase;
  }
}

function tick(state: string): string {
  switch (state) {
    case 'done': return '✓';
    case 'doing': return '▸';
    case 'abandoned': return '✕';
    default: return '·';
  }
}

function size(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(bytes < 10240 ? 1 : 0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

/** A run's folder name is the UTC moment it started: 20261114-183000. */
function when(stamp: string): string {
  const match = /^(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})$/.exec(stamp);
  if (!match) return stamp;
  const [, year, month, day, hour, minute, second] = match;
  const date = new Date(Date.UTC(+year, +month - 1, +day, +hour, +minute, +second));
  return date.toLocaleString([], {
    weekday: 'short', day: 'numeric', month: 'short',
    hour: '2-digit', minute: '2-digit',
  });
}
