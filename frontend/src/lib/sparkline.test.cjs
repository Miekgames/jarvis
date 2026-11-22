/**
 * Cases for the sparkline's geometry.
 *
 * The interesting cases are missing data and not-enough data, and both are
 * far easier to be sure about as numbers than as pixels in a screenshot.
 *
 * node sparkline.test.cjs <path-to-compiled-Sparkline.js>
 */
const assert = require('assert');
const path = require('path');
const { buildPoints } = require(path.resolve(process.argv[2]));

let passed = 0;
function check(name, fn) {
  try { fn(); passed += 1; }
  catch (error) { console.error(`FAIL: ${name}\n  ${error.message}`); process.exitCode = 1; }
}

const coords = (result) =>
  result.line.split(' ').map((pair) => pair.split(',').map(Number));

check('a flat series draws a flat line', () => {
  const r = buildPoints([50, 50, 50, 50], 100, 40, 10);
  const ys = coords(r).map(([, y]) => y);
  assert.strictEqual(new Set(ys).size, 1, `expected one y value, got ${ys}`);
});

check('higher values sit higher on screen', () => {
  const r = buildPoints([10, 90], 100, 40, 10);
  const [[, lowY], [, highY]] = coords(r);
  assert.ok(highY < lowY, 'a larger reading must plot nearer the top');
});

check('the line spans the full width', () => {
  const r = buildPoints([1, 2, 3, 4, 5], 100, 40, 10);
  const xs = coords(r).map(([x]) => x);
  assert.strictEqual(xs[0], 0);
  assert.strictEqual(xs[xs.length - 1], 40);
});

check('the head marker sits on the newest reading', () => {
  const r = buildPoints([10, 20, 80], 100, 40, 10);
  const last = coords(r).pop();
  assert.strictEqual(r.headX, last[0]);
  assert.strictEqual(r.headY, last[1]);
});

check('gaps in the data are skipped, not plotted as zero', () => {
  // GPU and battery are absent on plenty of machines, and a missing tick
  // happens. Plotting those as 0 would invent a crash to the floor.
  const withGaps = buildPoints([50, null, 50, undefined, 50], 100, 40, 10);
  const clean = buildPoints([50, 50, 50], 100, 40, 10);
  assert.strictEqual(withGaps.line, clean.line);
});

check('too little data draws nothing at all', () => {
  // One reading is not a trend. An empty box says "not enough yet"; a flat
  // line would be a claim about data that does not exist.
  assert.strictEqual(buildPoints([], 100, 40, 10), null);
  assert.strictEqual(buildPoints([42], 100, 40, 10), null);
  assert.strictEqual(buildPoints([null, undefined], 100, 40, 10), null);
});

check('NaN is treated as missing', () => {
  assert.strictEqual(buildPoints([NaN, NaN], 100, 40, 10), null);
  assert.ok(buildPoints([10, NaN, 90], 100, 40, 10));
});

check('values beyond the scale are clamped, not drawn off the top', () => {
  const r = buildPoints([0, 150], 100, 40, 10);
  const ys = coords(r).map(([, y]) => y);
  assert.ok(Math.min(...ys) >= 0, `drew above the box: ${ys}`);
  assert.ok(Math.max(...ys) <= 10, `drew below the box: ${ys}`);
});

check('everything stays inside the box', () => {
  const r = buildPoints([0, 25, 50, 75, 100], 100, 40, 10);
  for (const [x, y] of coords(r)) {
    assert.ok(x >= 0 && x <= 40, `x out of range: ${x}`);
    assert.ok(y >= 0 && y <= 10, `y out of range: ${y}`);
  }
});

check('the scale is fixed, so a quiet series stays quiet', () => {
  // Auto-scaling would make 1%–2% jitter look like a crisis.
  const quiet = buildPoints([1, 2, 1, 2], 100, 40, 10);
  const ys = coords(quiet).map(([, y]) => y);
  assert.ok(Math.max(...ys) - Math.min(...ys) < 1,
    `a 1% range should barely move, spanned ${Math.max(...ys) - Math.min(...ys)}`);
});

if (process.exitCode) console.error(`\n${passed} passed, with failures above.`);
else console.log(`sparkline: ${passed} cases passed`);
