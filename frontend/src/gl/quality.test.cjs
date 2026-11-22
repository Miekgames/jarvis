/**
 * Cases for adaptive particle quality, run under node against the compiled
 * module.
 *
 * The field is the application's primary identity, so quietly halving it and
 * never putting it back is not a small bug. The rule this replaced did
 * exactly that on a single bad two-second window.
 *
 * node quality.test.cjs <path-to-compiled-quality.js>
 */
const assert = require('assert');
const path = require('path');

const {
  qualityDecision,
  DEGRADE_BELOW_FPS,
  RESTORE_ABOVE_FPS,
  BAD_WINDOWS_TO_DEGRADE,
  GOOD_WINDOWS_TO_RESTORE,
} = require(path.resolve(process.argv[2]));

const FRESH = { degraded: false, badWindows: 0, goodWindows: 0 };

/** Feed a run of identical measurements and collect what happened. */
function feed(state, fps, times) {
  const actions = [];
  for (let i = 0; i < times; i += 1) {
    const result = qualityDecision(fps, state);
    state = result.next;
    actions.push(result.action);
  }
  return { state, actions };
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
check('a single bad window does not degrade', () => {
  // The whole point. A tab switch or a passing GPU spike is not evidence that
  // the machine cannot render the field.
  const { state, actions } = feed(FRESH, 20, 1);
  assert.strictEqual(actions[0], 'none');
  assert.strictEqual(state.degraded, false);
});

check('sustained low frame rate does degrade', () => {
  const { state, actions } = feed(FRESH, 20, BAD_WINDOWS_TO_DEGRADE);
  assert.strictEqual(actions[actions.length - 1], 'degrade');
  assert.strictEqual(state.degraded, true);
});

check('degrading happens once, not on every window after it', () => {
  const { actions } = feed(FRESH, 20, 10);
  const degrades = actions.filter((a) => a === 'degrade').length;
  assert.strictEqual(degrades, 1, `expected one degrade, got ${degrades}`);
});

check('quality is restored when the frame rate recovers', () => {
  let { state } = feed(FRESH, 20, BAD_WINDOWS_TO_DEGRADE);
  assert.strictEqual(state.degraded, true);

  const recovery = feed(state, 60, GOOD_WINDOWS_TO_RESTORE);
  assert.strictEqual(recovery.actions[recovery.actions.length - 1], 'restore');
  assert.strictEqual(recovery.state.degraded, false);
});

check('one good window is not enough to restore', () => {
  let { state } = feed(FRESH, 20, BAD_WINDOWS_TO_DEGRADE);
  const recovery = feed(state, 60, 1);
  assert.strictEqual(recovery.actions[0], 'none');
  assert.strictEqual(recovery.state.degraded, true);
});

check('a stutter during recovery resets the count', () => {
  let { state } = feed(FRESH, 20, BAD_WINDOWS_TO_DEGRADE);
  state = feed(state, 60, GOOD_WINDOWS_TO_RESTORE - 1).state;
  state = feed(state, 20, 1).state;              // one bad window
  const nearly = feed(state, 60, GOOD_WINDOWS_TO_RESTORE - 1);
  assert.strictEqual(nearly.state.degraded, true, 'recovery must start over after a stutter');
});

check('the dead band changes nothing', () => {
  // Between the two thresholds there is no evidence either way. A machine
  // sitting here must not flip quality back and forth every two seconds.
  const middle = (DEGRADE_BELOW_FPS + RESTORE_ABOVE_FPS) / 2;
  const { state, actions } = feed(FRESH, middle, 20);
  assert.ok(actions.every((a) => a === 'none'));
  assert.deepStrictEqual(state, FRESH);
});

check('the thresholds leave a real gap', () => {
  assert.ok(RESTORE_ABOVE_FPS > DEGRADE_BELOW_FPS + 10,
    'too narrow a gap and the field oscillates');
});

check('a degraded field sitting in the dead band stays degraded', () => {
  let { state } = feed(FRESH, 20, BAD_WINDOWS_TO_DEGRADE);
  const middle = (DEGRADE_BELOW_FPS + RESTORE_ABOVE_FPS) / 2;
  const held = feed(state, middle, 20);
  assert.strictEqual(held.state.degraded, true);
});

check('it can degrade again after recovering', () => {
  let { state } = feed(FRESH, 20, BAD_WINDOWS_TO_DEGRADE);
  state = feed(state, 60, GOOD_WINDOWS_TO_RESTORE).state;
  assert.strictEqual(state.degraded, false);
  const again = feed(state, 20, BAD_WINDOWS_TO_DEGRADE);
  assert.strictEqual(again.actions[again.actions.length - 1], 'degrade');
});

check('exactly at a threshold is not past it', () => {
  assert.strictEqual(qualityDecision(DEGRADE_BELOW_FPS, FRESH).action, 'none');
  let { state } = feed(FRESH, 20, BAD_WINDOWS_TO_DEGRADE);
  const atRestore = feed(state, RESTORE_ABOVE_FPS, GOOD_WINDOWS_TO_RESTORE);
  assert.strictEqual(atRestore.state.degraded, true, 'the restore threshold is exclusive');
});

if (process.exitCode) {
  console.error(`\n${passed} passed, with failures above.`);
} else {
  console.log(`adaptive quality: ${passed} cases passed`);
}
