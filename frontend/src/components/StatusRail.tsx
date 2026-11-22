import { useStore } from '../state/store';

/**
 * Ambient status.
 *
 * Deliberately typographic and edge-anchored: no boxes, no tiles. Everything
 * here is live from the WebSocket stream.
 *
 * The right edge is not here. System readings moved into the sidebar, which
 * owns that edge as a single unit -- see `Sidebar.tsx`. What is left is the
 * identity and state at top left, the wake-word light, and whatever work is
 * in progress at the bottom.
 */

const STATE_LABEL: Record<string, string> = {
  idle: 'STANDING BY',
  listening: 'LISTENING',
  thinking: 'THINKING',
  speaking: 'SPEAKING',
  working: 'WORKING',
  error: 'ERROR',
  offline: 'OFFLINE',
  sleep: 'ASLEEP',
};

export function StatusRail() {
  const state = useStore((s) => s.state);
  const mode = useStore((s) => s.mode);
  const connected = useStore((s) => s.connected);
  const overall = useStore((s) => s.overall);
  const tasks = useStore((s) => s.tasks);
  const activity = useStore((s) => s.activity);
  const focus = useStore((s) => s.focus);
  const wake = useStore((s) => s.wakeStatus);
  const wakeWord = useStore((s) => s.settings?.wake_word);

  const active = tasks.filter((t) => ['running', 'queued', 'paused', 'waiting'].includes(t.status));
  const running = active.find((t) => t.status === 'running');
  const latest = activity[0];

  return (
    <>
      {/* ---------------- identity ---------------- */}
      <div className="rail rail--top-left">
        <div className="identity">JARVIS</div>
        <div className="rail__line">
          <span className={`pulse-dot ${dotClass(state, connected)}`} />
          <span className="identity__state">{STATE_LABEL[state] ?? state.toUpperCase()}</span>
        </div>
        {mode !== 'normal' && (
          <div className="rail__line">
            <span className="rail__label">MODE</span>
            <span className="rail__value rail__value--accent">{mode.toUpperCase()}</span>
          </div>
        )}
        {focus?.active_app && (
          <div className="rail__line" title={focus.window_title}>
            <span className="rail__label">SEEING</span>
            <span className="rail__value">{truncate(focus.active_app, 26)}</span>
          </div>
        )}
        {!connected && (
          <div className="rail__line">
            <span className="rail__value rail__value--danger">LINK LOST — RECONNECTING</span>
          </div>
        )}
      </div>

      {/* ---------------- work in progress ---------------- */}
      {/* Wake word: visible on the main screen, not buried in settings.
          An always-listening microphone should be something you can see. */}
      {wake.phase !== 'off' && (
        <div className="rail rail--wake" title={wake.detail || wake.heard}>
          <span className={`pulse-dot ${wake.phase === 'failed' ? 'pulse-dot--danger'
            : wake.phase === 'triggered' ? 'pulse-dot--live' : 'pulse-dot--live'}`} />
          <span className="rail__value">
            {wake.phase === 'failed' ? 'WAKE WORD OFF'
              : wake.phase === 'triggered' ? 'HEARD YOU'
              : wake.phase === 'starting' ? 'WAKE WORD STARTING'
              : `LISTENING FOR "${(wakeWord || 'jarvis').toUpperCase()}"`}
          </span>
        </div>
      )}

      <div className="rail rail--bottom-left">
        {running ? (
          <>
            <div className="rail__line">
              <span className="pulse-dot pulse-dot--live" />
              <span className="rail__value rail__value--accent">
                {truncate(running.name, 42)}
              </span>
            </div>
            <div className="rail__line">
              <span className="rail__label">
                {Math.round((running.progress ?? 0) * 100)}% · {truncate(running.current_step, 46) || 'working'}
              </span>
            </div>
          </>
        ) : active.length > 0 ? (
          <div className="rail__line">
            <span className="pulse-dot pulse-dot--idle" />
            <span className="rail__label">{active.length} TASK{active.length === 1 ? '' : 'S'} QUEUED</span>
          </div>
        ) : latest ? (
          <div className="rail__line">
            <span className="rail__label">{truncate(latest.message, 62)}</span>
          </div>
        ) : null}
      </div>
    </>
  );
}

// ---------------------------------------------------------------------------

function dotClass(state: string, connected: boolean): string {
  if (!connected) return 'pulse-dot--danger';
  if (state === 'error') return 'pulse-dot--danger';
  if (state === 'offline') return 'pulse-dot--idle';
  if (state === 'idle') return 'pulse-dot--live';
  return 'pulse-dot--live';
}

function truncate(text: string | undefined, limit: number): string {
  if (!text) return '';
  return text.length > limit ? `${text.slice(0, limit - 1)}…` : text;
}
