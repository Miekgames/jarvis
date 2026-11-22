/**
 * Wake-word matcher cases.
 *
 * Run through `python -m pytest tests/test_wakeword.py`, which compiles
 * wakeword.ts with the project's own TypeScript and executes this with node.
 * Kept as plain JS so it needs no test framework and no new dependency.
 *
 * Usage: node wakeword.test.cjs <path-to-compiled-wakeword.js>
 */
const { matchWakeWord, editDistance } = require(process.argv[2]);

let pass = 0;
const failures = [];

function check(name, got, want) {
  if (JSON.stringify(got) === JSON.stringify(want)) pass += 1;
  else failures.push(`${name}\n     got  ${JSON.stringify(got)}\n     want ${JSON.stringify(want)}`);
}

/** The wake word is present, and `rest` is what followed it. */
function hit(name, transcript, wake, rest) {
  const m = matchWakeWord(transcript, wake);
  check(name, { hit: m.hit, rest: m.rest }, { hit: true, rest });
}

/** The wake word is absent — this must not fire. */
function miss(name, transcript, wake) {
  check(name, matchWakeWord(transcript, wake).hit, false);
}

// -- the case that was broken: a bare wake word, said and then paused on ------
hit('bare wake word', 'Jarvis', 'jarvis', '');
hit('bare with full stop', 'Jarvis.', 'jarvis', '');
hit('bare with comma', 'jarvis,', 'jarvis', '');

// -- wake word and command in one utterance ----------------------------------
hit('wake + command', "jarvis what's my CPU doing", 'jarvis', 'what s my cpu doing');
hit('wake + short command', 'jarvis sleep', 'jarvis', 'sleep');
hit('comma separated', 'Jarvis, open Spotify', 'jarvis', 'open spotify');

// -- mishearings Chrome actually returns for "Jarvis" -------------------------
hit('travis', 'Travis what is the weather', 'jarvis', 'what is the weather');
hit('javis', 'javis sleep', 'jarvis', 'sleep');
hit('jervis', 'Jervis, run the tests', 'jarvis', 'run the tests');
hit('garvis', 'garvis', 'jarvis', '');
hit('jarviz', 'jarviz status', 'jarvis', 'status');

// -- the wake word need not start the sentence -------------------------------
hit('leading filler', 'um jarvis what time is it', 'jarvis', 'what time is it');
hit('mid sentence', 'so jarvis open the developer panel', 'jarvis', 'open the developer panel');

// -- multi-word wake words ----------------------------------------------------
hit('hey jarvis', 'hey jarvis turn on the lights', 'hey jarvis', 'turn on the lights');
hit('hey jarvis bare', 'Hey Jarvis', 'hey jarvis', '');
hit('hey + misheard', 'hey travis play music', 'hey jarvis', 'play music');
miss('leading word is required', 'jarvis play music', 'hey jarvis');

// -- must not fire ------------------------------------------------------------
miss('empty', '', 'jarvis');
miss('unrelated speech', 'what is the weather today', 'jarvis');
miss('similar english word', 'the harvest was good', 'jarvis');
miss('service', 'restart the service please', 'jarvis');
miss('too far away', 'marvellous', 'jarvis');

// -- custom wake words --------------------------------------------------------
hit('custom friday', 'friday what is my schedule', 'friday', 'what is my schedule');
hit('custom computer', 'computer status report', 'computer', 'status report');
hit('short word exact', 'max sleep', 'max', 'sleep');
miss('short words match strictly', 'man', 'max');

// -- edit distance ------------------------------------------------------------
check('distance identical', editDistance('jarvis', 'jarvis'), 0);
check('distance one substitution', editDistance('jervis', 'jarvis'), 1);
check('distance one deletion', editDistance('javis', 'jarvis'), 1);
check('distance is capped', editDistance('completely', 'different', 3), 4);

if (failures.length) {
  console.log(`${failures.length} FAILED:\n  - ${failures.join('\n  - ')}`);
  process.exit(1);
}
console.log(`${pass} wake-word cases passed`);
