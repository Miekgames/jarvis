/**
 * Which tab speaks.
 *
 * Spoken commands are answered by the backend and broadcast to every
 * connected page, which is right for displaying the conversation and wrong
 * for reading it aloud: with three tabs open you get three overlapping
 * voices. Only one tab should own the speakers.
 *
 * Election is a lease in `localStorage`, which is shared across tabs of the
 * same origin. The holder renews it; if it stops renewing -- closed, crashed,
 * suspended -- the lease expires and another tab takes over. No central
 * coordinator, no server round trip, and the failure mode is "someone else
 * speaks a second later" rather than silence.
 *
 * Correctness note: two tabs can briefly both believe they hold the lease if
 * they write in the same instant. The claim is re-read after a short delay to
 * settle that, and the cost of losing the race is one duplicated sentence,
 * not a broken page.
 */

const KEY = 'jarvis.speaker';

/** Renewed this often. */
const RENEW_MS = 2000;

/** Considered abandoned after this long without a renewal. */
const LEASE_MS = 6000;

/** How long to wait before trusting a claim, so a simultaneous write loses. */
const SETTLE_MS = 120;

/** This tab, for the lifetime of the page. */
const ME = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`;

interface Lease {
  owner: string;
  at: number;
}

function read(): Lease | null {
  try {
    const raw = window.localStorage.getItem(KEY);
    if (!raw) return null;
    const lease = JSON.parse(raw) as Lease;
    if (typeof lease?.owner !== 'string' || typeof lease?.at !== 'number') return null;
    return lease;
  } catch {
    // Private mode, blocked storage, or a corrupt value. A tab that cannot
    // read the lease must still work, so it behaves as the only tab.
    return null;
  }
}

function write(lease: Lease): boolean {
  try {
    window.localStorage.setItem(KEY, JSON.stringify(lease));
    return true;
  } catch {
    return false;
  }
}

class SpeakerLease {
  private holder = false;
  private timer = 0;
  private settled = false;

  /** Start competing for the lease. Safe to call more than once. */
  start() {
    if (this.timer) return;
    this.tick();
    this.timer = window.setInterval(() => this.tick(), RENEW_MS);
    window.addEventListener('pagehide', () => this.release());
  }

  stop() {
    if (this.timer) window.clearInterval(this.timer);
    this.timer = 0;
    this.release();
  }

  private tick() {
    const lease = read();
    const now = Date.now();

    if (lease && lease.owner !== ME && now - lease.at < LEASE_MS) {
      // Someone else holds a live lease.
      this.holder = false;
      this.settled = true;
      return;
    }

    // Free, expired, or already ours.
    const claiming = !this.holder;
    if (!write({ owner: ME, at: now })) {
      // No storage at all: behave as a lone tab rather than going mute.
      this.holder = true;
      this.settled = true;
      return;
    }

    if (claiming) {
      // Re-read after a beat. If another tab wrote at the same moment, the
      // last writer wins and this one steps back.
      this.settled = false;
      window.setTimeout(() => {
        const confirmed = read();
        this.holder = !confirmed || confirmed.owner === ME;
        this.settled = true;
      }, SETTLE_MS);
    } else {
      this.holder = true;
      this.settled = true;
    }
  }

  private release() {
    if (!this.holder) return;
    this.holder = false;
    try {
      const lease = read();
      if (lease?.owner === ME) window.localStorage.removeItem(KEY);
    } catch {
      /* nothing useful to do */
    }
  }

  /**
   * Should this tab speak?
   *
   * Before the first claim settles this returns true: a half-second of
   * possible overlap on a fresh page is a far better failure than a tab that
   * silently refuses to talk because an election never resolved.
   */
  get shouldSpeak(): boolean {
    return this.holder || !this.settled;
  }

  /** Exposed for the diagnostics panel. */
  get debug() {
    return { me: ME, holder: this.holder, settled: this.settled, lease: read() };
  }
}

export const speakerLease = new SpeakerLease();


/* ---------------------------------------------------------------------------
   A second guard: nobody says the same line twice.

   The lease decides which tab owns the speakers, and that is the main
   mechanism. This sits behind it for the cases a lease cannot cover -- two
   tabs claiming in the same instant, a tab whose lease has just expired
   because the machine was asleep, a page opened while another was mid-reply.

   Tabs announce a line as they start it and skip anything another tab has
   just announced. Costs one message and gets rid of the chorus even when the
   election is briefly wrong.
   --------------------------------------------------------------------------- */

const UTTERANCE_WINDOW_MS = 5000;
const CHANNEL = 'jarvis.speech';

/** Lines other tabs have announced recently, keyed by text. */
const announced = new Map<string, number>();

let channel: BroadcastChannel | null = null;
try {
  if (typeof BroadcastChannel !== 'undefined') {
    channel = new BroadcastChannel(CHANNEL);
    channel.onmessage = (event) => {
      const { text, at } = (event.data ?? {}) as { text?: string; at?: number };
      if (typeof text === 'string' && typeof at === 'number') announced.set(text, at);
    };
  }
} catch {
  // Not available (older browser, blocked). The lease alone still applies.
  channel = null;
}

function forget(now: number) {
  for (const [text, at] of announced) {
    if (now - at > UTTERANCE_WINDOW_MS) announced.delete(text);
  }
}

/**
 * Claim a line for this tab. False means another tab is already saying it.
 *
 * Call it immediately before speaking, not when the text arrives: the point
 * is to be the first to announce.
 */
export function claimUtterance(text: string): boolean {
  const now = Date.now();
  forget(now);

  const seen = announced.get(text);
  if (seen !== undefined && now - seen < UTTERANCE_WINDOW_MS) return false;

  announced.set(text, now);
  try {
    channel?.postMessage({ text, at: now });
  } catch {
    // Posting failed; this tab still speaks. Better one duplicate than
    // silence because a message could not be sent.
  }
  return true;
}
