"""Unattended work: start it, watch it, stop it."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from ..container import container
from .deps import current_user

router = APIRouter()


def _service():
    service = container.get("autonomy")
    if service is None:
        raise HTTPException(status_code=503,
                            detail="Unattended work is not available.")
    return service


class StartRequest(BaseModel):
    prompt: str
    away_minutes: float = 240
    work_minutes: float = 180
    allow_shell: bool = False


@router.get("/autonomy", tags=["autonomy"])
async def status(user: Dict[str, Any] = Depends(current_user)):
    return _service().status()


@router.post("/autonomy/start", tags=["autonomy"])
async def start(payload: StartRequest,
                user: Dict[str, Any] = Depends(current_user)):
    try:
        return await _service().start(
            payload.prompt, payload.away_minutes, payload.work_minutes,
            payload.allow_shell)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/autonomy/produced", tags=["autonomy"])
async def produced(stamp: str = Query(""),
                   user: Dict[str, Any] = Depends(current_user)):
    """What a run actually made."""
    return _service().produced(stamp)


@router.get("/autonomy/runs", tags=["autonomy"])
async def runs(user: Dict[str, Any] = Depends(current_user)):
    """Every run that left files behind, including from before a restart."""
    return {"runs": _service().runs()}


# What a page JARVIS built is allowed to do when opened from here.
#
# It is served from this origin, which means its scripts would otherwise run
# with the owner's session: a page written by a model, unsupervised, able to
# call every endpoint this server has. So it is fenced. It may run its own
# inline script and style, keep things in localStorage, draw, and make
# sound -- everything a self-contained app needs -- and it may not make a
# request of any kind, submit a form, or load anything from anywhere.
#
# `connect-src 'none'` is the line doing the work: no fetch, no XHR, no
# websocket, so nothing on the page can reach /api. The rest closes the
# side doors (images, forms and frames all make requests too).
#
# It also makes "works offline" true where the owner actually opens it, and
# not only in the browser check.
APP_POLICY = "; ".join((
    "default-src 'none'",
    "script-src 'unsafe-inline' 'unsafe-eval'",
    "style-src 'unsafe-inline'",
    "img-src data: blob:",
    "media-src data: blob:",
    "font-src data:",
    "worker-src blob:",
    "connect-src 'none'",
    "form-action 'none'",
    "frame-src 'none'",
    "base-uri 'none'",
))

PAGE_SUFFIXES = (".html", ".htm", ".svg", ".xml")
APP_SUFFIXES = (".html", ".htm")

# Each built app gets storage of its own.
#
# Opened from here, an app shares this origin's localStorage with JARVIS's
# own interface and with every other app JARVIS has built. That goes wrong
# in ordinary ways: a habit tracker that lists "everything saved" lists
# JARVIS's settings as habits, a "clear all data" button clears them, and
# two apps that both keep their data under "items" overwrite each other.
# None of it shows in testing, where the app has an empty store to itself.
#
# So the page is handed a localStorage that only sees its own keys. Added
# as it is served -- the file on disk, and the one that is saved or sent,
# stays exactly what was built.
_OWN_STORAGE = """<script>
/* Added by JARVIS when serving this page: keeps this app's saved data
   separate from JARVIS's own and from other apps it has built. */
(() => {
  let real;
  try { real = window.localStorage; } catch (error) { return; }
  if (!real) return;
  const prefix = __PREFIX__;
  const mine = () => {
    const keys = [];
    for (let i = 0; i < real.length; i++) {
      const key = real.key(i);
      if (key !== null && key.startsWith(prefix)) keys.push(key.slice(prefix.length));
    }
    return keys;
  };
  const store = {
    getItem: (key) => real.getItem(prefix + key),
    setItem: (key, value) => { real.setItem(prefix + key, String(value)); },
    removeItem: (key) => { real.removeItem(prefix + key); },
    clear: () => { for (const key of mine()) real.removeItem(prefix + key); },
    key: (index) => { const key = mine()[index]; return key === undefined ? null : key; },
  };
  const scoped = new Proxy(store, {
    get: (target, name) => {
      if (name === 'length') return mine().length;
      if (name in target) return target[name];
      if (typeof name !== 'string') return undefined;
      const value = real.getItem(prefix + name);
      return value === null ? undefined : value;
    },
    set: (target, name, value) => { real.setItem(prefix + String(name), String(value)); return true; },
    deleteProperty: (target, name) => { real.removeItem(prefix + String(name)); return true; },
    has: (target, name) => name in target || name === 'length' || real.getItem(prefix + String(name)) !== null,
    ownKeys: () => mine(),
    getOwnPropertyDescriptor: (target, name) => {
      const value = typeof name === 'string' ? real.getItem(prefix + name) : null;
      return value === null ? undefined
        : { value, enumerable: true, configurable: true, writable: true };
    },
  });
  try {
    Object.defineProperty(window, 'localStorage', { value: scoped, configurable: true });
  } catch (error) { /* leave the shared store in place rather than none */ }
})();
</script>"""

_HEAD = re.compile(r"<head\b[^>]*>", re.IGNORECASE)
_HTML = re.compile(r"<html\b[^>]*>", re.IGNORECASE)


def with_own_storage(html: str, app: str) -> str:
    """A page, with storage that belongs to it alone.

    `app` names the app -- its run and its file -- and becomes the prefix
    its keys are kept under. It is put into a script, so it is written as a
    JSON string with the one sequence that could end that script escaped.
    """
    prefix = json.dumps(f"jarvis-app:{app}:").replace("<", "\\u003c")
    shim = _OWN_STORAGE.replace("__PREFIX__", prefix)
    # Before any script of the page's own, so that nothing of the app's
    # ever sees the shared store.
    for anchor in (_HEAD, _HTML):
        found = anchor.search(html)
        if found:
            return html[:found.end()] + "\n" + shim + html[found.end():]
    return shim + html


@router.get("/autonomy/file", tags=["autonomy"])
async def file(path: str = Query(...), stamp: str = Query(""),
               download: bool = Query(False),
               user: Dict[str, Any] = Depends(current_user)):
    """Serve one produced file, so a page it built can simply be opened.

    Served inline rather than as a download: the whole point is that an
    HTML app it wrote can be looked at. Confinement to the workspace is
    enforced in the service, not here.
    """
    resolved = _service().resolve_file(path, stamp)
    if resolved is None:
        raise HTTPException(status_code=404, detail="No such file in that run.")

    headers = {
        # A run still in progress replaces this file every few minutes.
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }
    if resolved.suffix.lower() in PAGE_SUFFIXES:
        headers["Content-Security-Policy"] = APP_POLICY

    if download:
        return FileResponse(resolved, filename=resolved.name, headers=headers)

    if resolved.suffix.lower() in APP_SUFFIXES:
        service = _service()
        try:
            app = resolved.relative_to(Path(service.root).resolve()).as_posix()
        except ValueError:
            app = resolved.name
        page = resolved.read_text(encoding="utf-8", errors="replace")
        return HTMLResponse(with_own_storage(page, app), headers=headers)
    return FileResponse(resolved, headers=headers)


@router.get("/autonomy/download", tags=["autonomy"])
async def download(stamp: str = Query(""),
                   user: Dict[str, Any] = Depends(current_user)):
    """The whole run as one zip."""
    bundle = _service().archive(stamp)
    if bundle is None:
        raise HTTPException(status_code=404, detail="That run produced nothing.")
    return FileResponse(bundle, filename=bundle.name,
                        media_type="application/zip")


@router.post("/autonomy/back", tags=["autonomy"])
async def back(user: Dict[str, Any] = Depends(current_user)):
    """The owner is back: hand over the report and clear the slot."""
    return await _service().welcome_back()


@router.post("/autonomy/stop", tags=["autonomy"])
async def stop(user: Dict[str, Any] = Depends(current_user)):
    """Ask it to finish the current step and wrap up."""
    return await _service().stop()


@router.post("/autonomy/cancel", tags=["autonomy"])
async def cancel(user: Dict[str, Any] = Depends(current_user)):
    """Stop immediately, without wrapping up."""
    return await _service().cancel()
