/**
 * Fuzzy matching for the command palette.
 *
 * Separate from the component because ranking is the part worth being sure
 * about, and it is far easier to pin down as a list of expected orderings
 * than by typing into a box and squinting at what comes back.
 *
 * The rule it follows: what you typed should get you where you meant to go
 * with as few keystrokes as possible, and the obvious thing should win. "set"
 * must reach Settings before it reaches "Reset conversation", and "wh" must
 * reach "What it heard" rather than any of the dozen entries with an h in
 * them somewhere.
 */

export interface Candidate {
  id: string;
  /** What the user sees and is really searching. */
  label: string;
  /** Where it lives, shown greyed beside the label. Also searchable, so
   *  "panel settings" works, but worth less than a label match. */
  group?: string;
  /** Extra words that should find it without being displayed -- the common
   *  name for a thing that is formally called something else. */
  keywords?: string;
}

export interface Match<T extends Candidate = Candidate> {
  item: T;
  score: number;
  /** Indices into `label` that matched, for highlighting. */
  hits: number[];
}

/**
 * Score one candidate against a query.
 *
 * Returns null when it does not match at all. Every character of the query
 * must appear, in order, somewhere in the searchable text -- a subsequence
 * match, so "stg" finds "Settings" but "xyz" finds nothing.
 */
export function scoreCandidate<T extends Candidate>(item: T, query: string): Match<T> | null {
  const needle = query.trim().toLowerCase();
  if (!needle) return { item, score: 0, hits: [] };

  const label = item.label.toLowerCase();

  // A run of the query appearing verbatim in the label is the strongest
  // signal there is, and worth far more than the same letters scattered.
  const direct = label.indexOf(needle);
  if (direct >= 0) {
    const hits = Array.from({ length: needle.length }, (_, i) => direct + i);
    // Starting the label beats appearing in the middle of it; a word
    // boundary beats mid-word.
    let score = 1000 - direct;
    if (direct === 0) score += 400;
    else if (/[\s\-_/]/.test(label[direct - 1])) score += 200;
    // Shorter labels win ties: "Tasks" over "Task manager settings".
    score -= label.length;
    return { item, score, hits };
  }

  // Initials: "wih" -> "What it heard".
  const initials = label.split(/[\s\-_/]+/).filter(Boolean);
  if (initials.length > 1) {
    const acronym = initials.map((word) => word[0]).join('');
    if (acronym.startsWith(needle)) {
      let at = 0;
      const hits: number[] = [];
      for (const word of initials.slice(0, needle.length)) {
        const found = label.indexOf(word, at);
        hits.push(found);
        at = found + word.length;
      }
      return { item, score: 900 - label.length, hits };
    }
  }

  // Scattered subsequence in the label. Consecutive runs score better, so
  // "setti" beats "snis" even though both technically match.
  const scattered = subsequence(label, needle);
  if (scattered) {
    return { item, score: 500 + scattered.run * 10 - label.length, hits: scattered.hits };
  }

  // Finally the things that are searchable but not displayed. A hit here is
  // real but should never outrank a visible-label match.
  const hidden = `${item.group ?? ''} ${item.keywords ?? ''}`.toLowerCase();
  if (hidden.includes(needle) || subsequence(hidden, needle)) {
    return { item, score: 100 - label.length, hits: [] };
  }

  return null;
}

/** Rank every candidate, best first, dropping the ones that do not match. */
export function search<T extends Candidate>(items: T[], query: string, limit = 12): Match<T>[] {
  const matches: Match<T>[] = [];
  for (const item of items) {
    const match = scoreCandidate(item, query);
    if (match) matches.push(match);
  }
  // Stable within a score, so an unfiltered palette keeps its authored order
  // rather than shuffling on every keystroke.
  return matches
    .map((match, index) => ({ match, index }))
    .sort((a, b) => (b.match.score - a.match.score) || (a.index - b.index))
    .slice(0, limit)
    .map(({ match }) => match);
}

/** Does `needle` appear in `haystack` in order? Reports the longest run. */
function subsequence(haystack: string, needle: string): { hits: number[]; run: number } | null {
  const hits: number[] = [];
  let at = 0;
  let run = 0;
  let best = 0;
  for (const char of needle) {
    const found = haystack.indexOf(char, at);
    if (found < 0) return null;
    run = found === at ? run + 1 : 1;
    best = Math.max(best, run);
    hits.push(found);
    at = found + 1;
  }
  return { hits, run: best };
}
