"""Building one real file, and proving that each version of it runs.

The first unattended runs reported work that had not happened. "Alarm
feature -- finished", "24-hour option -- finished", nineteen lines like it,
beside a clock.html that was still the same 441 bytes it had been after the
first job. Nothing was lying on purpose. The loop asked a small model to
edit files through tool calls and then asked the same model whether it had,
and a model that has just rewritten a file in roughly its old shape will say
yes.

So this turns the arrangement round. **The program owns the file; the model
only transforms it.**

  * The model is never asked to open, write or name anything. It is handed
    the current file in full and asked for a complete replacement.
  * It is never asked whether it succeeded. The replacement is measured:
    is it a whole document, did it quietly drop half the app, did it change
    anything at all, does it reach for the network.
  * Then it is opened in a real browser with the network cut, and every
    button on the page is pressed once. If anything throws, that version is
    discarded and the previous one stays exactly where it was.

The file on disk is therefore always the last version that passed. A run can
be interrupted at any moment, or have every later change rejected, and what
is left is still an app that opens -- and the report is assembled from the
changes that were kept, not from what anything claimed.
"""
from __future__ import annotations

import asyncio
import difflib
import gc
import logging
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple

from .session import HIDDEN

logger = logging.getLogger("jarvis.autonomy.builder")

# The screenshot of the current version, beside the app itself.
PREVIEW = "preview.png"

# Characters per token, for sizing against the context window.
#
# Measured on this machine, qwen2.5 writing an indented page came out at 4.4.
# The figure used is deliberately lower: estimating high here would overfill
# the window, and an overfull window does not fail -- it silently forgets
# the top of the file being edited.
CHARS_PER_TOKEN = 3.6
DEFAULT_NUM_CTX = 16384

# A replacement smaller than this fraction of what it replaces has lost
# content, whatever else it has gained.
SHRINK_LIMIT = 0.6

# How far past the size ceiling a change may land and still be kept. Past
# the ceiling nothing further is added, so a small overshoot costs nothing.
OVERSHOOT = 1.15

FIRST_ATTEMPTS = 3
FIRST_MAX_TOKENS = 6000
BUILD_TEMPERATURE = 0.25

# A feature that adds fewer lines than this has not added a feature.
MIN_FEATURE_LINES = 3

# Browser check.
MAX_CLICKS = 25
SMOKE_TIMEOUT_S = 60.0
SETTLE_MS = 1200

# Until a run has timed a few of its own steps, assume this long for one.
DEFAULT_STEP_SECONDS = 120.0


@dataclass(frozen=True)
class Target:
    """The one file being built."""

    filename: str
    kind: str                     # html | python | text

    @property
    def language(self) -> str:
        return {"html": "html", "python": "python"}.get(self.kind, "")

    @property
    def suffix(self) -> str:
        return Path(self.filename).suffix or ".txt"


@dataclass
class Smoke:
    """What happened when a version was opened in a browser."""

    tested: bool = False          # False: could not be checked at all
    ok: bool = True
    errors: List[str] = field(default_factory=list)
    detail: str = ""
    # The labels of what is on the page: buttons, fields, headings. Compared
    # between versions, it is the evidence that a change put something there.
    inventory: List[str] = field(default_factory=list)
    # The subset of those that can be pressed or typed into. Compared the
    # other way, it is the evidence that a change took something away.
    controls: List[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return self.tested and not self.ok

    def summary(self) -> str:
        if not self.errors:
            return self.detail
        more = len(self.errors) - 1
        return self.errors[0] + (f" (and {more} more)" if more > 0 else "")


@dataclass
class Trial:
    """What happened when the app was used the way a person would use it.

    Not "did it throw" -- that is the smoke test. This types into the
    fields, presses the buttons, reloads the page, and writes down what was
    on the screen after each. The first app built this way passed every
    check and, when actually used, lost the habit you had just added the
    moment the page was reloaded.
    """

    ran: bool = False
    lines: List[str] = field(default_factory=list)
    # What was observed that is wrong whenever it is seen: a stable key (so
    # the same problem can be recognised in the next version) and a plain
    # sentence. Kept short on purpose -- these are repeated to the owner as
    # fact, so nothing goes in that is only probably a fault.
    problems: dict = field(default_factory=dict)
    # Whether something typed in and added was still there after a reload.
    # None when nothing was added, so there was nothing to lose.
    saved: Optional[bool] = None
    detail: str = ""

    def account(self) -> str:
        text = ("A tester has just used this app in a real browser. This is "
                "exactly what happened:\n"
                + "\n".join(f"- {line}" for line in self.lines))
        if self.problems:
            text += "\n\nProblems seen:\n" + "\n".join(
                f"{number}. {sentence[0].upper()}{sentence[1:]}."
                for number, sentence in enumerate(self.problems.values(), 1))
        return text


@dataclass
class Outcome:
    """What became of one reply from the model."""

    kept: bool
    sentence: str
    plus: int = 0
    minus: int = 0
    added: List[str] = field(default_factory=list)
    fixed: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# deciding what is being built
# ---------------------------------------------------------------------------
# A bare filename only: no separators, so text from a prompt can never name
# a path outside the workspace.
_FILENAME = re.compile(
    r"(?<![\w/\\.-])([A-Za-z0-9][\w-]{0,40}\.(?:html?|py|js|css))\b",
    re.IGNORECASE)

_VERB = (r"(?:build|make|create|write|design|code|develop|generate|"
         r"put together|whip up)")
_NOUN = (r"(?:app|application|page|webpage|website|site|game|tool|dashboard|"
         r"calculator|clock|timer|stopwatch|tracker|editor|player|viewer|"
         r"widget|quiz|planner|converter|generator|visuali[sz]er|simulator|"
         r"script|program|html)")
_BUILD = re.compile(
    rf"\b{_VERB}\b(?P<between>(?:\s+[\w'-]+){{0,6}}?)\s+{_NOUN}s?\b",
    re.IGNORECASE)
_ANY_VERB = re.compile(rf"\b{_VERB}\b", re.IGNORECASE)

# "Write a report about the best note apps" has a build verb and an app noun
# and is not a request to build anything.
_DOCUMENT_WORDS = frozenset({
    "report", "summary", "essay", "email", "letter", "list", "review",
    "article", "notes", "note", "plan", "document", "guide", "comparison",
    "overview", "description", "explanation",
})

_PYTHON_HINT = re.compile(r"\b(?:python|script)\b", re.IGNORECASE)


def wants_a_build(prompt: str) -> bool:
    """Whether a prompt is asking for something to be made, as one file.

    A filename alone is not enough: "summarise report.txt" names a file and
    wants it read, and treating that as a build would write a new report.txt
    into the workspace and call it done.
    """
    text = prompt or ""
    if not _ANY_VERB.search(text):
        return False
    if _FILENAME.search(text):
        return True
    found = _BUILD.search(text)
    if not found:
        return False
    between = found.group("between").lower().split()
    return not any(word in _DOCUMENT_WORDS for word in between)


def choose_target(prompt: str) -> Target:
    """The file to build: the one the prompt names, or a page by default."""
    text = prompt or ""
    found = _FILENAME.search(text)
    if found:
        name = found.group(1)
        extension = name.rsplit(".", 1)[1].lower()
        if extension in ("html", "htm"):
            return Target(name, "html")
        if extension == "py":
            return Target(name, "python")
        return Target(name, "text")
    if _PYTHON_HINT.search(text) and not re.search(r"\bhtml\b", text, re.I):
        return Target("main.py", "python")
    return Target("index.html", "html")


def max_app_bytes(num_ctx: int) -> int:
    """How large a file this model can be trusted to rewrite whole.

    Each step sends the file and receives it back, so both copies and the
    instructions have to fit the context window at once. Run past that and
    the model does not report a problem: the window slides, the top of the
    file falls out of view, and what comes back is a confident rewrite of
    the half it could still see.
    """
    usable = max(2048, int(num_ctx * 0.95) - 850)
    return int(usable / 2.25 * CHARS_PER_TOKEN)


# ---------------------------------------------------------------------------
# reading what the model sent back
# ---------------------------------------------------------------------------
_FENCE = re.compile(r"```[\w+-]*[ \t]*\n(.*?)```", re.DOTALL)


def extract_document(reply: str, kind: str) -> Optional[str]:
    """The file inside a reply, or None if there is not a whole one.

    A page is taken from its doctype to its closing tag and anything outside
    that -- a preamble, a fence, a closing remark -- is discarded. A reply
    with no closing tag was cut off part-way, and half a file is not kept.
    """
    text = (reply or "").strip()
    if not text:
        return None

    if kind == "html":
        lowered = text.lower()
        end = lowered.rfind("</html>")
        if end == -1:
            return None
        # The last complete document, if it wrote the page out twice.
        start = lowered.rfind("<!doctype", 0, end)
        if start == -1:
            start = lowered.rfind("<html", 0, end)
        if start == -1:
            return None
        return text[start:end + len("</html>")].strip() + "\n"

    fenced = _FENCE.search(text)
    if fenced:
        return fenced.group(1).strip() + "\n"
    if "```" in text:
        return None               # opened a fence and never closed it
    return text + "\n"


# The classic failure of asking for a whole file back: a comment standing in
# for the part the model did not feel like writing out again.
_ELISION = re.compile(
    r"(?:rest of (?:the|your) (?:code|file|script|styles?|css|html|functions?|content)"
    r"|(?:existing|previous|remaining|other) (?:code|styles?|css|functions?|script|html|content|features)"
    r" (?:here|goes here|remains?|stays?|unchanged|as before|omitted)"
    r"|(?:remains?|stays?) (?:the same|unchanged)"
    r"|same as before|omitted for brevity"
    r"|(?://|<!--|/\*|#)\s*\.\.\.\s*(?:-->|\*/)?\s*$)",
    re.IGNORECASE | re.MULTILINE)

_URL = re.compile(r"""(?:https?:)?//[A-Za-z0-9][^\s"'<>)`]*""")
# XML namespaces look like addresses and load nothing.
_HARMLESS_URLS = ("http://www.w3.org/", "//www.w3.org/")


def outside_addresses(document: str) -> List[str]:
    """Network addresses a file refers to.

    The browser check blocks the network while it watches, but it only
    watches for a couple of seconds. Refusing any outside address in the
    text is what makes "self-contained" a property of the file rather than
    of the moment it was tested.
    """
    found = []
    for match in _URL.finditer(document):
        url = match.group(0)
        # "//" also opens a JavaScript comment; an address has a dot in it.
        host = url.split("//", 1)[1].split("/", 1)[0]
        if "." not in host:
            continue
        if any(marker in url for marker in _HARMLESS_URLS):
            continue
        if url not in found:
            found.append(url)
    return found


# Browser features that exist on a desktop and not where the owner will
# actually open the app: a phone, over the tailnet, on plain HTTP. They are
# only switched on for pages served securely, so a button that calls one
# works in every test and throws in the owner's hand.
#
# Each is (what marks a use, what marks a check for it, the sentence).
_NOT_ON_A_PHONE = (
    (re.compile(r"\bnew\s+Notification\s*\(|\bNotification\s*\.\s*(?:requestPermission|permission)\b"),
     re.compile(r"""["']Notification["']\s+in\s+window|window\s*\.\s*Notification|typeof\s+Notification"""),
     "it uses the browser's Notification feature without checking that it "
     "exists, and it does not on a phone over plain HTTP; show the message "
     "in a banner on the page instead"),
    (re.compile(r"\bnavigator\s*\.\s*clipboard\s*\.\s*\w+"),
     re.compile(r"""navigator\s*\.\s*clipboard\s*(?:&&|\?\.|\)|\s*\|\|)|isSecureContext|["']clipboard["']\s+in\s+navigator|!\s*navigator\s*\.\s*clipboard"""),
     "it copies with navigator.clipboard without checking that it exists, "
     "and it does not on a phone over plain HTTP; fall back to selecting "
     "the text and document.execCommand('copy')"),
)


def unavailable_features(document: str) -> Optional[str]:
    """A browser feature this page leans on that a phone will not have."""
    for used, checked, sentence in _NOT_ON_A_PHONE:
        if used.search(document) and not checked.search(document):
            return sentence
    return None


def diff_stats(before: str, after: str) -> Tuple[int, int]:
    """Lines added and removed between two versions."""
    old, new = before.splitlines(), after.splitlines()
    added = removed = 0
    matcher = difflib.SequenceMatcher(None, old, new, autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "delete"):
            removed += i2 - i1
        if tag in ("replace", "insert"):
            added += j2 - j1
    return added, removed


def judge(candidate: str, previous: str, kind: str, max_bytes: int,
          feature: bool = True) -> Optional[str]:
    """Why a replacement cannot be kept, or None if it can go on to testing.

    Every check here is a way this has actually gone wrong, phrased as the
    sentence that ends up in the report -- because the owner reads these.
    """
    size = len(candidate.encode("utf-8"))

    if kind == "html":
        if "<body" not in candidate.lower() or size < 200:
            return "it was not a complete page"
        outside = outside_addresses(candidate)
        if outside:
            return (f"it refers to an outside address ({outside[0][:60]}), and "
                    "this has to work offline with nothing loaded from the network")
        unavailable = unavailable_features(candidate)
        if unavailable:
            return unavailable

    if _ELISION.search(candidate):
        return ("it left part of the file out behind a placeholder comment "
                "instead of writing it")

    if kind == "python":
        try:
            compile(candidate, "<candidate>", "exec")
        except SyntaxError as exc:
            return f"it has a syntax error on line {exc.lineno}: {exc.msg}"

    if size > max_bytes * OVERSHOOT:
        return (f"it grew to {size / 1000:.1f} KB, past what this model can "
                "safely edit in one piece")

    if not previous:
        return None

    if " ".join(candidate.split()) == " ".join(previous.split()):
        return "it returned the file unchanged"

    if size < len(previous.encode("utf-8")) * SHRINK_LIMIT:
        return (f"it dropped most of the existing app "
                f"({len(previous.splitlines())} lines down to "
                f"{len(candidate.splitlines())})")

    added, removed = diff_stats(previous, candidate)
    if feature and added < MIN_FEATURE_LINES:
        return (f"it barely changed the file (+{added} lines), which is not "
                "this feature")

    return None


# ---------------------------------------------------------------------------
# the browser check
# ---------------------------------------------------------------------------
# Console errors that mean the page itself is broken. An app that logs
# "please enter a task" when its Add button is pressed on an empty field is
# doing its job, and is not failed for it; uncaught exceptions arrive
# separately and always count.
_CONSOLE_FAILURES = ("failed to load resource", "refused to", "uncaught",
                     "is not defined", "is not a function",
                     "cannot read properties", "unexpected token")

_INVENTORY_JS = """() => {
  const seen = new Set(); const out = [];
  const add = (text) => {
    text = (text || '').replace(/\\s+/g, ' ').trim();
    if (!text || text.length > 40) return;
    const key = text.toLowerCase();
    if (seen.has(key)) return;
    seen.add(key); out.push(text);
  };
  document.querySelectorAll(
      'button, [role=button], input[type=button], input[type=submit], [onclick]')
    .forEach((e) => add(e.innerText || e.value
                        || e.getAttribute('aria-label') || e.title));
  document.querySelectorAll(
      'input:not([type=button]):not([type=submit]):not([type=hidden]), select, textarea')
    .forEach((e) => add(e.getAttribute('aria-label') || e.placeholder
                        || (e.labels && e.labels[0] && e.labels[0].innerText)
                        || e.name || e.id));
  const controls = out.length;
  document.querySelectorAll('h1, h2, h3, label, summary, legend, th')
    .forEach((e) => add(e.innerText));
  return {all: out.slice(0, 120), controls: out.slice(0, Math.min(controls, 120))};
}"""

# Run before the page's own script, to make the test browser as limited as
# the least capable place the app will be opened. Without this the check
# passes on abilities the owner's phone does not have.
_AS_ON_A_PHONE = """
try { Object.defineProperty(Navigator.prototype, 'clipboard',
        { get: () => undefined, configurable: true }); } catch (e) {}
try { delete window.Notification; } catch (e) {}
try { Object.defineProperty(Crypto.prototype, 'randomUUID',
        { value: undefined, configurable: true, writable: true }); } catch (e) {}
"""

# Counts sounds, so that a button whose whole job is to beep is not written
# down as a button that does nothing.
_COUNT_SOUND = """
window.__jarvisSound = 0;
for (const name of ['AudioContext', 'webkitAudioContext']) {
  const Real = window[name];
  if (!Real) continue;
  const Counted = function (...args) { window.__jarvisSound += 1; return new Real(...args); };
  Counted.prototype = Real.prototype;
  try { window[name] = Counted; } catch (e) {}
}
"""

# Put something in every empty field, the way a person trying the app would.
# The text is a made-up word so that it can be looked for afterwards.
_FILL_JS = """() => {
  const filled = []; let n = 0;
  for (const el of document.querySelectorAll('input, textarea, select')) {
    if (el.disabled || el.readOnly) continue;
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') continue;
    const tag = el.tagName;
    const type = (el.getAttribute('type') || 'text').toLowerCase();
    const label = (el.getAttribute('aria-label') || el.placeholder
        || (el.labels && el.labels[0] && el.labels[0].innerText)
        || el.name || el.id || type).replace(/\\s+/g, ' ').trim().slice(0, 40);
    let value = null;
    if (tag === 'SELECT') {
      if (el.options.length > 1 && el.selectedIndex < 1) {
        el.selectedIndex = 1; value = el.options[1].text;
      }
    } else if (tag === 'TEXTAREA'
               || ['text', 'search', 'email', 'url', 'tel'].includes(type)) {
      if (!el.value) { value = 'Zebra' + (41 + n++); el.value = value; }
    } else if (type === 'number') {
      if (!el.value) { value = '3'; el.value = value; }
    } else if (type === 'date') {
      if (!el.value) { value = new Date().toISOString().slice(0, 10); el.value = value; }
    } else if (type === 'time') {
      if (!el.value) { value = '09:30'; el.value = value; }
    }
    if (value !== null) {
      el.dispatchEvent(new Event('input', { bubbles: true }));
      el.dispatchEvent(new Event('change', { bubbles: true }));
      filled.push([label, value]);
    }
  }
  return filled;
}"""

_STATE_JS = """() => {
  const html = document.body ? document.body.innerHTML : '';
  let hash = 0;
  for (let i = 0; i < html.length; i++) { hash = (hash * 31 + html.charCodeAt(i)) | 0; }
  let store = '';
  try { store = JSON.stringify(localStorage); } catch (e) {}
  return { text: document.body ? document.body.innerText : '',
           html: html.length + ':' + hash, store: store.length + ':' + store.slice(0, 2000),
           sound: window.__jarvisSound || 0 };
}"""

# Everything on the page a person would take for a button.
#
# Not only <button>. The third real build made its main action a
# <div onclick="addHabit()">, and a tester that only looked for <button>
# pressed the theme switch, added nothing, and reported that nothing had
# gone wrong -- which was true, and worthless. So: real buttons, anything
# with a click handler in the markup, and anything styled to look pressable
# (a pointer cursor) that is not merely a wrapper around one of those.
_PRESSABLE_JS = """() => {
  const found = [];
  const taken = new Set();
  const usable = (el) => {
    const style = getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden') return false;
    const box = el.getBoundingClientRect();
    return box.width >= 2 && box.height >= 2;
  };
  const take = (el) => { if (!taken.has(el) && usable(el)) { taken.add(el); found.push(el); } };
  document.querySelectorAll(
      'button, input[type=button], input[type=submit], [role=button], [onclick]')
    .forEach(take);
  document.querySelectorAll('div, span, a, li, label, td').forEach((el) => {
    if (taken.has(el) || getComputedStyle(el).cursor !== 'pointer') return;
    if (el.querySelector('button, input, select, textarea, a, [onclick]')) return;
    if (el.closest('button, [onclick], [role=button], label[for]')) return;
    for (const other of taken) { if (other.contains(el)) return; }
    take(el);
  });
  return found.slice(0, 60);
}"""

_LABEL_JS = ("(el) => (el.innerText || el.value || el.getAttribute('aria-label')"
             " || el.title || '')")


async def _pressables(page) -> List[Tuple[Any, str]]:
    """The things on a page that can be pressed, with what each one says."""
    found: List[Tuple[Any, str]] = []
    try:
        listing = await page.evaluate_handle(_PRESSABLE_JS)
        properties = await listing.get_properties()
    except Exception:
        return found
    for value in properties.values():
        element = value.as_element()
        if element is None:
            continue
        try:
            label = await element.evaluate(_LABEL_JS)
        except Exception:
            continue
        label = " ".join(str(label or "").split())[:40]
        if label:
            found.append((element, label))
    return found
_DESTRUCTIVE = re.compile(r"delete|remove|clear|reset|erase|wipe|undo", re.I)
_ADDS = re.compile(r"add|save|create|new|submit|log|record|start", re.I)
TRIAL_TIMEOUT_S = 90.0

_ID = re.compile(r"""\bid\s*=\s*["']([^"']+)["']""")


def element_ids(document: str) -> set:
    """The ids a page gives its elements, read from its source."""
    return set(_ID.findall(document or ""))


def _same_label(label: str) -> str:
    """A label with its numbers blanked, so "25:00" matches "24:59"."""
    return re.sub(r"\d+", "#", label.lower()).strip()


async def _smoke(path: Path, shot: Optional[Path]) -> Smoke:
    from playwright.async_api import async_playwright

    errors: List[str] = []
    inventory: List[str] = []
    controls: List[str] = []
    stage = {"pressing": False}

    def note(message: Any) -> None:
        text = " ".join(str(message).split())[:220]
        if not text:
            return
        if stage["pressing"]:
            text = "after pressing its buttons: " + text
        if text not in errors:
            errors.append(text)

    def on_console(message) -> None:
        if message.type != "error":
            return
        text = message.text or ""
        if any(marker in text.lower() for marker in _CONSOLE_FAILURES):
            note(text)

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800})
            await context.add_init_script(_AS_ON_A_PHONE)

            async def gate(route) -> None:
                url = route.request.url
                if url.startswith(("file:", "data:", "blob:", "about:")):
                    await route.continue_()
                else:
                    note(f"it tried to load {url[:90]} from the network")
                    await route.abort()

            await context.route("**/*", gate)
            page = await context.new_page()
            page.on("pageerror", lambda error: note(error))
            page.on("console", on_console)

            # Answer its own dialogs, or the first alert() ends the check.
            # "5" because it is acceptable both as a name and as a number.
            async def answer(dialog) -> None:
                try:
                    if dialog.type == "prompt":
                        await dialog.accept("5")
                    else:
                        await dialog.accept()
                except Exception:
                    pass

            page.on("dialog", lambda dialog: asyncio.ensure_future(answer(dialog)))

            # From here on, a failure is the page's failure and counts as
            # one. Only being unable to start a browser at all is "untested".
            try:
                await page.goto(path.as_uri(), wait_until="load", timeout=15000)
                await page.wait_for_timeout(SETTLE_MS)

                # Text, or something drawn or usable. Counting elements
                # alone would pass a body holding nothing but its own
                # <script> tag, which is exactly what a page looks like
                # when the script that was meant to fill it failed.
                rendered = await asyncio.wait_for(page.evaluate(
                    "document.body ? document.body.innerText.trim().length"
                    " + document.body.querySelectorAll('canvas, svg, img,"
                    " video, button, input, select, textarea').length : 0"), 10)
                if not rendered:
                    note("the page rendered nothing at all")

                try:
                    found = await asyncio.wait_for(
                        page.evaluate(_INVENTORY_JS), 10)
                    inventory = list(found.get("all") or [])
                    controls = list(found.get("controls") or [])
                except Exception:
                    inventory, controls = [], []

                # Photographed before anything is pressed, so the preview
                # shows the app as it opens rather than in whatever state
                # the button mashing below leaves it.
                if shot is not None:
                    try:
                        await page.screenshot(path=str(shot), timeout=10000)
                    except Exception:
                        logger.debug("screenshot failed", exc_info=True)

                # Press everything once. Most broken features are a handler
                # that calls something which does not exist, and that only
                # shows when the button is actually used.
                stage["pressing"] = True
                for handle, _ in (await _pressables(page))[:MAX_CLICKS]:
                    try:
                        await handle.click(timeout=350, no_wait_after=True)
                    except Exception:
                        continue  # hidden or covered: not this check's concern
                await page.wait_for_timeout(300)

                # And once more with something typed in. Pressed on empty
                # fields, most handlers return at their first line; the
                # code that can actually be wrong only runs with input.
                # Looked up afresh, because the first round may have put
                # new buttons on the page.
                try:
                    await asyncio.wait_for(page.evaluate(_FILL_JS), 8)
                except Exception:
                    pass
                for handle, _ in (await _pressables(page))[:MAX_CLICKS]:
                    try:
                        await handle.click(timeout=350, no_wait_after=True)
                    except Exception:
                        continue
                await page.wait_for_timeout(400)
            except asyncio.TimeoutError:
                note("the page stopped responding, which usually means a "
                     "loop that never ends")
            except Exception as exc:
                text = str(exc).splitlines()[0][:160] if str(exc) else type(exc).__name__
                if "Timeout" in text:
                    note("the page never finished loading, which usually "
                         "means a loop that never ends")
                else:
                    note(f"the browser could not open it: {text}")
        finally:
            try:
                await browser.close()
            except Exception:
                pass

    return Smoke(tested=True, ok=not errors, errors=errors, inventory=inventory,
                 controls=controls,
                 detail="opened and clicked through in a browser without errors"
                 if not errors else "")


def _on_fresh_loop(make: Callable[[], Any], timeout: float) -> Any:
    """Run one browser job on its own event loop, in its own thread.

    Playwright needs subprocess support, which on Windows means a proactor
    loop. Whether the server's loop is one depends on how it was started, so
    this does not ask -- it brings its own.

    The time limit is applied in here rather than by whoever is waiting,
    because a thread cannot be cancelled from outside: a limit enforced on
    this loop unwinds through the `finally` that closes the browser, and one
    enforced by the caller would walk away and leave Chromium running.
    """
    if sys.platform == "win32":
        loop = asyncio.ProactorEventLoop()
    else:
        loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(asyncio.wait_for(make(), timeout))
    finally:
        try:
            # The pipes to the browser process close themselves when they
            # are collected, and to do it they need the loop. Left to be
            # collected after it has gone, each one writes "Event loop is
            # closed" into the log -- harmless, and several lines of noise
            # per change for a whole day. So: collect them now, and give
            # the loop a moment to finish what that starts.
            gc.collect()
            loop.run_until_complete(asyncio.sleep(0.25))
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        loop.close()


def _in_fresh_loop(path: Path, shot: Optional[Path]) -> Smoke:
    try:
        return _on_fresh_loop(lambda: _smoke(path, shot), SMOKE_TIMEOUT_S)
    except asyncio.TimeoutError:
        return Smoke(tested=True, ok=False,
                     errors=["the page hung the browser for a minute"])


async def smoke_test(path: Path, shot: Optional[Path] = None) -> Smoke:
    """Open a page in a real browser, offline, and press its buttons.

    Returns `tested=False` when the check could not be run at all. That is
    reported as "not browser-tested" and is never quietly treated as a pass.
    """
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return Smoke(tested=False, detail="Playwright is not installed")

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_in_fresh_loop, path, shot),
            timeout=SMOKE_TIMEOUT_S + 20)
    except asyncio.TimeoutError:
        return Smoke(tested=False, detail="the browser check timed out")
    except Exception as exc:
        return Smoke(tested=False,
                     detail=f"the browser check could not run ({str(exc)[:120]})")


# ---------------------------------------------------------------------------
# using it
# ---------------------------------------------------------------------------
def _quote(text: str, limit: int) -> str:
    flat = " ".join((text or "").split())
    if len(flat) > limit:
        flat = flat[:limit - 1].rstrip() + "…"
    return f"“{flat}”" if flat else "nothing"


def _what_changed(before: dict, after: dict, marks: dict, counts: dict) -> str:
    """One press of one button, as a person watching would describe it."""
    old = [line.strip() for line in before["text"].splitlines() if line.strip()]
    new = [line.strip() for line in after["text"].splitlines() if line.strip()]
    appeared = [line for line in new if line not in old]
    gone = [line for line in old if line not in new]

    parts = []
    if appeared:
        parts.append("the page now also shows " + _quote(" / ".join(appeared), 150))
    if gone:
        parts.append("it no longer shows " + _quote(" / ".join(gone), 110))
    if not appeared and not gone and before["html"] != after["html"]:
        parts.append("something on the page changed, but none of its text")
    if counts["dialog"] > marks["dialog"]:
        said = counts.get("said") or ""
        parts.append("a message box appeared"
                     + (" saying " + _quote(said, 90) if said else ""))
    if counts["download"] > marks["download"]:
        parts.append("a file was downloaded")
    if after["sound"] > before["sound"]:
        parts.append("a sound played")
    if not parts and before["store"] != after["store"]:
        parts.append("something was saved, but nothing on the page changed")
    return "; ".join(parts)


async def _trial(path: Path) -> Trial:
    from playwright.async_api import async_playwright

    lines: List[str] = []
    problems: dict = {}
    errors: List[str] = []
    counts = {"dialog": 0, "download": 0}
    saved: Optional[bool] = None

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 800})
            await context.add_init_script(_AS_ON_A_PHONE + _COUNT_SOUND)

            async def gate(route) -> None:
                if route.request.url.startswith(("file:", "data:", "blob:", "about:")):
                    await route.continue_()
                else:
                    await route.abort()

            await context.route("**/*", gate)
            page = await context.new_page()
            page.on("pageerror", lambda error: errors.append(
                " ".join(str(error).split())[:160]))

            async def answer(dialog) -> None:
                counts["dialog"] += 1
                counts["said"] = dialog.message or ""
                try:
                    if dialog.type == "prompt":
                        await dialog.accept("Zebra40")
                    else:
                        await dialog.accept()
                except Exception:
                    pass

            page.on("dialog", lambda dialog: asyncio.ensure_future(answer(dialog)))
            page.on("download", lambda _: counts.__setitem__(
                "download", counts["download"] + 1))

            async def state() -> dict:
                return await asyncio.wait_for(page.evaluate(_STATE_JS), 8)

            async def buttons() -> List[Tuple[Any, str]]:
                return await _pressables(page)

            await page.goto(path.as_uri(), wait_until="load", timeout=15000)
            await page.wait_for_timeout(900)
            opened = await state()
            lines.append("Opened it. The page showed " + _quote(opened["text"], 240) + ".")

            filled = await asyncio.wait_for(page.evaluate(_FILL_JS), 8)
            if filled:
                lines.append("Typed " + "; ".join(
                    f"“{value}” into “{label}”" for label, value in filled[:6]) + ".")
            tokens = [value for _, value in filled if str(value).startswith("Zebra")]

            async def press(label: str) -> None:
                try:
                    more = await asyncio.wait_for(page.evaluate(_FILL_JS), 8)
                    tokens.extend(value for _, value in more
                                  if str(value).startswith("Zebra"))
                    before, marks = await state(), dict(counts)
                    target = next((handle for handle, name in await buttons()
                                   if name == label), None)
                    if target is None:
                        return
                    await target.click(timeout=600, no_wait_after=True)
                except Exception:
                    return
                await page.wait_for_timeout(350)
                after = await state()
                happened = _what_changed(before, after, marks, counts)
                if not happened:
                    # A timer's first tick is a second away.
                    await page.wait_for_timeout(900)
                    after = await state()
                    happened = _what_changed(before, after, marks, counts)
                # Written down, and deliberately not counted as a problem.
                # "Nothing visibly changed" is true of a dead button and
                # equally true of a Pause that worked -- its whole effect is
                # that the clock stops moving. Only things that are wrong
                # whenever they are seen go on the list the report repeats.
                lines.append(f"Pressed “{label}”: "
                             + (happened or "nothing visibly changed") + ".")

            pressed: List[str] = []

            async def press_each(harsh: bool, limit: int) -> None:
                """Press what there is, looking again after every press.

                Looking again matters: the buttons on a list item -- Done,
                Rename, Remove -- do not exist until something has been
                added, so a list made once at the start never includes
                the buttons most worth pressing.
                """
                for _ in range(limit):
                    label = next(
                        (name for _, name in await buttons()
                         if name not in pressed
                         and bool(_DESTRUCTIVE.search(name)) == harsh), None)
                    if label is None:
                        return
                    pressed.append(label)
                    await press(label)

            await press_each(harsh=False, limit=12)

            shown = (await state())["text"]
            typed = list(dict.fromkeys(tokens))
            seen = [token for token in typed if token in shown]

            # Typed, added, kept by the app -- and nowhere on the page. The
            # app has it and is not showing it. Checked against what the
            # app itself stored, so that a search box, which is meant to
            # show nothing for a made-up word, is not mistaken for this.
            if typed and not seen:
                try:
                    stored = await asyncio.wait_for(page.evaluate(
                        "(tokens) => { let all = ''; try { all = "
                        "JSON.stringify(localStorage); } catch (e) {} "
                        "return tokens.filter((t) => all.includes(t)); }",
                        typed), 8)
                except Exception:
                    stored = []
                if stored:
                    await page.reload(wait_until="load", timeout=15000)
                    await page.wait_for_timeout(700)
                    again = (await state())["text"]
                    if any(token in again for token in stored):
                        saved = True
                        lines.append(
                            f"“{stored[0]}” did not appear on the page after "
                            "it was added. Reloaded the page: now it was there.")
                        problems["stale"] = (
                            "what was added only appeared after the page was "
                            "reloaded, so the page is not being updated when "
                            "something is added")
                    else:
                        lines.append(
                            f"“{stored[0]}” was saved by the app but did not "
                            "appear on the page, before or after a reload.")
                        problems["hidden"] = (
                            "what was added is saved but is never shown "
                            "anywhere on the page")

            if seen:
                await page.reload(wait_until="load", timeout=15000)
                await page.wait_for_timeout(700)
                again = (await state())["text"]
                if any(token in again for token in seen):
                    saved = True
                    lines.append("Reloaded the page: what had been added was still there.")
                else:
                    saved = False
                    lines.append(f"Reloaded the page: “{seen[0]}”, which had "
                                 "just been added, was gone.")
                    problems["reload"] = ("what was added was gone after "
                                          "reloading the page, so it is not "
                                          "being saved")
                    # Put something back, so the buttons that remove things
                    # have something to remove.
                    for label in [name for name in pressed
                                  if _ADDS.search(name)][:2]:
                        await press(label)

            await press_each(harsh=True, limit=5)

            for error in dict.fromkeys(errors):
                problems[f"error:{error[:60]}"] = (
                    f"it threw an error while being used ({error})")
        finally:
            try:
                await browser.close()
            except Exception:
                pass

    return Trial(ran=True, lines=lines[:24], problems=problems, saved=saved)


def _trial_in_fresh_loop(path: Path) -> Trial:
    return _on_fresh_loop(lambda: _trial(path), TRIAL_TIMEOUT_S)


async def try_out(path: Path) -> Trial:
    """Use a page the way a person would, and write down what happened.

    `ran=False` when it could not be done. A trial that did not happen is
    never read as a trial that found nothing wrong.
    """
    try:
        import playwright.async_api  # noqa: F401
    except ImportError:
        return Trial(ran=False, detail="Playwright is not installed")
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_trial_in_fresh_loop, path),
            timeout=TRIAL_TIMEOUT_S + 20)
    except Exception as exc:
        return Trial(ran=False,
                     detail=f"could not use it ({type(exc).__name__}: {str(exc)[:100]})")


# ---------------------------------------------------------------------------
# the builder
# ---------------------------------------------------------------------------
_SYSTEM_HTML ="""You are an expert front-end developer writing one complete, self-contained file.

Rules that are not negotiable:
- Output ONLY the file, from <!DOCTYPE html> to </html>. No explanation before or after it.
- Everything inline: all CSS in one <style> tag, all JavaScript in one <script> tag at the end of <body>.
- No outside resources of any kind: no CDN links, no web fonts, no remote images, no requests to other sites. It must work offline, opened by double-clicking the file. Make sounds with the Web Audio API, never with audio files.
- Always write the WHOLE file. Never abbreviate, and never write a comment such as "rest of the code here". Every line that should exist must be written out.
- It must run without errors: every function you call must be defined, every element you look up by id must exist in the HTML, and buttons must cope with empty input.
- Save anything the user would expect to keep in localStorage.
- It will be opened on a phone over plain HTTP, where some browser features do not exist. Never use the Notification API: show messages in a banner on the page. Before using navigator.clipboard check that it exists, and fall back to document.execCommand('copy').

Visual style, unless the request asks for a different look:
- Define these once and use them everywhere:
  :root { --bg:#0e1320; --panel:#171f31; --line:#27324a; --text:#e7ecf6; --muted:#8793ab; --accent:#5ad2ff; --good:#4ade80; --bad:#f87171; }
- body: background var(--bg), color var(--text), font-family system-ui, sans-serif, margin 0. Centre the app in a container about 720px wide with 24px of padding.
- Group related controls in panels: background var(--panel), 1px solid var(--line), border-radius 14px, padding 20px.
- Buttons: padding 10px 16px, border-radius 10px, no default border, cursor pointer, a visible :hover and :focus-visible state. The main action uses var(--accent) with dark text; other buttons are quiet.
- Inputs, selects and textareas: background var(--bg), color var(--text), 1px solid var(--line), border-radius 8px, padding 8px 10px, font inherit. Every one has a visible label.
- Make the single most important thing on screen large and obvious, with the main actions directly under it.
- Settings and secondary options go in their own panel below the main one, laid out as a tidy grid of label-and-field pairs. Never cram them beside the main display.
- It must be usable on a 375px-wide phone: let rows wrap, and never use fixed widths wider than the screen."""

_SYSTEM_OTHER = """You are an expert programmer writing one complete file.

Rules that are not negotiable:
- Output ONLY the file's contents inside a single fenced code block. No explanation before or after it.
- Always write the WHOLE file. Never abbreviate, and never write a comment such as "rest of the code here".
- It must be correct and complete as it stands: no placeholders, nothing left to fill in.
- Standard library only, and nothing that needs the network."""


# Known-good ways of doing the things a small model reliably gets wrong.
#
# Each of these is here because a real run failed on it: sound came back as
# an <audio> tag pointing at a file that does not exist, a "notification"
# as a call to an API the phone does not have. Telling a 7B model "use the
# Web Audio API" is a wish; handing it the eight lines is a result.
RECIPES = (
    (re.compile(r"\b(?:sound|audio|beep|chime|alarm|ring|tone)", re.I),
     "For sound, put this helper in the script and call it. Do not use "
     "<audio> elements, audio files or data: URIs.\n"
     "function beep(frequency = 880, ms = 250) {\n"
     "  try {\n"
     "    const Ctx = window.AudioContext || window.webkitAudioContext;\n"
     "    const ctx = new Ctx();\n"
     "    const osc = ctx.createOscillator();\n"
     "    const gain = ctx.createGain();\n"
     "    osc.connect(gain); gain.connect(ctx.destination);\n"
     "    osc.frequency.value = frequency; gain.gain.value = 0.15;\n"
     "    osc.start(); osc.stop(ctx.currentTime + ms / 1000);\n"
     "    osc.onended = () => ctx.close();\n"
     "  } catch (error) { /* no audio here */ }\n"
     "}"),
    (re.compile(r"\b(?:notif|remind|alert the user|let the user know)", re.I),
     "Show the message as a banner inside the page: an element fixed to "
     "the top that appears with the text and can be dismissed. Do not use "
     "the Notification API and do not use alert()."),
    (re.compile(r"\b(?:copy|clipboard)", re.I),
     "To copy text, use this helper:\n"
     "function copyText(text) {\n"
     "  if (navigator.clipboard && window.isSecureContext) {\n"
     "    return navigator.clipboard.writeText(text);\n"
     "  }\n"
     "  const area = document.createElement('textarea');\n"
     "  area.value = text; document.body.appendChild(area);\n"
     "  area.select(); document.execCommand('copy'); area.remove();\n"
     "}"),
    (re.compile(r"\b(?:export|download|save (?:as|to) (?:a )?file|csv|json file|backup)", re.I),
     "To give the user a file, use this helper:\n"
     "function downloadFile(name, text) {\n"
     "  const link = document.createElement('a');\n"
     "  link.href = URL.createObjectURL(new Blob([text], { type: 'text/plain' }));\n"
     "  link.download = name; document.body.appendChild(link);\n"
     "  link.click(); link.remove();\n"
     "}"),
    (re.compile(r"\b(?:dark|light) (?:mode|theme)|\btheme", re.I),
     "Do themes by redefining the CSS variables under a class on <body> "
     "(for example body.light { --bg: #f5f7fb; --panel: #ffffff; --line: "
     "#d9e0ec; --text: #1b2333; --muted: #5d6880; }) and toggling that "
     "class. Remember the choice in localStorage."),
    (re.compile(r"\b(?:keyboard|shortcut|hotkey|key ?press)", re.I),
     "Ignore key presses while the user is typing: return early when "
     "event.target is an input, select or textarea. Show the shortcuts "
     "somewhere on the page."),
    (re.compile(r"\b(?:chart|graph|statistic|stats|history)", re.I),
     "Draw any chart yourself with inline SVG or a <canvas>. No chart "
     "libraries. Keep the data behind it in localStorage."),
)


# How to put right each fault the tester can name.
#
# Told only "what was added was gone after a reload", the model looked at
# its own setItem call, concluded the app already saved things, and handed
# the same file back -- twice. It had the saving and not the loading. So
# the remedy is spelled out, as the recipes above are.
REMEDIES = {
    "reload": (
        "To fix what was lost on reload: keep everything the user adds in "
        "ONE array. Every time that array changes, save all of it with "
        "localStorage.setItem('data', JSON.stringify(array)). When the page "
        "loads, before anything else, read it back with "
        "JSON.parse(localStorage.getItem('data') || '[]') and call the "
        "function that draws the list. Saving without loading back is the "
        "usual mistake."),
    "stale": (
        "To fix what did not appear: straight after something is added, "
        "call the same function that draws the list when the page loads, "
        "so the page shows it at once."),
    "hidden": (
        "To fix what never appeared: write one function that draws every "
        "saved item onto the page, and call it when the page loads and "
        "again every time something is added, changed or removed."),
}


def remedies_for(problems: dict) -> str:
    return "\n".join(REMEDIES[key] for key in problems if key in REMEDIES)


def recipes_for(instruction: str) -> str:
    """The known-good snippets that bear on one change."""
    found = [text for pattern, text in RECIPES if pattern.search(instruction or "")]
    return "\n\n".join(found[:2])


class Builder:
    """Grows one file a change at a time, keeping only versions that work."""

    def __init__(self, orchestrator, session, target: Target,
                 num_ctx: int = DEFAULT_NUM_CTX):
        self.orchestrator = orchestrator
        self.session = session
        self.target = target
        self.num_ctx = int(num_ctx or DEFAULT_NUM_CTX)
        self.max_bytes = max_app_bytes(self.num_ctx)

        self.path = Path(session.workspace) / target.filename
        self.hidden = Path(session.workspace) / HIDDEN
        self.current = ""             # the last version that was kept
        self.version = 0
        self.inventory: List[str] = []
        self.controls: List[str] = []
        self.last_smoke: Optional[Smoke] = None
        self.durations: List[float] = []

    # -- state -------------------------------------------------------------
    @property
    def size(self) -> int:
        return len(self.current.encode("utf-8"))

    @property
    def full(self) -> bool:
        """Whether the file is as large as this model can safely rewrite."""
        return bool(self.current) and self.size >= self.max_bytes

    @property
    def expected_seconds(self) -> float:
        """How long the next step is likely to take, from the last few."""
        if not self.durations:
            return DEFAULT_STEP_SECONDS
        recent = self.durations[-3:]
        return sum(recent) / len(recent)

    # -- prompts -----------------------------------------------------------
    def _system(self) -> str:
        return _SYSTEM_HTML if self.target.kind == "html" else _SYSTEM_OTHER

    def _whole_file(self) -> str:
        if self.target.kind == "html":
            return "every line, from <!DOCTYPE html> to </html>"
        return "every line, in one fenced code block"

    def _first_prompt(self, rejected: str) -> str:
        prompt = (
            f"Build this: {self.session.prompt}\n\n"
            f"Write the complete file `{self.target.filename}` now. Aim for a "
            "solid, genuinely usable first version of roughly 200 to 300 "
            "lines: the core idea working properly and looking good. More "
            "features are added afterwards, one at a time, so do the core "
            "well rather than everything thinly."
        )
        if rejected:
            prompt += (f"\n\nYour previous attempt was rejected because "
                       f"{rejected}. Write it again without that problem.")
        return prompt

    def _improve_prompt(self, instruction: str, rejected: str,
                        whole_task: bool = False) -> str:
        lines = len(self.current.splitlines())
        prompt = (
            f"Here is the current `{self.target.filename}` ({lines} lines). "
            + ("It loads without errors.\n\n" if whole_task
               else "It works and has been tested.\n\n")
            + f"```{self.target.language}\n{self.current}```\n\n"
            + (instruction if whole_task
               else f"Make this ONE change: {instruction}")
            + "\n\nDo it properly: it must really work, and anything it adds "
            "for the user must be visible and usable on the page. Keep "
            "everything that already works exactly as it is, and return the "
            f"complete updated file: {self._whole_file()}."
        )
        recipe = recipes_for(instruction) if self.target.kind == "html" else ""
        if recipe:
            prompt += "\n\n" + recipe
        if rejected:
            prompt += (f"\n\nYour previous attempt at this was rejected "
                       f"because {rejected}. Make the same change again "
                       "without that problem.")
        return prompt

    def _room(self) -> int:
        """Tokens to allow for a rewrite, kept inside the context window."""
        current = len(self.current) / CHARS_PER_TOKEN
        wanted = int(current * 1.3) + 900
        ceiling = int(self.num_ctx * 0.97) - int(current) - 700
        return max(1024, min(wanted, ceiling))

    async def _generate(self, prompt: str, max_tokens: int) -> str:
        return await self.orchestrator.ask(
            prompt, system=self._system(), purpose="coding",
            max_tokens=max_tokens, temperature=BUILD_TEMPERATURE)

    # -- judging a reply ---------------------------------------------------
    async def _consider(self, reply: str, feature: bool, check=None) -> Outcome:
        """Decide whether a reply becomes the new version.

        On a refusal the file on disk has not been touched, which is the
        property everything else here leans on.

        `check` is one more test, given the candidate's path before anything
        is committed: it returns a reason to refuse, or None, and what it
        found to be fixed.
        """
        document = extract_document(reply, self.target.kind)
        if document is None:
            return Outcome(False, "the reply was cut off before the end of the file")

        problem = judge(document, self.current, self.target.kind,
                        self.max_bytes, feature=feature)
        if problem:
            return Outcome(False, problem)

        self.hidden.mkdir(parents=True, exist_ok=True)
        candidate = self.hidden / f"candidate{self.target.suffix}"
        candidate.write_text(document, encoding="utf-8")

        smoke = Smoke(tested=False, detail="only pages are opened in a browser")
        shot = self.hidden / "candidate.png"
        if self.target.kind == "html":
            if shot.exists():
                shot.unlink()
            smoke = await smoke_test(candidate, shot)
            if smoke.failed:
                return Outcome(False, "it failed when opened in a browser: "
                               + smoke.summary())

        lost = await self._lost(document, smoke)
        if lost:
            shown = ", ".join(lost[:4]) + (f" and {len(lost) - 4} more"
                                           if len(lost) > 4 else "")
            return Outcome(False, f"it removed things that were already in "
                                  f"the app ({shown}), and everything that "
                                  "was there has to stay")

        fixed: List[str] = []
        if check is not None:
            refusal, fixed = await check(candidate)
            if refusal:
                return Outcome(False, refusal)

        # Kept. Only now does the real file change.
        plus, minus = diff_stats(self.current, document)
        first = not self.current
        self.version += 1
        self.path.write_text(document, encoding="utf-8")

        versions = self.hidden / "versions"
        versions.mkdir(parents=True, exist_ok=True)
        shutil.copy2(candidate, versions / f"v{self.version:03d}{self.target.suffix}")

        if self.target.kind == "html" and shot.exists():
            shutil.copy2(shot, Path(self.session.workspace) / PREVIEW)
            self.session.preview = PREVIEW

        # What is on the page now that was not before: the evidence, as
        # opposed to the claim, that this change put something there.
        before = {label.lower() for label in self.inventory}
        added = ([] if first else
                 [label for label in smoke.inventory
                  if label.lower() not in before][:6])
        if smoke.tested:
            self.inventory = smoke.inventory
            self.controls = smoke.controls

        self.current = document
        self.last_smoke = smoke
        lines = len(document.splitlines())

        if self.target.kind == "html":
            checked = "browser" if smoke.tested else "untested"
        elif self.target.kind == "python":
            checked = "syntax"
        else:
            checked = "none"

        self.session.artifact = self.target.filename
        self.session.app = {
            "file": self.target.filename,
            "kind": self.target.kind,
            "lines": lines,
            "bytes": self.size,
            "max_bytes": self.max_bytes,
            "version": self.version,
            "checked": checked,
            "tested": smoke.tested and smoke.ok,
            "test_detail": smoke.detail,
            "full": self.full,
        }

        sentence = f"{lines} lines" if first else f"+{plus} −{minus} lines"
        if self.target.kind == "html" and not smoke.tested:
            sentence += f" · not browser-tested ({smoke.detail})"
        return Outcome(True, sentence, plus=plus, minus=minus, added=added,
                       fixed=fixed)

    async def _lost(self, document: str, smoke: Smoke) -> List[str]:
        """What a new version no longer has that the current one does.

        The report says a feature is "in the app" from the moment the
        change adding it is kept. That stays true only if no later change
        quietly takes it out again -- and a model asked to add a dark mode
        to a long file will sometimes hand back the dark mode and a file
        missing its export button. Size does not show that. What was on the
        page does.

        Two things are compared: the ids in the source, and the labels of
        the controls a browser actually found on the page.
        """
        if not self.current:
            return []

        lost = [f'id="{name}"'
                for name in sorted(element_ids(self.current) - element_ids(document))]

        if smoke.tested and self.controls:
            now = {_same_label(label) for label in smoke.controls}
            missing = [label for label in self.controls
                       if _same_label(label) not in now]
            if missing and self.target.kind == "html":
                # Before blaming the change, look at the current version
                # again, now. A label can differ because the day changed
                # since it was read -- and a stale reading would otherwise
                # reject every change from midnight onwards.
                fresh = await smoke_test(self.path)
                if fresh.tested:
                    self.controls = fresh.controls
                    missing = [label for label in fresh.controls
                               if _same_label(label) not in now]
            lost += [f"“{label}”" for label in missing]

        return lost

    # -- the two things it does -------------------------------------------
    async def first_version(self, job) -> bool:
        """Write the first version. Until this works there is no app."""
        job.state = "doing"
        self.session.note("step", f"starting: {job.title}")
        rejected = ""

        for attempt in range(FIRST_ATTEMPTS):
            if self.session.stopping or self.session.expired:
                break
            started = time.monotonic()
            try:
                reply = await self._generate(self._first_prompt(rejected),
                                             FIRST_MAX_TOKENS)
            except Exception as exc:
                rejected = f"the model call failed ({str(exc)[:120]})"
                self.session.record_step(job.title, False, rejected,
                                         source=job.source)
                continue

            outcome = await self._consider(reply, feature=False)
            self.durations.append(time.monotonic() - started)
            if outcome.kept:
                self.session.record_step(job.title, True, outcome.sentence,
                                         source=job.source)
                self.session.finish_job(job, outcome.sentence, done=True)
                self.session.save_record()
                return True

            rejected = outcome.sentence
            self.session.record_step(
                f"{job.title} (attempt {attempt + 1})", False, rejected,
                source=job.source)
            self.session.note("note", f"first version, attempt {attempt + 1}: "
                                      f"{rejected}")

        self.session.finish_job(job, rejected or "ran out of time", done=False)
        self.session.save_record()
        return False

    async def improve(self, job, instruction: str = "", feature: bool = True,
                      has_time: Optional[Callable[[], bool]] = None,
                      check=None, whole_task: bool = False) -> bool:
        """Apply one change, keeping it only if the result still works.

        One retry, with the reason for the refusal handed back. A second
        failure usually means the change is beyond the model rather than
        that it was unlucky, and the time is better spent on the next one.
        """
        job.state = "doing"
        self.session.note("step", f"starting: {job.title}")
        started = time.monotonic()
        rejected = ""

        for attempt in range(2):
            if attempt and has_time is not None and not has_time():
                break
            if self.session.stopping:
                break
            try:
                reply = await self._generate(
                    self._improve_prompt(instruction or job.title, rejected,
                                         whole_task=whole_task),
                    self._room())
            except Exception as exc:
                rejected = f"the model call failed ({str(exc)[:120]})"
                break

            outcome = await self._consider(reply, feature=feature, check=check)
            if outcome.kept:
                self.durations.append(time.monotonic() - started)
                self.session.record_step(
                    job.title, True, outcome.sentence, source=job.source,
                    plus=outcome.plus, minus=outcome.minus, added=outcome.added,
                    fixed=outcome.fixed)
                self.session.finish_job(job, outcome.sentence, done=True)
                self.session.save_record()
                return True
            rejected = outcome.sentence

        self.durations.append(time.monotonic() - started)
        reason = rejected or "stopped before it could be tried"
        self.session.record_step(job.title, False, reason, source=job.source)
        self.session.finish_job(job, reason, done=False)
        self.session.save_record()
        return False

    async def fix(self, job, trial: Trial,
                  has_time: Optional[Callable[[], bool]] = None) -> bool:
        """Have what went wrong in use put right, and check that it was.

        The model is shown what a tester actually saw, not asked to imagine
        what might be wrong. And "fixed" is not its word for it: the new
        version is used in exactly the same way, and it is kept only if a
        problem that was seen before is not seen again.
        """
        instruction = (
            trial.account() + "\n\n"
            + ("Fix the problems listed, and anything else in that record "
               "that is clearly wrong" if trial.problems else
               "Fix anything in that record that is clearly wrong")
            + f" for this: {self.session.prompt}\n"
            "Do not add new features and do not remove any."
        )
        remedy = remedies_for(trial.problems)
        if remedy:
            instruction += "\n\n" + remedy

        async def check(candidate: Path):
            after = await try_out(candidate)
            if not after.ran:
                return None, []   # could not compare; the other checks stand
            cured = [sentence for key, sentence in trial.problems.items()
                     if key not in after.problems]
            caused = [sentence for key, sentence in after.problems.items()
                      if key not in trial.problems]
            if caused:
                return f"it made something new go wrong: {caused[0]}", []
            if trial.problems and not cured:
                first = next(iter(trial.problems.values()))
                return (f"it did not fix what the tester saw ({first}): the "
                        "same thing happened again when the new version was "
                        "used"), []
            return None, cured

        return await self.improve(job, instruction=instruction, feature=False,
                                  has_time=has_time, check=check,
                                  whole_task=True)
