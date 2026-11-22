/**
 * What you have typed before, and what you were in the middle of typing.
 *
 * Both are kept on this device only, in localStorage, and both are
 * conveniences: if storage is unavailable (a private window, a blocked
 * site) everything still works, it simply forgets.
 */

const SENT_KEY = 'jarvis.sent';
const DRAFT_KEY = 'jarvis.draft';
const LIMIT = 60;

function read(key: string): string | null {
  try { return window.localStorage.getItem(key); } catch { return null; }
}

function write(key: string, value: string | null): void {
  try {
    if (value === null) window.localStorage.removeItem(key);
    else window.localStorage.setItem(key, value);
  } catch { /* storage refused: forget quietly */ }
}

/** Messages sent from this device, oldest first. */
export function sentMessages(): string[] {
  try {
    const parsed = JSON.parse(read(SENT_KEY) ?? '[]');
    return Array.isArray(parsed) ? parsed.filter((x) => typeof x === 'string') : [];
  } catch {
    return [];
  }
}

/** Remember a message as sent. A repeat moves to the end rather than
 *  appearing twice, so ↑ never steps through the same line over and over. */
export function rememberSent(text: string): void {
  const clean = text.trim();
  if (!clean) return;
  const list = sentMessages().filter((item) => item !== clean);
  list.push(clean);
  write(SENT_KEY, JSON.stringify(list.slice(-LIMIT)));
}

/**
 * Everything ↑ can step back through, oldest first: what was sent from this
 * device, plus the user's turns in the conversation on screen (which may
 * have been typed on another device). Deduplicated, most recent use wins.
 */
export function recallList(conversation: string[]): string[] {
  const merged: string[] = [];
  for (const text of [...sentMessages(), ...conversation]) {
    const clean = text.trim();
    if (!clean) continue;
    const at = merged.indexOf(clean);
    if (at !== -1) merged.splice(at, 1);
    merged.push(clean);
  }
  return merged;
}

export function loadDraft(): string {
  return read(DRAFT_KEY) ?? '';
}

export function saveDraft(text: string): void {
  write(DRAFT_KEY, text ? text : null);
}
