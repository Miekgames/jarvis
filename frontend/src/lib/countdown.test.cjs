/**
 * Cases for the countdown's arithmetic: month ends, leap days, daylight
 * saving, the moment itself.
 *
 * node countdown.test.cjs <path-to-compiled-countdown.js>
 */
const assert = require('assert');
const path = require('path');
const { left, targetParts, daysSince, inOrder, when, spoken } = require(path.resolve(process.argv[2]));

let passed = 0;
function check(name, fn) {
  try { fn(); passed += 1; }
  catch (error) { console.error(`FAIL: ${name}\n  ${error.message}`); process.exitCode = 1; }
}
const at = (text) => {
  const [d, t = '00:00:00'] = text.split(' ');
  const [year, month, day] = d.split('-').map(Number);
  const [hour, minute, second = 0] = t.split(':').map(Number);
  return { year, month: month - 1, day, hour, minute, second };
};
const is = (l, months, days, hours, minutes, seconds) =>
  assert.deepStrictEqual(l, { months, days, hours, minutes, seconds });

check('under a month shows no months', () => is(left(at('2026-11-22 18:30:15'), at('2026-12-18')), 0, 25, 5, 29, 45));
check('exactly two calendar months', () => is(left(at('2026-11-22'), at('2027-01-22')), 2, 0, 0, 0, 0));
check('one second short of a month', () => is(left(at('2026-11-22 00:00:01'), at('2026-12-22')), 0, 29, 23, 59, 59));
check('months are the largest unit: a year and a half is 18', () =>
  is(left(at('2026-11-22'), at('2028-05-22')), 18, 0, 0, 0, 0));
check('far away still counts in months', () => assert.strictEqual(left(at('2026-11-22'), at('2030-06-08')).months, 42));
check('from the 31st: February is clamped, not skipped', () =>
  is(left(at('2027-01-31'), at('2027-03-01')), 1, 1, 0, 0, 0));
check('from the 31st to the 30th', () => is(left(at('2026-12-31'), at('2027-04-30')), 4, 0, 0, 0, 0));
check('leap day', () => is(left(at('2028-01-29'), at('2028-02-29')), 1, 0, 0, 0, 0));
check('every unit is in range over four years, hour by hour', () => {
  const target = at('2030-11-22 12:00');
  for (let n = 0; n < 24 * 365 * 4; n += 7) {
    const d = new Date(Date.UTC(2026, 10, 22) + n * 3600000);
    const now = { year: d.getUTCFullYear(), month: d.getUTCMonth(), day: d.getUTCDate(),
                  hour: d.getUTCHours(), minute: 0, second: 0 };
    const l = left(now, target);
    assert.ok(l.days >= 0 && l.days <= 31 && l.hours < 24 && l.minutes < 60 && l.seconds < 60, JSON.stringify([now, l]));
  }
});
check('it only ever goes down, across every month end', () => {
  const target = at('2027-12-31 23:59');
  let last = Infinity;
  for (let n = 0; n < 60 * 24 * 60; n += 1) {
    const d = new Date(Date.UTC(2027, 0, 1) + n * 61000 * 7);
    if (d.getUTCFullYear() > 2027) break;
    const now = { year: 2027, month: d.getUTCMonth(), day: d.getUTCDate(), hour: d.getUTCHours(),
                  minute: d.getUTCMinutes(), second: d.getUTCSeconds() };
    const l = left(now, target);
    if (!l) break;
    const size = l.months * 1e9 + l.days * 86400 + l.hours * 3600 + l.minutes * 60 + l.seconds;
    assert.ok(size <= last || l.months < Math.floor(last / 1e9), `${JSON.stringify(now)} ${size} > ${last}`);
    last = size;
  }
});
check('the moment itself, and after, is null', () => {
  assert.strictEqual(left(at('2026-12-18'), at('2026-12-18')), null);
  assert.strictEqual(left(at('2026-12-19'), at('2026-12-18')), null);
});
check('a time of day is used; without one it is midnight', () => {
  is(left(at('2026-12-18 10:00'), targetParts({ day: '2026-12-18', clock: '19:30' })), 0, 0, 9, 30, 0);
  assert.strictEqual(left(at('2026-12-18 10:00'), targetParts({ day: '2026-12-18', clock: '' })), null);
});
check('days since: today, yesterday', () => {
  assert.strictEqual(daysSince(at('2026-12-18 23:00'), at('2026-12-18')), 0);
  assert.strictEqual(daysSince(at('2026-12-19 00:01'), at('2026-12-18 19:30')), 1);
});
check('coming first and soonest first; past last', () => {
  const list = [{ day: '2026-01-01', clock: '' }, { day: '2028-01-01', clock: '' }, { day: '2027-01-01', clock: '' }];
  assert.deepStrictEqual(inOrder(list, at('2026-11-22')).map((i) => i.day), ['2027-01-01', '2028-01-01', '2026-01-01']);
});
check('news ages', () => {
  const now = new Date('2026-11-22T12:00:00Z');
  assert.strictEqual(when('2026-11-22T11:57:00Z', now), '3 min ago');
  assert.strictEqual(when('2026-11-22T02:00:00Z', now), '10 h ago');
  assert.strictEqual(when('2026-11-21T12:00:00Z', now), 'yesterday');
  assert.strictEqual(when('not a date', now), '');
});
check('said aloud', () => {
  assert.strictEqual(spoken({ title: 'X' }, { months: 1, days: 2, hours: 3, minutes: 4, seconds: 5 }),
    'X: 1 month, 2 days, 3 hours left.');
  assert.strictEqual(spoken({ title: 'X' }, null), 'X: it is here.');
});
console.log(`${passed} passed`);
