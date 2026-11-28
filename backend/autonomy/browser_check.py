"""Look at a web page the way a person would: a screenshot and what it says.

Run by Claude Code while it builds (it may run Python, not browsers
directly), so a page it made gets looked at, not just its tests:

    python browser_check.py index.html            # desktop size
    python browser_check.py index.html --mobile   # a 390-wide phone
    python browser_check.py http://localhost:5173

Uses a headless Edge (or Chrome) with a throwaway profile of its own, never
the owner's: no cookies, no logins, no extensions. Prints where the
screenshot went (open it to look) and the page's visible text after its
scripts ran, so empty or broken pages show up.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BROWSERS = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]


def find_browser() -> str:
    for name in ("msedge", "chrome", "google-chrome", "chromium"):
        found = shutil.which(name)
        if found:
            return found
    for path in BROWSERS:
        if os.path.exists(path):
            return path
    raise SystemExit("No Edge or Chrome found to look at the page with.")


def address(target: str) -> str:
    if re.match(r"^[a-z]+://", target, re.I):
        return target
    path = Path(target).resolve()
    if not path.exists():
        raise SystemExit(f"There's no {target} here.")
    return path.as_uri()


NARROWEST = 500          # headless Edge/Chrome won't lay a window out any narrower


def crop(picture: Path, width: int, height: int) -> None:
    """Keep just the phone-sized frame of the screenshot."""
    try:
        from PIL import Image
    except ImportError:
        return                                           # the frame is still at the top left
    with Image.open(picture) as image:
        image.crop((0, 0, width, height)).save(picture)


def visible_text(dom: str) -> str:
    dom = re.sub(r"(?is)<(script|style|noscript)[^>]*>.*?</\1>", " ", dom)
    text = html.unescape(re.sub(r"(?s)<[^>]+>", " ", dom))
    return " ".join(text.split())


def main() -> int:
    parser = argparse.ArgumentParser(description="Screenshot a page and print what it says.")
    parser.add_argument("target", help="an HTML file or a URL")
    parser.add_argument("--mobile", action="store_true", help="a 390x844 phone screen")
    parser.add_argument("--width", type=int, default=1280)
    parser.add_argument("--height", type=int, default=900)
    parser.add_argument("--out", default="", help="where to save the screenshot")
    args = parser.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")   # pages have "−", "✓", "€"
    except (AttributeError, OSError):
        pass

    width, height = (390, 844) if args.mobile else (args.width, args.height)
    url = address(args.target)
    out = Path(args.out or Path("screenshots") / f"{Path(args.target).stem or 'page'}-{width}x{height}.png").resolve()
    out.parent.mkdir(parents=True, exist_ok=True)
    browser = find_browser()
    with tempfile.TemporaryDirectory(prefix="jarvis-browser-") as profile:
        def browse(size: str, *extra: str):
            return subprocess.run([browser, "--headless=new", "--disable-gpu", "--no-first-run",
                                   "--disable-extensions", "--no-default-browser-check", "--hide-scrollbars",
                                   f"--user-data-dir={profile}", f"--window-size={size}",
                                   "--virtual-time-budget=4000", *extra],
                                  capture_output=True, text=True, timeout=60, encoding="utf-8", errors="replace")

        if width < NARROWEST:
            # Headless windows don't go narrower than about 500px: a "390-wide"
            # window lays the page out at ~492 and crops the picture, so every
            # centred page looked cut off. Instead the page gets a frame of
            # exactly the phone's size, and the picture is cropped to it.
            frame = Path(profile) / "phone.html"
            frame.write_text(f'<!doctype html><body style="margin:0;background:#fff">'
                             f'<iframe src="{html.escape(url)}" width="{width}" height="{height}" '
                             f'style="border:0;display:block"></iframe></body>', encoding="utf-8")
            shot = browse(f"{NARROWEST + 20},{height + 20}", f"--screenshot={out}", frame.as_uri())
            if out.exists():
                crop(out, width, height)
        else:
            shot = browse(f"{width},{height}", f"--screenshot={out}", url)
        dom = browse(f"{max(width, NARROWEST)},{height}", "--dump-dom", url)
    if not out.exists():
        print("The screenshot failed:", (shot.stderr or shot.stdout).strip()[:500])
        return 1
    text = visible_text(dom.stdout or "")
    print(f"Screenshot ({width}x{height}): {out}")
    print(f"Open it to look at the page. Visible text after scripts ran ({len(text)} characters):")
    print(text[:3000] or "(nothing: the page rendered empty)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
