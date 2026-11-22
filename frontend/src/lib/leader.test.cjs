/**
 * Cases for the speaker lease, run under node against the compiled module.
 *
 * The lease decides which tab talks. Getting it wrong is either a chorus of
 * overlapping voices or an assistant that has gone mute, and neither is
 * something to find out by opening three tabs and listening.
 *
 * node leader.test.cjs <path-to-compiled-leader.js>
 */
const assert = require('assert');
const path = require('path');

const modulePath = path.resolve(process.argv[2]);

// ---------------------------------------------------------------------------
// A fake browser. Each "tab" gets its own module instance but they share one
// localStorage, which is exactly the real arrangement.
// ---------------------------------------------------------------------------
function makeWorld() {
  const store = new Map();
  let now = 1_000_000;
  const timers = [];

  const storage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => { store.set(k, String(v)); },
    removeItem: (k) => { store.delete(k); },
  };

  return {
    now: () => now,
    advance(ms) {
      now += ms;
      // Fire anything due. Timers carry an absolute deadline.
      for (let i = 0; i < timers.length; i += 1) {
        if (timers[i] && timers[i].at <= now) {
          const fn = timers[i].fn;
          timers[i] = null;
          fn();
        }
      }
    },
    timers,
    storage,
  };
}

/**
 * Load a fresh copy of the module as a new "tab".
 *
 * Each tab gets its own `window` -- including its own interval registry --
 * and they share only `localStorage`, which is the real arrangement. Sharing
 * the timers too would mean a tab that has "closed" carries on renewing its
 * lease, and the takeover case could never be tested.
 */
/** Every tab's BroadcastChannel, so a post reaches the others. */
function makeChannels() {
  const open = [];
  return {
    open,
    Ctor: class FakeBroadcastChannel {
      constructor(name) { this.name = name; this.onmessage = null; open.push(this); }
      postMessage(data) {
        for (const other of open) {
          if (other !== this && other.name === this.name && other.onmessage) {
            other.onmessage({ data });
          }
        }
      }
    },
  };
}

function loadTab(world) {
  const intervals = [];
  const win = {
    localStorage: world.storage,
    setTimeout: (fn, ms) => { world.timers.push({ fn, at: world.now() + ms }); },
    clearTimeout: () => {},
    setInterval: (fn) => { intervals.push(fn); return intervals.length; },
    clearInterval: () => {},
    addEventListener: () => {},
  };

  delete require.cache[modulePath];
  global.window = win;
  global.localStorage = world.storage;
  if (!world.channels) world.channels = makeChannels();
  global.BroadcastChannel = world.channels.Ctor;
  const realNow = Date.now;
  Date.now = world.now;
  const mod = require(modulePath);
  Date.now = realNow;
  return { lease: mod.speakerLease, intervals, world };
}

function withClock(world, fn) {
  const realNow = Date.now;
  Date.now = world.now;
  try { return fn(); } finally { Date.now = realNow; }
}

/** Run the given tabs' interval callbacks once, as the browser would. */
function tickAll(tabs, world) {
  withClock(world, () => tabs.forEach((tab) => tab.intervals.forEach((fn) => fn())));
}

let passed = 0;
function check(name, fn) {
  try {
    fn();
    passed += 1;
  } catch (error) {
    console.error(`FAIL: ${name}\n  ${error.message}`);
    process.exitCode = 1;
  }
}

// ---------------------------------------------------------------------------
check('a lone tab speaks', () => {
  const world = makeWorld();
  const a = loadTab(world);
  withClock(world, () => a.lease.start());
  world.advance(200);                       // let the claim settle
  assert.strictEqual(a.lease.shouldSpeak, true, 'the only tab must speak');
});

check('a tab speaks before the election has settled', () => {
  // Better a rare duplicated sentence than a page that never talks because
  // an election did not resolve.
  const world = makeWorld();
  const a = loadTab(world);
  withClock(world, () => a.lease.start());
  assert.strictEqual(a.lease.shouldSpeak, true);
});

check('a second tab defers to a live lease', () => {
  const world = makeWorld();
  const a = loadTab(world);
  withClock(world, () => a.lease.start());
  world.advance(200);
  assert.strictEqual(a.lease.shouldSpeak, true, 'first tab holds it');

  const b = loadTab(world);                 // shares world.storage
  withClock(world, () => b.lease.start());
  world.advance(200);
  assert.strictEqual(b.lease.shouldSpeak, false, 'second tab must stay quiet');
});

check('an abandoned lease is taken over', () => {
  const world = makeWorld();
  const a = loadTab(world);
  withClock(world, () => a.lease.start());
  world.advance(200);

  const b = loadTab(world);
  withClock(world, () => b.lease.start());
  world.advance(200);
  assert.strictEqual(b.lease.shouldSpeak, false);

  // The first tab goes away without releasing: closed, crashed, suspended.
  // Only B keeps ticking, so A's lease ages out.
  world.advance(10_000);
  tickAll([b], world);
  world.advance(300);
  assert.strictEqual(b.lease.shouldSpeak, true, 'an expired lease must be claimable');
});

check('the holder keeps the lease while it renews', () => {
  const world = makeWorld();
  const a = loadTab(world);
  withClock(world, () => a.lease.start());
  world.advance(200);

  for (let i = 0; i < 10; i += 1) {
    world.advance(2000);
    tickAll([a], world);
  }
  assert.strictEqual(a.lease.shouldSpeak, true, 'the holder must not lose its own lease');
});

check('unreadable storage does not mute the tab', () => {
  // Private browsing, blocked site data, a corrupt value. Silence would be
  // the worst possible response to not being able to read a flag.
  const world = makeWorld();
  world.storage.getItem = () => { throw new Error('blocked'); };
  world.storage.setItem = () => { throw new Error('blocked'); };
  const a = loadTab(world);
  withClock(world, () => a.lease.start());
  world.advance(300);
  assert.strictEqual(a.lease.shouldSpeak, true, 'a tab with no storage must still speak');
});

check('a corrupt lease value is ignored rather than trusted', () => {
  const world = makeWorld();
  world.storage.setItem('jarvis.speaker', 'not json at all');
  const a = loadTab(world);
  withClock(world, () => a.lease.start());
  world.advance(300);
  assert.strictEqual(a.lease.shouldSpeak, true);
});

check('a lease missing its fields is ignored', () => {
  const world = makeWorld();
  world.storage.setItem('jarvis.speaker', JSON.stringify({ nonsense: true }));
  const a = loadTab(world);
  withClock(world, () => a.lease.start());
  world.advance(300);
  assert.strictEqual(a.lease.shouldSpeak, true);
});

check('exactly one of several tabs ends up speaking', () => {
  const world = makeWorld();
  const tabs = [loadTab(world), loadTab(world), loadTab(world), loadTab(world)];
  tabs.forEach((tab) => withClock(world, () => tab.lease.start()));
  world.advance(400);
  tickAll(tabs, world);
  world.advance(400);

  const speaking = tabs.filter((tab) => tab.lease.shouldSpeak).length;
  assert.strictEqual(speaking, 1, `expected exactly one speaker, got ${speaking}`);
});

check('starting twice is harmless', () => {
  const world = makeWorld();
  const a = loadTab(world);
  withClock(world, () => { a.lease.start(); a.lease.start(); });
  world.advance(300);
  assert.strictEqual(a.lease.shouldSpeak, true);
});

// ---------------------------------------------------------------------------
// Claiming a line, so two tabs never say the same thing at once.
// ---------------------------------------------------------------------------
check('a line can be claimed once', () => {
  const world = makeWorld();
  const a = loadTab(world);
  assert.strictEqual(a.lease.constructor ? true : true, true);
  const { claimUtterance } = require(modulePath);
  assert.strictEqual(claimUtterance('the cpu is at forty percent'), true);
});

check('the same tab will not repeat a line it just claimed', () => {
  const world = makeWorld();
  loadTab(world);
  const { claimUtterance } = require(modulePath);
  assert.strictEqual(claimUtterance('hello there'), true);
  assert.strictEqual(claimUtterance('hello there'), false);
});

check('a second tab will not repeat a line the first just claimed', () => {
  // This is the chorus, and the case the lease alone cannot always cover.
  const world = makeWorld();
  const a = loadTab(world);
  const first = require(modulePath);
  const b = loadTab(world);
  const second = require(modulePath);

  assert.strictEqual(first.claimUtterance('your cpu is at forty percent'), true);
  assert.strictEqual(second.claimUtterance('your cpu is at forty percent'), false,
    'the second tab must not say the same line');
});

check('a different line is not blocked', () => {
  const world = makeWorld();
  loadTab(world);
  const first = require(modulePath);
  loadTab(world);
  const second = require(modulePath);

  assert.strictEqual(first.claimUtterance('one thing'), true);
  assert.strictEqual(second.claimUtterance('a different thing'), true);
});

check('with no BroadcastChannel a tab still speaks', () => {
  // Silence is the worst possible answer to a missing browser feature; the
  // lease is still doing the real work.
  const world = makeWorld();
  loadTab(world);
  delete global.BroadcastChannel;
  delete require.cache[modulePath];
  const mod = require(modulePath);
  assert.strictEqual(mod.claimUtterance('something'), true);
});

if (process.exitCode) {
  console.error(`\n${passed} passed, with failures above.`);
} else {
  console.log(`speaker lease: ${passed} cases passed`);
}
