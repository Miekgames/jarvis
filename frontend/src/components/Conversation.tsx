import { useEffect, useLayoutEffect, useRef, useState } from 'react';

import { Markdown } from './Markdown';
import { speakable, voice } from '../lib/voice';
import type { Telemetry } from '../lib/api';
import { useStore, type Utterance } from '../state/store';

/**
 * The conversation, flowing below the core.
 *
 * Not a chat window: recent turns are prominent, older ones fade toward the
 * top and out of the mask. The full history lives in the History panel.
 *
 * Two things were missing that a conversation surface really needs. There
 * was no way to get a reply back out — a long answer could only be
 * retyped — and no way to see more than the last few turns without opening
 * a panel, which loses your place. Both are handled here rather than by
 * turning this into a chat window, because the fade is the design.
 */

/** How many turns are on the stage before you scroll back. */
const SHALLOW = 6;

/** Below this, a wait is not worth counting out loud. */
const QUIET_SECONDS = 3;

export function Conversation() {
  const utterances = useStore((s) => s.utterances);
  const send = useStore((s) => s.send);
  const notify = useStore((s) => s.notify);
  const settings = useStore((s) => s.settings);
  const containerRef = useRef<HTMLDivElement>(null);
  const historyMore = useStore((s) => s.historyMore);
  const loadEarlier = useStore((s) => s.loadEarlier);
  // How many turns are on the stage. Scrolling up to the top shows more,
  // then fetches earlier ones from before this screen opened.
  const [shown, setShown] = useState(SHALLOW);
  const deep = shown > SHALLOW;
  const [copied, setCopied] = useState<string | null>(null);
  // Which reply is being read aloud on request, if any.
  const [reading, setReading] = useState<string | null>(null);
  // Which tool call is open for inspection: "<utterance id>:<index>".
  const [inspecting, setInspecting] = useState<string | null>(null);
  const readingRef = useRef<string | null>(null);

  // Follow the conversation down as it grows -- unless you have scrolled up
  // to read something, in which case a streaming reply must not keep
  // yanking you back to the bottom. A new turn of your own always returns
  // you there.
  const pinnedRef = useRef(true);
  const lastNewestRef = useRef<string | undefined>(undefined);
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;
    const newest = utterances[utterances.length - 1];
    // By the newest message, not the count: earlier ones loading in above
    // must not throw you back to the bottom.
    const youSpoke = newest?.id !== lastNewestRef.current && newest?.role === 'user';
    lastNewestRef.current = newest?.id;
    if (pinnedRef.current || youSpoke) {
      container.scrollTop = container.scrollHeight;
      pinnedRef.current = true;
    }
  }, [utterances]);

  // Rendered state alongside the ref: the ref is read on every streamed
  // token, the state only changes when you cross the line.
  const [scrolledUp, setScrolledUp] = useState(false);
  const onScroll = () => {
    const container = containerRef.current;
    if (!container) return;
    pinnedRef.current =
      container.scrollHeight - container.scrollTop - container.clientHeight < 60;
    setScrolledUp(!pinnedRef.current);
    if (!pinnedRef.current && container.scrollTop < 80) void showEarlier();
  };

  // Earlier turns appear above what you're reading: keep it where it was.
  const anchorRef = useRef<number | null>(null);
  const loadingRef = useRef(false);
  const showEarlier = async () => {
    const container = containerRef.current;
    if (loadingRef.current || !container) return;
    const onStage = Math.min(shown, utterances.length);
    if (onStage >= utterances.length && !historyMore) return;
    loadingRef.current = true;
    anchorRef.current = container.scrollHeight - container.scrollTop;
    try {
      if (onStage < utterances.length) {
        setShown(onStage + 20);
      } else {
        const added = await loadEarlier();
        setShown(onStage + added);
        if (!added) anchorRef.current = null;
      }
    } catch {
      anchorRef.current = null;
    } finally {
      loadingRef.current = false;
    }
  };
  const firstShownId = utterances[Math.max(0, utterances.length - shown)]?.id;
  useLayoutEffect(() => {
    const container = containerRef.current;
    if (container && anchorRef.current !== null) {
      container.scrollTop = container.scrollHeight - anchorRef.current;
      anchorRef.current = null;
    }
  }, [firstShownId]);

  const toLatest = () => {
    const container = containerRef.current;
    if (!container) return;
    container.scrollTo({ top: container.scrollHeight, behavior: 'smooth' });
    pinnedRef.current = true;
    setScrolledUp(false);
  };

  useUnreadInTitle(utterances);

  if (!utterances.length) return null;

  const latestButton = scrolledUp && (
    <button className="conversation__latest" onClick={toLatest}
            title="Back to the newest message">
      ↓ latest
    </button>
  );

  // Only the last few turns stay on the stage until you scroll back.
  const visible = utterances.slice(-shown);
  const hidden = utterances.length - visible.length;

  /** The question a reply was answering: the nearest user turn before it. */
  const askedBefore = (replyId: string): string | null => {
    const at = utterances.findIndex((u) => u.id === replyId);
    for (let i = at - 1; i >= 0; i -= 1) {
      if (utterances[i].role === 'user' && utterances[i].text.trim()) return utterances[i].text;
    }
    return null;
  };

  const copy = async (utterance: { id: string; text: string }) => {
    if (await copyText(utterance.text)) {
      setCopied(utterance.id);
      window.setTimeout(() => setCopied(null), 1400);
      return;
    }
    // Say so rather than leaving a button that appears to have worked.
    notify('Could not copy — your browser refused clipboard access.');
  };

  /**
   * Read one reply out loud, because you asked.
   *
   * Typed questions get typed answers -- that rule stays. This is for the
   * reply you would rather hear, chosen per reply. Pressed again, it stops.
   */
  const readAloud = async (utterance: { id: string; text: string }) => {
    if (readingRef.current === utterance.id) {
      voice.stopSpeaking();
      readingRef.current = null;
      setReading(null);
      return;
    }
    const text = speakable(utterance.text);
    if (!text.trim()) {
      notify('There is nothing in that reply that can be read out.');
      return;
    }
    const spoken = settings.voice ?? {};
    readingRef.current = utterance.id;
    setReading(utterance.id);
    try {
      await voice.speak(text, {
        engine: spoken.tts_engine === 'browser' ? 'browser' : 'edge',
        voice: spoken.voice,
        rate: spoken.rate ?? 1,
      });
    } finally {
      // Only clear it if it is still this one: pressing another reply's
      // button mid-sentence has already moved the marker on.
      if (readingRef.current === utterance.id) {
        readingRef.current = null;
        setReading(null);
      }
    }
  };

  return (
    <>
    {latestButton}
    <div className="conversation" ref={containerRef} aria-live="polite" aria-atomic="false"
         onScroll={onScroll}>
      {(hidden > 0 || historyMore) && (
        <button className="conversation__more" onClick={() => void showEarlier()}>
          earlier messages
        </button>
      )}
      {deep && (
        <button className="conversation__more" onClick={() => setShown(SHALLOW)}>
          show less
        </button>
      )}

      {visible.map((utterance, index) => {
        const age = visible.length - 1 - index;
        // Fade more gently when showing more, or twenty turns would end in
        // a wall of invisible text.
        const step = deep ? 0.055 : 0.22;
        // Scrolled back to read: everything at full strength. At the bottom,
        // the older turns fade toward the top as before.
        const opacity = utterance.streaming || scrolledUp ? 1 : Math.max(0.16, 1 - age * step);

        return (
          <div
            key={utterance.id}
            className={[
              'utterance',
              utterance.role === 'jarvis' ? 'utterance--jarvis' : 'utterance--user',
              utterance.error ? 'utterance--error' : '',
            ].join(' ')}
            style={{ opacity }}
          >
            <div className="utterance__who">
              {utterance.role === 'jarvis' ? 'JARVIS' : 'YOU'}
              {utterance.ts > 0 && (
                <time className="utterance__time" dateTime={new Date(utterance.ts).toISOString()}
                      title={new Date(utterance.ts).toLocaleString()}>
                  {stamp(utterance.ts)}
                </time>
              )}
            </div>

            {utterance.tools && utterance.tools.length > 0 && (
              <div className="tooltrace">
                {utterance.tools.map((tool, i) => (
                  <button
                    key={`${tool.name}-${i}`}
                    onClick={() => setInspecting(
                      inspecting === `${utterance.id}:${i}` ? null : `${utterance.id}:${i}`)}
                    aria-expanded={inspecting === `${utterance.id}:${i}`}
                    className={[
                      'tooltrace__item',
                      tool.ok === true ? 'tooltrace__item--ok' : '',
                      tool.ok === false ? 'tooltrace__item--fail' : '',
                      tool.flagged ? 'tooltrace__item--flagged' : '',
                    ].join(' ')}
                    title={[
                      tool.flagged
                        ? 'What this brought back contained text that looked like an attempt '
                          + 'to give JARVIS instructions. It was handed over as information '
                          + 'only, with a warning not to follow it.'
                        : '',
                      tool.redacted
                        ? `${tool.redacted} password or key removed before JARVIS read it.`
                        : '',
                      tool.summary ?? '',
                    ].filter(Boolean).join('\n\n')}
                  >
                    {tool.ok === undefined ? (
                      <span className="tooltrace__spinner" />
                    ) : (
                      <span>{tool.flagged ? '⚠' : tool.ok ? '✓' : '✕'}</span>
                    )}
                    {tool.name}
                    {tool.flagged && <span className="tooltrace__note">untrusted</span>}
                    {!tool.flagged && !!tool.redacted && (
                      <span className="tooltrace__note">secret hidden</span>
                    )}
                  </button>
                ))}
              </div>
            )}
            {utterance.tools?.map((tool, i) => inspecting === `${utterance.id}:${i}` && (
              <ToolCard key={`card-${i}`} tool={tool} />
            ))}

            <div className="utterance__text">
              {/* Only JARVIS writes markdown. Rendering the user's own
                  message would mangle anything they typed with an asterisk
                  in it, which is not a trade worth making. */}
              {utterance.role === 'jarvis'
                ? <Markdown text={utterance.text} />
                : utterance.text}
              {utterance.streaming && <span className="utterance__caret" />}
              {utterance.streaming && <Waiting since={utterance.ts} />}
            </div>

            {utterance.role === 'jarvis' && !utterance.streaming && (
              <Sources utterance={utterance} />
            )}

            {utterance.role === 'jarvis' && !utterance.streaming && utterance.telemetry && (
              <XRay telemetry={utterance.telemetry} />
            )}

            {!utterance.streaming && utterance.text && (
              <div className="utterance__actions">
                <button onClick={() => void copy(utterance)}
                        title="Copy this to the clipboard">
                  {copied === utterance.id ? 'copied' : 'copy'}
                </button>
                {utterance.role === 'user' && (
                  <button onClick={() => void send(utterance.text, 'text')}
                          title="Ask this again">
                    ask again
                  </button>
                )}
                {utterance.role === 'jarvis' && utterance.error && askedBefore(utterance.id) && (
                  <button onClick={() => void send(askedBefore(utterance.id)!, 'text')}
                          title="Send the question that failed again">
                    try again
                  </button>
                )}
                {utterance.role === 'jarvis' && !utterance.error && (
                  <button onClick={() => void readAloud(utterance)}
                          title={reading === utterance.id ? 'Stop reading' : 'Read this reply aloud'}>
                    {reading === utterance.id ? 'stop' : 'read aloud'}
                  </button>
                )}
              </div>
            )}
          </div>
        );
      })}
    </div>
    </>
  );
}


/**
 * "(2) JARVIS" in the browser tab while replies arrive unseen.
 *
 * A local model can take twenty seconds to answer, which is long enough to
 * go and do something else in another tab -- and then there was no way to
 * know it had answered without coming back to look. Counts finished replies
 * that arrived while the tab was hidden; clears the moment it is looked at.
 */
function useUnreadInTitle(
  utterances: { id: string; role: string; ts: number; streaming?: boolean }[],
) {
  const seenRef = useRef<Set<string>>(new Set());
  const unreadRef = useRef(0);
  // Anything older than this page is history being loaded, not news.
  const openedAtRef = useRef(Date.now());

  const paint = () => {
    // Its own marker, wherever it is: While away puts a ✓ in the title too.
    const base = document.title.replace(/\(\d+\) /g, '');
    document.title = unreadRef.current ? `(${unreadRef.current}) ${base}` : base;
  };

  useEffect(() => {
    let fresh = 0;
    for (const u of utterances) {
      if (u.role !== 'jarvis' || u.streaming || seenRef.current.has(u.id)) continue;
      seenRef.current.add(u.id);
      if (document.hidden && u.ts >= openedAtRef.current) fresh += 1;
    }
    if (fresh) {
      unreadRef.current += fresh;
      paint();
    }
  }, [utterances]);

  useEffect(() => {
    const onVisible = () => {
      if (document.hidden || !unreadRef.current) return;
      unreadRef.current = 0;
      paint();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => document.removeEventListener('visibilitychange', onVisible);
  }, []);
}

/**
 * What a reply cost to produce, measured: model, tokens, speed, time, and
 * the energy the GPU used while it was being written.
 *
 * One quiet line under the reply, more on a click. Every figure is a
 * measurement taken while the reply was produced -- nothing here is a
 * rule-of-thumb estimate except the cloud price, which says what it is
 * compared against.
 *
 * Ported idea: OpenJarvis's per-message "X-ray" footer.
 */
function XRay({ telemetry: t }: { telemetry: Telemetry }) {
  const [open, setOpen] = useState(false);

  const brief = [
    t.model,
    t.total_ms != null ? seconds(t.total_ms) : '',
    t.tokens_per_sec ? `${Math.round(t.tokens_per_sec)} tok/s` : '',
    t.energy_j != null ? `${t.energy_j < 10 ? t.energy_j.toFixed(1) : Math.round(t.energy_j)} J` : '',
  ].filter(Boolean).join(' · ');
  if (!brief) return null;

  const rows: [string, string][] = [];
  if (t.model) rows.push(['Model', `${t.model}${t.provider ? ` (${t.provider})` : ''}`]);
  rows.push(['Tokens', `${t.input_tokens ?? 0} read · ${t.output_tokens ?? 0} written`
    + (t.calls && t.calls > 1 ? ` · ${t.calls} model calls` : '')]);
  if (t.tokens_per_sec) rows.push(['Speed', `${t.tokens_per_sec} tokens a second`]);
  const timing = [
    t.ttft_ms != null ? `first words after ${seconds(t.ttft_ms)}` : '',
    t.total_ms != null ? `finished after ${seconds(t.total_ms)}` : '',
  ].filter(Boolean).join(' · ');
  if (timing) rows.push(['Time', timing]);
  if (t.load_ms && t.load_ms > 200) rows.push(['Model load', `${seconds(t.load_ms)} (was not in memory)`]);
  if (t.energy_j != null) {
    rows.push(['GPU energy', `${t.energy_j} J`
      + (t.avg_power_w ? ` · ${t.avg_power_w} W average` : '')
      + (t.tokens_per_joule ? ` · ${t.tokens_per_joule} tokens per joule` : '')]);
  }
  if (t.cloud_cost_usd != null) {
    rows.push(['Cloud price', `≈ $${t.cloud_cost_usd < 0.01 ? t.cloud_cost_usd.toFixed(4)
      : t.cloud_cost_usd.toFixed(3)} at $3 / $15 per million tokens — free here`]);
  }

  return (
    <div className="xray">
      <button className="xray__line" onClick={() => setOpen(!open)}
              aria-expanded={open} title="What producing this reply took">
        {brief}
      </button>
      {open && (
        <div className="xray__rows">
          {rows.map(([label, value]) => (
            <div className="xray__row" key={label}>
              <span className="xray__label">{label}</span>
              <span>{value}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/**
 * One step of a reply, opened up: what JARVIS asked a tool for, what came
 * back, how long it took, and anything the safety screen found.
 *
 * Ported idea: OpenJarvis's tool-call cards and trace debugger -- the reply
 * shown as the steps that produced it, not only as its final text.
 */
/**
 * The pages an answer came from, as links under it: "CBS Sports · USA TODAY".
 * Only pages a tool actually read, never a search result it only glanced at,
 * so each link is somewhere the answer can be checked.
 */
function Sources({ utterance }: { utterance: Utterance }) {
  const seen = new Set<string>();
  const sources = (utterance.tools ?? [])
    .flatMap((tool) => tool.sources ?? [])
    .filter((s) => /^https?:\/\//i.test(s.url) && !seen.has(s.url) && !!seen.add(s.url))
    .slice(0, 5);
  if (!sources.length) return null;
  // Three articles from one site read "CBS Sports CBS Sports CBS Sports":
  // a second and third from the same place are numbered instead.
  const count = new Map<string, number>();
  return (
    <div className="utterance__sources">
      <span className="utterance__sources-label">from</span>
      {sources.map((s) => {
        const name = s.source || s.url.replace(/^https?:\/\/(www\.)?/i, '').split('/')[0];
        const n = (count.get(name) ?? 0) + 1;
        count.set(name, n);
        return (
          <a key={s.url} href={s.url} target="_blank" rel="noopener noreferrer" title={s.title}>
            {n === 1 ? name : `${name} (${n})`}
          </a>
        );
      })}
    </div>
  );
}

function ToolCard({ tool }: { tool: NonNullable<Utterance['tools']>[number] }) {
  const input = tool.input && Object.keys(tool.input).length
    ? JSON.stringify(tool.input, null, 2) : '';
  return (
    <div className={`toolcard ${tool.flagged ? 'toolcard--flagged' : ''}`}>
      <div className="toolcard__head">
        <span className="toolcard__name">{tool.name}</span>
        <span className="toolcard__meta">
          {tool.ok === undefined ? 'running…' : tool.ok ? 'done' : 'failed'}
          {tool.durationMs != null && ` · ${seconds(tool.durationMs)}`}
        </span>
      </div>
      {input && (
        <>
          <div className="toolcard__label">Asked with</div>
          <pre className="toolcard__body">{input}</pre>
        </>
      )}
      {tool.summary && (
        <>
          <div className="toolcard__label">Got back</div>
          <pre className="toolcard__body">{tool.summary}</pre>
        </>
      )}
      {tool.flagged && (
        <div className="toolcard__warn">
          This contained text that looked like an attempt to give JARVIS instructions. It was
          handed over as information only, with a warning not to follow it.
        </div>
      )}
      {!!tool.redacted && (
        <div className="toolcard__warn">
          {tool.redacted} password or key was removed before JARVIS read it.
        </div>
      )}
    </div>
  );
}

function seconds(ms: number): string {
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

/** When a turn happened: the time today, the day and time before that. */
function stamp(ts: number): string {
  const when = new Date(ts);
  const time = when.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false });
  const today = new Date();
  if (when.toDateString() === today.toDateString()) return time;
  const day = when.toLocaleDateString([], { day: 'numeric', month: 'short' });
  return `${day}, ${time}`;
}

/**
 * How long this reply has been coming.
 *
 * A local model thinking for twenty seconds and a local model that has
 * quietly died look identical: a caret, blinking. The elapsed count is the
 * difference between waiting and wondering.
 *
 * It stays absent until the pause is long enough to be worth remarking on
 * -- a number appearing after half a second would just be noise on the
 * replies that are already fast.
 */
function Waiting({ since }: { since: number }) {
  const [seconds, setSeconds] = useState(() =>
    Math.floor((Date.now() - since) / 1000));

  useEffect(() => {
    const timer = window.setInterval(
      () => setSeconds(Math.floor((Date.now() - since) / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, [since]);

  if (seconds < QUIET_SECONDS) return null;
  return <span className="utterance__waiting">{seconds}s</span>;
}

// ---------------------------------------------------------------------------
/**
 * Copy text, including where the modern clipboard API will not work.
 *
 * `navigator.clipboard` requires a secure context. On this machine JARVIS is
 * reached at localhost, which counts — but from a phone it is reached over
 * Tailscale at `http://100.64.0.10:8000`, which does not. So on the device
 * where copying is most useful, the modern API is unavailable and the
 * button would have failed every single time.
 *
 * The deprecated `execCommand` path has no such requirement, so it is kept
 * as the fallback rather than as the primary: it is also synchronous and
 * selection-based, which is worse in every way except working here.
 */
export async function copyText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Fall through and try the old way.
  }

  try {
    const scratch = document.createElement('textarea');
    scratch.value = text;
    // Off-screen but focusable: `display: none` cannot be selected.
    scratch.setAttribute('readonly', '');
    scratch.style.position = 'fixed';
    scratch.style.top = '-1000px';
    scratch.style.opacity = '0';
    document.body.appendChild(scratch);
    scratch.select();
    // iOS ignores select() on a read-only field; it needs the range set.
    scratch.setSelectionRange(0, text.length);
    const worked = document.execCommand('copy');
    document.body.removeChild(scratch);
    return worked;
  } catch {
    return false;
  }
}
