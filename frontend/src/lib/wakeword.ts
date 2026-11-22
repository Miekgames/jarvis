/**
 * Wake-word matching.
 *
 * Browser speech recognition is a general-purpose dictation engine, not a
 * keyword spotter. Asked to transcribe "Jarvis" it will happily return
 * "Travis", "Javis", "service" or "drives". Requiring an exact string match is
 * why a wake word appears not to work at all: the loop is running, the audio is
 * arriving, and the one word it is waiting for almost never comes back verbatim.
 *
 * So we match tolerantly — exact word, a known mishearing, or an edit distance
 * of one on a long-enough word — and we return what came *after* the wake word
 * so "jarvis what's my CPU" is a single utterance rather than two steps.
 *
 * Deliberately dependency-free and pure, so it can be tested on its own.
 */

/** Mishearings observed from Chrome's recogniser for common wake words. */
const CONFUSIONS: Record<string, string[]> = {
  jarvis: [
    'travis', 'jarvis', 'javis', 'jervis', 'jarvus', 'garvis', 'darvis', 'harvis',
    'jarviz', 'jarvi', 'charvis', 'jarvez', 'jarv', 'drives', 'jarvis.', 'jervais',
  ],
  friday: ['friday', 'fry day', 'fridays', 'friyay'],
  computer: ['computer', 'computers', 'compute'],
};

export interface WakeMatch {
  /** Did the wake word appear? */
  hit: boolean;
  /** Everything said after it, trimmed. Empty when only the wake word was said. */
  rest: string;
  /** The token that matched, for showing the user what was actually heard. */
  matched: string;
}

const NO_MATCH: WakeMatch = { hit: false, rest: '', matched: '' };

/** Levenshtein distance, capped — we only ever care about "is it <= 1 or 2". */
export function editDistance(a: string, b: string, cap = 3): number {
  if (a === b) return 0;
  if (Math.abs(a.length - b.length) > cap) return cap + 1;

  let previous = Array.from({ length: b.length + 1 }, (_, i) => i);
  for (let i = 1; i <= a.length; i += 1) {
    const current = [i];
    let best = i;
    for (let j = 1; j <= b.length; j += 1) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      const value = Math.min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost);
      current.push(value);
      if (value < best) best = value;
    }
    // Every path through this row already exceeds the cap; no need to continue.
    if (best > cap) return cap + 1;
    previous = current;
  }
  return previous[b.length];
}

/** Strip punctuation and collapse whitespace, so "Jarvis," matches "jarvis". */
function normalise(text: string): string {
  return text
    .toLowerCase()
    .replace(/[.,!?;:"'`~()[\]{}]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
}

/** How far a single token may differ before we stop believing it. */
function tolerance(word: string): number {
  if (word.length <= 3) return 0;      // "max" vs "man" — too risky
  if (word.length <= 6) return 1;
  return 2;
}

function tokenMatches(token: string, target: string, confusions: string[]): boolean {
  if (token === target) return true;
  if (confusions.includes(token)) return true;
  return editDistance(token, target, tolerance(target)) <= tolerance(target);
}

/**
 * Look for `wakeWord` in `transcript`.
 *
 * Multi-word wake words ("hey jarvis") are matched as a phrase. The search runs
 * over every position, so the wake word does not have to start the utterance —
 * recognisers routinely prepend filler like "um" or "so".
 */
export function matchWakeWord(transcript: string, wakeWord: string): WakeMatch {
  const text = normalise(transcript);
  const target = normalise(wakeWord || 'jarvis');
  if (!text || !target) return NO_MATCH;

  const tokens = text.split(' ');
  const targetTokens = target.split(' ');
  // Keyed by the distinctive final word, so "hey jarvis" gets jarvis's list.
  const confusions = CONFUSIONS[targetTokens[targetTokens.length - 1]] ?? [];

  for (let i = 0; i + targetTokens.length <= tokens.length; i += 1) {
    const window = tokens.slice(i, i + targetTokens.length);

    const matched = targetTokens.every((word, offset) => {
      // Only the final word of a multi-word wake word gets the confusion list;
      // the leading words ("hey") are short and must match closely.
      const list = offset === targetTokens.length - 1 ? confusions : [];
      return tokenMatches(window[offset], word, list);
    });

    if (matched) {
      return {
        hit: true,
        rest: tokens.slice(i + targetTokens.length).join(' ').trim(),
        matched: window.join(' '),
      };
    }
  }

  return NO_MATCH;
}
