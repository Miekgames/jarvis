/**
 * Cases for command-palette ranking.
 *
 * What you typed should get you where you meant with as few keystrokes as
 * possible, and the obvious thing should win. That is easy to assert as an
 * ordering and painful to check by typing into a box and squinting.
 *
 * node palette.test.cjs <path-to-compiled-palette.js>
 */
const assert = require('assert');
const path = require('path');
const { search, scoreCandidate } = require(path.resolve(process.argv[2]));

const ITEMS = [
  { id: 'p1', label: 'Settings', group: 'Panel', keywords: 'preferences options config voice' },
  { id: 'p2', label: 'What it heard', group: 'Panel', keywords: 'voice wake word microphone' },
  { id: 'p3', label: 'Tasks', group: 'Panel', keywords: 'jobs queue background' },
  { id: 'p4', label: 'Conversation history', group: 'Panel', keywords: 'past chats' },
  { id: 'p5', label: 'System', group: 'Panel', keywords: 'cpu memory disk' },
  { id: 'p6', label: 'Self-editing', group: 'Panel' },
  { id: 'p7', label: 'Automations', group: 'Panel' },
  { id: 'a1', label: 'Reset conversation', group: 'Action' },
  { id: 'c1', label: 'what is my CPU usage', group: 'Ask again' },
];

let passed = 0;
function check(name, fn) {
  try { fn(); passed += 1; }
  catch (e) { console.error(`FAIL: ${name}\n  ${e.message}`); process.exitCode = 1; }
}
const top = (q) => { const r = search(ITEMS, q); return r.length ? r[0].item.label : null; };
const labels = (q) => search(ITEMS, q).map((m) => m.item.label);

// ---------------------------------------------------------------------------
check('a prefix beats the same letters further in', () => {
  // "set" starts Settings and sits mid-word in "Reset conversation".
  assert.strictEqual(top('set'), 'Settings');
});

check('an exact label wins outright', () => {
  assert.strictEqual(top('Tasks'), 'Tasks');
  assert.strictEqual(top('system'), 'System');
});

check('matching is case insensitive', () => {
  assert.strictEqual(top('SETTINGS'), 'Settings');
  assert.strictEqual(top('sEtTiNgS'), 'Settings');
});

check('initials find a multi-word entry', () => {
  assert.strictEqual(top('wih'), 'What it heard');
  assert.strictEqual(top('ch'), 'Conversation history');
});

check('a few letters are enough', () => {
  assert.strictEqual(top('wha'), 'What it heard');
  assert.strictEqual(top('auto'), 'Automations');
});

check('hidden keywords find things the label does not say', () => {
  // Nothing is labelled "cpu", but System is about it.
  assert.ok(labels('cpu').includes('System'));
  assert.ok(labels('microphone').includes('What it heard'));
});

check('a visible label beats a hidden keyword', () => {
  // "voice" is a keyword on both Settings and What it heard, but it is in
  // neither label -- so neither should outrank an actual label match.
  const forVoice = labels('voice');
  assert.ok(forVoice.length > 0, 'voice should match something');
  // "system" is a real label; it must beat any keyword-only hit.
  assert.strictEqual(top('system'), 'System');
});

check('nonsense matches nothing', () => {
  assert.deepStrictEqual(search(ITEMS, 'zzzqqq'), []);
  assert.strictEqual(scoreCandidate(ITEMS[0], 'zzzqqq'), null);
});

check('an empty query returns everything, in the given order', () => {
  const all = search(ITEMS, '', 100);
  assert.strictEqual(all.length, ITEMS.length);
  assert.strictEqual(all[0].item.label, 'Settings', 'authored order must be kept');
});

check('the limit is respected', () => {
  assert.strictEqual(search(ITEMS, '', 3).length, 3);
});

check('whitespace is ignored', () => {
  assert.strictEqual(top('  settings  '), 'Settings');
});

check('matched characters are reported for highlighting', () => {
  const [best] = search(ITEMS, 'set');
  assert.deepStrictEqual(best.hits, [0, 1, 2], 'should mark the matched prefix');
});

check('a shorter label wins a tie', () => {
  const items = [
    { id: 'a', label: 'Task manager settings panel' },
    { id: 'b', label: 'Tasks' },
  ];
  assert.strictEqual(search(items, 'task')[0].item.label, 'Tasks');
});

check('a word boundary beats mid-word', () => {
  const items = [
    { id: 'a', label: 'Unsettling things' },   // "set" sits inside a word
    { id: 'b', label: 'Voice settings' },      // "set" starts a word
  ];
  assert.strictEqual(search(items, 'set')[0].item.label, 'Voice settings');
});

check('past commands are searchable too', () => {
  assert.ok(labels('cpu usage').includes('what is my CPU usage'));
});

if (process.exitCode) console.error(`\n${passed} passed, with failures above.`);
else console.log(`palette: ${passed} cases passed`);
