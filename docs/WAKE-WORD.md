# Wake word

Say **"Hey Jarvis"** and it starts listening.

It runs entirely on this machine. No audio leaves the computer, nothing is
recorded, and it costs about 2.6% of one CPU core.

---

## Why it used to fail

Three separate causes, and the last one was the real killer.

### 1. A dictation engine was being used as a keyword spotter

The wake word used to run in the browser on the Web Speech API. That is a
general-purpose dictation engine: it uploads audio to Google, returns free
text, and stops itself after about a minute. Asked to transcribe "Jarvis" it
returns "Travis", "Javis", "Jervis" or "drives" — rarely the literal word.

Matching tolerantly enough to catch the real word also catches words that
aren't it. There is no threshold that works, because the engine was never
built for this job.

### 2. Two wake-word loops fighting over one microphone

React runs effects twice in development. Both runs awaited the microphone
check, both resolved, and both started a listening loop on the same shared
recognition object — where each restart aborted the other's session. The
result was a wake word that reported itself as running and never heard
anything.

### 3. The microphone was deaf

This is the one that mattered. On this machine **25 of 26 input devices return
digital silence, including the Windows default** — a virtual Oculus headset
device that opens perfectly and delivers nothing.

An open stream that yields no audio is indistinguishable from a quiet room.
Nothing in the stack noticed, so the wake word spent its life listening
attentively to a dead device, and every symptom — "it opens for a second then
goes back to standby", "it never responds" — followed from that.

---

## How it works now

[`backend/voice/hotword.py`](../backend/voice/hotword.py) runs
[openWakeWord](https://github.com/dscripka/openWakeWord)'s pretrained
`hey_jarvis` model through ONNX, on 80 ms frames of 16 kHz mono audio captured
straight from the device.

```
microphone --> 80ms frames --> melspectrogram --> embedding --> classifier
                                                                    |
                                            score >= threshold for 2 frames
                                                                    |
                                     jarvis.wakeword.detected on the event bus
                                                                    |
                                   websocket --> the page opens a command session
```

The page's only job is to react. The backend listens for *the word*; it never
listens for a command, so nothing is transcribed until you are actually
addressing it.

### Measured, not assumed

Scored against speech synthesised in two voices
(`tests/fixtures/wakeword/`, regenerate with
`scripts/make_wakeword_fixtures.ps1`):

| Phrase | Score |
| --- | --- |
| "Hey Jarvis" | 0.998 |
| "Hey Jarvis, what is my CPU usage?" | 0.998 |
| "Jarvis" (bare, no "hey") | 0.984 |
| "The service was marvellous, and Travis agreed." | 0.004 |
| "Hey Google, what is the weather?" | 0.000 |
| "What is my CPU usage?" | 0.000 |
| a long unrelated sentence | 0.000 |
| **"Hey, drive us to the shop."** | **0.992 / 0.076** |

That last row is the honest limitation. "Hey, drive us" is phonetically almost
"hey jarvis", and it fires in one synthetic voice while scoring 0.076 in the
other. Keyword spotters have near-homophones; pretending otherwise would be
the dishonest option. It is kept as a test fixture rather than deleted, the
threshold is a setting, and every detection is logged.

Throughput on this machine: **38x realtime**, so detection costs roughly 2.6%
of one core.

### The command after the wake word

Having heard the word on a device that demonstrably works, handing the
sentence after it to a different microphone would be perverse -- and that is
what the browser does: the Web Speech API records from whatever Chrome
considers the default input, which here is the silent one. A perfectly
detected wake word would still be followed by a deaf command.

So the detector keeps the same stream open and records the command itself:

1. **Speech boundary.** `webrtcvad` marks each 20 ms slice voiced or not.
   Recording ends after 1.1s of silence, or 14s, whichever comes first. A
   shorter pause does not end it -- people pause mid-sentence, and cutting
   them off there is what makes voice assistants infuriating.
2. **Nothing said?** If no speech follows within 3 seconds, or it is shorter
   than 0.3s, it says so and discards the audio rather than transcribing room
   noise and presenting whatever the model hallucinates from it.
3. **Transcription.** faster-whisper `base.en`, locally, around 4x realtime.
   The audio stays in memory; it is never written to disk.
4. **Execution.** The *backend* runs the command, not the page. One sentence
   produces one answer no matter how many tabs are open -- and it works with
   no page open at all. Because `chat()` publishes to the event bus, every
   open page still shows the whole exchange.

Measured end to end: `"Hey Jarvis, what is my CPU usage?"` -> detected at
0.98 -> 3.2s captured -> transcribed to `"What is my CPU usage?"` in about a
second -> `system_status` -> `"Your CPU usage is currently at 62.1%."`

Note that the wake word itself is not in the transcript: recording starts
after detection, so the command arrives clean.

Turn it off with `voice.local_command_capture` to hand the command back to
the browser. It switches itself off anyway if faster-whisper is missing,
rather than promising something it cannot do.

### The microphone button

The same reasoning applies to push-to-talk, so it takes the same path. The
button asks `POST /api/voice/listen`, the detector records on the stream it
already holds, and the command runs exactly as a spoken one does.

If local capture is unavailable it returns 409 and the page falls back to
browser speech recognition -- which works, as long as the input Chrome
prefers is one that carries audio.

### Choosing a microphone

Given what cause 3 turned out to be, device selection measures rather than
assumes. In order:

1. **A device already proven to carry audio.** Recorded in settings as
   `voice.wake_device_proven` the first time real sound arrives on it. Past
   evidence beats a fresh guess — a probe that lands during a quiet moment
   measures nothing at all.
2. **Whatever is carrying audio right now**, loudest first.
3. **A physical-looking device over a virtual one**, when nothing is making a
   sound. Names containing `virtual`, `steam streaming`, `stereo mix`,
   `sound mapper`, `voicemod` and friends are deprioritised.

The OS default only breaks ties. On the evidence here it is not a signal.

**Settings → Voice → Microphone → Test microphones** opens and measures every
input and shows which ones carry audio. That is the first thing to look at if
the wake word is not working.

### States it can be in

| State | Means |
| --- | --- |
| `off` | Not running. |
| `starting` | Choosing a device and loading the model (~2s). |
| `listening` | Running, and audio is arriving. |
| `recording` | Capturing a command after a detection or a button press. |
| `thinking` | Transcribing what it captured. |
| `silent` | Running, but the device has been flat for 20 seconds. |
| `failed` | It cannot run. The reason is in `detail`. |

`silent` is deliberately ambiguous, and the health check resolves it rather
than guessing: a device that has delivered sound this session and has gone
quiet is a quiet room and stays green; one that has *never* delivered
anything is deaf and is reported as degraded. Treating every quiet evening as
a fault is how a status light stops being read.

### When the microphone disappears

A USB microphone unplugged mid-session takes the capture stream with it. The
detector retries with a widening backoff (5s doubling to 60s, eight attempts),
re-probing the devices each time -- so plugging a different microphone in is
enough to bring it back. After that it stops and says so, rather than looping
in silence.

`silent` exists because that was the invisible failure. The live level meter
in Settings is the same idea: a bar that moves proves audio is arriving, and a
bar that never moves names the problem.

---

## Privacy and control

Continuous microphone capture is the most invasive thing in this application,
so:

- It is **off until you switch it on** (Settings → Voice → Wake word).
- It **stops immediately** when switched off, and while JARVIS is asleep.
- **No audio is written to disk**, ever. Nothing is kept beyond the 80 ms
  frame being scored.
- **No audio leaves the machine.** The model is local; there is no network
  call in the detection path.
- Every start, stop and detection goes to the **audit log**.
- While a command session has the microphone, the detector **stops scoring**,
  so the two never double-handle the same utterance.

---

## Settings

| Setting | Default | What it does |
| --- | --- | --- |
| `voice.wake_word_enabled` | off | Starts and stops the detector. |
| `voice.wake_device` | unset | Pin a specific input device index. |
| `voice.wake_device_proven` | learned | Set automatically once a device delivers real audio. |
| `voice.wake_threshold` | `0.5` | Raise it if you get false triggers; lower it if it misses you. |
| `voice.wake_model` | `hey_jarvis` | Also `alexa`, `hey_mycroft`, `hey_rhasspy`. |
| `voice.local_command_capture` | on | Capture and transcribe the command here rather than in the browser. |

## API

| Endpoint | Purpose |
| --- | --- |
| `GET /api/voice/hotword` | Current state, device, live level, detection count. |
| `POST /api/voice/hotword/start` | Start listening. 503 with a reason if it cannot. |
| `POST /api/voice/hotword/stop` | Stop capturing. |
| `GET /api/voice/devices` | Every input, opened and measured. |
| `POST /api/voice/listen` | Record a command now, as the wake word does. 409 if unavailable. |

## Fallback

If `openwakeword`, `onnxruntime` or `sounddevice` is missing, the old browser
loop runs instead and says so in the UI. It is strictly worse, for all the
reasons above, and the two never run at the same time. Install the real thing:

```bash
pip install openwakeword onnxruntime sounddevice
```

The models download on first use.

---

## Speaking

### One voice, one tab

With several JARVIS tabs open, every one of them used to answer out loud. The
effect was a room full of assistants replying at once, in voices that did not
even match -- a tab using the server voice sounds British, one that had fallen
back to the browser sounds like whatever Windows offers.

Three things fixed it:

1. **One tab owns the speakers.** A lease in `localStorage`, renewed by the
   holder and taken over if it stops being renewed. No coordinator, and the
   failure mode is "someone else speaks a second later" rather than silence.
2. **No line is said twice.** Tabs announce each line over a `BroadcastChannel`
   as they start it and skip anything another tab has just announced. This
   covers what a lease cannot: two tabs claiming in the same instant, or one
   opened while another is mid-reply.
3. **One gate.** Three separate code paths could start speech -- a new
   utterance, the broadcast of the same message, and the automation channel --
   and only one of them checked the lease. They all go through a single
   function now.

### Interrupting a reply

Say the wake word over the top of JARVIS speaking and it stops and listens.

The detector used to go completely deaf while a reply played -- it stopped
scoring for the duration -- so you had to wait for a sentence to finish
before it would hear you at all. Telling "the user spoke over it" from
"JARVIS heard its own voice" needs no echo cancellation, because the backend
knows exactly what is coming out of the speakers:

* **If the reply does not contain the name**, a wake word arriving at the
  microphone did not come from the speakers. It came from the room.
* **The bar is raised, not lowered.** Speech competing with playback is a
  harder signal, so an interruption needs 0.7 rather than the usual 0.5. The
  conservative direction is deliberate: a missed interruption costs a few
  seconds of waiting, a false one cuts JARVIS off mid-answer to listen to
  nothing.

Pressing the microphone button while it talks means the same thing, and
takes the same path.

#### Not losing your first word

The spotter can only recognise the wake word once it has heard all of it, and
its score stays high for several frames after, so by the time a detection
fires you are already into the next word. "Hey Jarvis, what is my CPU usage?"
was being recorded as "My CPU usage."

A second of audio is therefore kept at all times and prepended to the
recording. That puts the wake word itself in the capture, so the transcript
is trimmed to whatever follows the name -- including common mishearings like
"Travis", which otherwise reach the model as part of the command.

The pre-roll is **not** used when interrupting, and that was measured rather
than assumed: during a barge-in that second of audio has the reply at full
volume on top of your voice, and seeding from it made the transcript worse
-- "what is my CPU usage" came back as "to my CPU usage". An interruption
starts clean once the reply has stopped, which is also how people actually
interrupt: say the name, wait for it to stop, then speak.

### One voice, consistently

The browser voice is resolved once and kept. It used to be picked per
utterance from `/daniel|google uk english male|male/i` over whatever
`getVoices()` returned at that moment, which had three problems: the list is
empty until the browser finishes loading it, `/male/i` also matches "female",
and the configured name is an edge-tts name like `en-GB-RyanNeural` that no
browser voice is ever called -- so the lookup always missed.

Now it resolves in a defined order (exact name, same locale and apparent
gender, then sensible fallbacks), caches the result, and re-resolves only when
the browser reports its voice list has changed.

**Settings -> Voice -> Voice in use** names the voice actually speaking, says
whether this tab is the one speaking, and -- if the server voice has failed --
says so, because that is an audible change and "it suddenly sounded different"
deserves an answer.
