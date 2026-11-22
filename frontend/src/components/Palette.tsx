import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { copyText } from './Conversation';
import { search, type Candidate } from '../lib/palette';
import { useStore, type Panel, type Utterance } from '../state/store';

/**
 * Ctrl+K.
 *
 * Everything this opens was already reachable -- the panels are in the
 * sidebar, past commands are in the history panel, sleep is a button. What
 * was missing is the fast path: a way to get to any of it without taking
 * your hands off the keyboard or remembering which panel a thing lives in.
 *
 * Two rules it holds to:
 *
 *   * **It only lists things that exist.** The panel entries come from the
 *     same list the sidebar renders, so one cannot drift from the other, and
 *     recent commands are the real ones out of the store. Nothing here is a
 *     plausible-looking action that does nothing when chosen.
 *   * **Running a command runs it.** Choosing a past command sends it again
 *     for real. It does not paste it into the box and leave you to press
 *     enter, which would be a nicer demo and a worse tool.
 */

const PANELS: { key: Exclude<Panel, null>; label: string; keywords?: string }[] = [
  { key: 'away', label: 'While away',
    keywords: 'unattended work build app leave working out away report' },
  { key: 'workshop', label: 'Workshop', keywords: 'wallpapers packs images make generate' },
  { key: 'skills', label: 'Skills', keywords: 'skill instructions how-to recipes slash commands import' },
  { key: 'plugins', label: 'Plug-ins', keywords: 'mcp servers tools extensions connect add' },
  { key: 'documents', label: 'Documents', keywords: 'files folders pdf notes index search ask rag' },
  { key: 'email', label: 'Email', keywords: 'mail inbox gmail unread messages send reply write' },
  { key: 'goals', label: 'Goals' },
  { key: 'tasks', label: 'Tasks', keywords: 'jobs queue background running' },
  { key: 'agents', label: 'Agents' },
  { key: 'research', label: 'Research' },
  { key: 'notifications', label: 'Notifications',
    keywords: 'alerts unread desktop toasts messages missed' },
  { key: 'voice', label: 'What it heard', keywords: 'voice wake word microphone heard' },
  { key: 'activity', label: 'Activity', keywords: 'log events' },
  { key: 'calendar', label: 'Calendar', keywords: 'events schedule diary' },
  { key: 'automations', label: 'Automations', keywords: 'rules triggers' },
  { key: 'memory', label: 'Memory', keywords: 'remember facts' },
  { key: 'developer', label: 'Developer', keywords: 'code git workspace' },
  { key: 'system', label: 'System', keywords: 'cpu memory disk processes' },
  { key: 'integrations', label: 'Integrations', keywords: 'connections github weather' },
  { key: 'history', label: 'Conversation history', keywords: 'past chats' },
  { key: 'selfedit', label: 'Self-editing', keywords: 'code change itself' },
  { key: 'settings', label: 'Settings', keywords: 'preferences options config voice' },
];

interface Item extends Candidate {
  run: () => void;
}

export function Palette() {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState('');
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  const setPanel = useStore((s) => s.setPanel);
  const send = useStore((s) => s.send);
  const sleep = useStore((s) => s.sleep);
  const utterances = useStore((s) => s.utterances);
  const sleeping = useStore((s) => s.sleeping);
  const notify = useStore((s) => s.notify);
  const clearConversation = useStore((s) => s.clearConversation);

  const items: Item[] = useMemo(() => {
    const panels: Item[] = PANELS.map((entry) => ({
      id: `panel:${entry.key}`,
      label: entry.label,
      group: 'Panel',
      keywords: entry.keywords,
      run: () => setPanel(entry.key),
    }));

    // The real things that were actually asked, most recent first, deduped.
    const seen = new Set<string>();
    const recent: Item[] = [];
    for (let i = utterances.length - 1; i >= 0 && recent.length < 12; i -= 1) {
      const utterance = utterances[i];
      if (utterance.role !== 'user') continue;
      const text = utterance.text.trim();
      if (!text || seen.has(text.toLowerCase())) continue;
      seen.add(text.toLowerCase());
      recent.push({
        id: `again:${utterance.id}`,
        label: text,
        group: 'Ask again',
        run: () => void send(text, 'text'),
      });
    }

    // Things to do with the conversation on screen -- offered only when
    // there is one, so none of them can be chosen to no effect.
    const said = utterances.filter((u) => !u.streaming && u.text.trim());
    const conversation: Item[] = said.length === 0 ? [] : [
      { id: 'action:save-conversation', label: 'Save this conversation as a file',
        group: 'Action', keywords: 'export download transcript markdown keep',
        run: () => {
          download(transcriptName(), transcript(said));
          notify('Saved the conversation as a file.');
        } },
      { id: 'action:copy-conversation', label: 'Copy this conversation',
        group: 'Action', keywords: 'clipboard transcript all',
        run: () => void copyText(transcript(said)).then((worked) =>
          notify(worked ? 'Copied the whole conversation.'
                        : 'Could not copy — your browser refused clipboard access.')) },
      { id: 'action:new-conversation', label: 'Start a new conversation',
        group: 'Action', keywords: 'clear fresh reset blank new chat',
        run: () => { clearConversation(); notify('New conversation. The old one is in History.', 'history'); } },
    ];

    return [
      ...panels,
      ...recent,
      ...conversation,
      { id: 'action:sleep', label: 'Sleep', group: 'Action',
        keywords: 'rest black screen off', run: () => void sleep() },
    ];
  }, [setPanel, send, sleep, utterances, notify, clearConversation]);

  const results = useMemo(() => search(items, query, 14), [items, query]);

  const close = useCallback(() => {
    setOpen(false);
    setQuery('');
    setCursor(0);
  }, []);

  // -- Ctrl+K anywhere -------------------------------------------------
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const combo = (event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k';
      if (combo) {
        event.preventDefault();
        setOpen((was) => !was);
        return;
      }
      if (event.key === 'Escape' && open) {
        event.preventDefault();
        close();
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open, close]);

  useEffect(() => {
    if (open) inputRef.current?.focus();
  }, [open]);

  // Asleep means asleep: nothing opens over a black screen.
  useEffect(() => {
    if (sleeping && open) close();
  }, [sleeping, open, close]);

  // Keep the highlighted row in view when arrowing past the fold.
  useEffect(() => {
    const row = listRef.current?.children[cursor] as HTMLElement | undefined;
    row?.scrollIntoView({ block: 'nearest' });
  }, [cursor]);

  if (!open || sleeping) return null;

  const choose = (index: number) => {
    const match = results[index];
    if (!match) return;
    close();
    (match.item as Item).run();
  };

  const onKeyDown = (event: React.KeyboardEvent) => {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      setCursor((c) => Math.min(c + 1, Math.max(results.length - 1, 0)));
    } else if (event.key === 'ArrowUp') {
      event.preventDefault();
      setCursor((c) => Math.max(c - 1, 0));
    } else if (event.key === 'Enter') {
      event.preventDefault();
      choose(cursor);
    }
  };

  return (
    <div className="palette" role="dialog" aria-label="Command palette" aria-modal="true">
      {/* Clicking away closes it, as a dialog should. */}
      <button className="palette__scrim" onClick={close} aria-label="Close" tabIndex={-1} />

      <div className="palette__box">
        <div className="palette__field">
          <span className="palette__prompt" aria-hidden="true">⌕</span>
          <input
            ref={inputRef}
            className="palette__input"
            value={query}
            onChange={(event) => { setQuery(event.target.value); setCursor(0); }}
            onKeyDown={onKeyDown}
            placeholder="Go to a panel, ask something again, save the conversation…"
            aria-label="Search panels and commands"
            autoComplete="off"
            spellCheck={false}
          />
          <kbd className="palette__hint">Esc</kbd>
        </div>

        <div className="palette__list" ref={listRef}>
          {results.length === 0 && (
            <div className="palette__empty">
              Nothing matches “{query}”.
            </div>
          )}
          {results.map((match, index) => (
            <button
              key={match.item.id}
              className={`palette__row ${index === cursor ? 'palette__row--on' : ''}`}
              onMouseEnter={() => setCursor(index)}
              onClick={() => choose(index)}
            >
              <span className="palette__label">
                {highlight(match.item.label, match.hits)}
              </span>
              <span className="palette__group">{match.item.group}</span>
            </button>
          ))}
        </div>

        <div className="palette__footer">
          <kbd>↑</kbd><kbd>↓</kbd> move
          <kbd>↵</kbd> choose
          <kbd>Ctrl</kbd>+<kbd>K</kbd> toggle
        </div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
/** The conversation as plain Markdown: who, when, what. */
function transcript(turns: Utterance[]): string {
  const lines = [`# Conversation with JARVIS`, '',
                 `Saved ${new Date().toLocaleString()}`, ''];
  for (const turn of turns) {
    const who = turn.role === 'jarvis' ? 'JARVIS' : 'You';
    const when = turn.ts ? new Date(turn.ts).toLocaleString() : '';
    lines.push(`## ${who}${when ? ` · ${when}` : ''}`, '', turn.text.trim(), '');
  }
  return lines.join('\n');
}

function transcriptName(): string {
  const now = new Date();
  const pad = (n: number) => String(n).padStart(2, '0');
  return `jarvis-conversation-${now.getFullYear()}-${pad(now.getMonth() + 1)}-`
    + `${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}.md`;
}

/** Hand the browser a file to save, made here rather than on the server. */
function download(name: string, text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }));
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  document.body.appendChild(link);
  link.click();
  link.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 2000);
}

/** Mark the characters that matched, so it is obvious why a row is listed. */
function highlight(label: string, hits: number[]) {
  if (!hits.length) return label;
  const marked = new Set(hits);
  return Array.from(label).map((char, i) =>
    marked.has(i)
      ? <mark key={i} className="palette__hit">{char}</mark>
      : <span key={i}>{char}</span>);
}
