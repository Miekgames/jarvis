import { useEffect, useState } from 'react';

import { useStore } from '../state/store';

/**
 * What you can press, on pressing `?`.
 *
 * There were already several keyboard shortcuts and nothing anywhere said
 * so, which makes them worth roughly nothing: a shortcut nobody knows about
 * is a shortcut nobody uses. This is the cheapest possible fix — a list,
 * behind the key people already try when they want one.
 *
 * It reads the user's own bindings rather than hard-coding the defaults, so
 * it cannot drift into describing keys that no longer do anything.
 */

interface Row {
  keys: string;
  what: string;
}

export function Shortcuts() {
  const [open, setOpen] = useState(false);
  const settings = useStore((s) => s.settings);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      const typing = target && (
        target.tagName === 'INPUT' ||
        target.tagName === 'TEXTAREA' ||
        target.isContentEditable
      );

      if (event.key === 'Escape' && open) {
        setOpen(false);
        return;
      }
      // `?` is a perfectly ordinary character to type into a message, so
      // the overlay only answers it when the caret is not in a field.
      if (!typing && event.key === '?') {
        event.preventDefault();
        setOpen((shown) => !shown);
      }
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [open]);

  if (!open) return null;

  const bound = settings.shortcuts ?? {};
  const rows: Row[] = [
    { keys: pretty(bound.command_bar ?? 'ctrl+space'), what: 'Type to JARVIS' },
    { keys: pretty(bound.push_to_talk ?? 'ctrl+shift+space'), what: 'Talk to JARVIS' },
    { keys: '↑ / ↓', what: 'In the message box: step through earlier messages' },
    { keys: pretty(bound.palette ?? 'ctrl+k'),
      what: 'Command palette — panels, ask again, save or copy the conversation' },
    { keys: pretty(bound.history ?? 'ctrl+h'), what: 'Conversation history' },
    { keys: pretty(bound.sleep ?? 'ctrl+shift+s'), what: 'Sleep — black screen, rendering stops' },
    { keys: pretty(bound.stop_all ?? 'ctrl+shift+x'), what: 'Stop everything running' },
    { keys: 'Esc', what: 'Close whatever is open' },
    { keys: '?', what: 'This list' },
  ];

  return (
    <div className="shortcuts" role="dialog" aria-label="Keyboard shortcuts"
         onClick={() => setOpen(false)}>
      <div className="shortcuts__box" onClick={(event) => event.stopPropagation()}>
        <div className="shortcuts__title">Keyboard</div>
        {rows.map((row) => (
          <div className="shortcuts__row" key={row.what}>
            <kbd className="shortcuts__keys">{row.keys}</kbd>
            <span className="shortcuts__what">{row.what}</span>
          </div>
        ))}
        <div className="shortcuts__hint">Press ? again, or Esc, to close.</div>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
/** "ctrl+shift+s" as something a person reads rather than parses. */
function pretty(combo: string): string {
  return combo
    .split('+')
    .map((part) => {
      switch (part.toLowerCase()) {
        case 'ctrl': return 'Ctrl';
        case 'shift': return 'Shift';
        case 'alt': return 'Alt';
        case 'meta': return 'Cmd';
        case 'space': return 'Space';
        default: return part.length === 1 ? part.toUpperCase() : part;
      }
    })
    .join(' + ');
}
