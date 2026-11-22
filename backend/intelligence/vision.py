"""
Vision — JARVIS can look at the screen.

Capture, downscale, and hand an image to a vision-capable model with a task
prompt. Powers "what am I looking at", explaining an on-screen error, reading
text out of an image, understanding a diagram or graph, comparing two
screenshots and general visual troubleshooting.

Privacy is explicit: a screenshot is only ever captured when a tool asks for
one, the `system.screenshot` permission gates it, capture is confirmed at the
medium risk tier, and sending the image to a cloud model is refused outright in
privacy mode. Every capture is recorded in the observations table so there is a
visible history of what was looked at.
"""
from __future__ import annotations

import asyncio
import base64
import logging
import platform
import subprocess

from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.proc import run as _run, popen as _popen
from ..core.util import new_id, now_iso, since, truncate
from ..db.database import dumps, loads
from ..systemmon import control

logger = logging.getLogger("jarvis.vision")

# Anthropic downsamples above ~1568px on the long edge anyway; sending a raw 4K
# PNG just wastes tokens and time.
MAX_EDGE = 1400
MAX_BYTES = 4_500_000

PROMPTS = {
    "describe": (
        "Describe what is on this screen. Say which application it appears to be, what the user "
        "is doing, and anything notable. Be concrete and brief."
    ),
    "error": (
        "There may be an error on this screen. Find it, quote the exact message, explain in plain "
        "language what it means, and give the most likely fix. If there is no error visible, say so."
    ),
    "ocr": (
        "Transcribe all readable text in this image. Preserve the reading order and structure. "
        "Output only the text."
    ),
    "elements": (
        "List the interactive elements visible (buttons, fields, menus, links). For each give its "
        "visible label and roughly where it is (e.g. 'top-right', 'centre-left')."
    ),
    "diagram": (
        "Interpret this diagram or chart. Explain what it shows, the relationships or trend, and "
        "any specific values you can read. Say clearly if part of it is illegible."
    ),
    "troubleshoot": (
        "Something is wrong on this screen. Diagnose it: what is the user trying to do, what is "
        "blocking them, and what are the concrete next steps?"
    ),
}


class VisionService:
    def __init__(self, db, settings, settings_store, orchestrator, activity, awareness=None):
        self.db = db
        self.settings = settings
        self.settings_store = settings_store
        self.orchestrator = orchestrator
        self.activity = activity
        self.awareness = awareness
        self._last_provider: Dict[str, Any] = {}

    # ==================================================================
    async def _pick_provider(self) -> Dict[str, Any]:
        """Choose who looks at the image.

        Local first when privacy mode is on -- a screenshot is the most
        sensitive thing JARVIS handles, so 'stay on this machine' has to be a
        real option, not a refusal."""
        privacy = await self.settings_store.get("privacy", {})
        local_only = bool(
            self.settings.local_only                     # configured local-only
            or privacy.get("privacy_mode")
            or not privacy.get("cloud_ai", True)
        )

        ollama = self.orchestrator.providers.get("ollama")
        local_model = None
        if ollama is not None:
            try:
                if await ollama.available():
                    local_model = await ollama.vision_model()
            except Exception:
                logger.debug("could not probe local vision models", exc_info=True)

        if local_only:
            if local_model:
                return {"ok": True, "provider": ollama, "name": "ollama", "model": local_model,
                        "local": True}
            why = ("JARVIS is running on local models only"
                   if self.settings.local_only
                   else "Privacy mode keeps screenshots on this machine")
            return {
                "ok": False,
                "reason": (
                    f"{why}, but no local vision model is installed. Run `ollama pull gemma3` "
                    "(or llava / llama3.2-vision) and try again."
                ),
            }

        anthropic = self.orchestrator.providers.get("anthropic")
        if anthropic is not None and self.settings.ANTHROPIC_API_KEY:
            return {"ok": True, "provider": anthropic, "name": "anthropic",
                    "model": self.orchestrator.model_for("vision"), "local": False}

        if local_model:
            return {"ok": True, "provider": ollama, "name": "ollama", "model": local_model,
                    "local": True}

        return {
            "ok": False,
            "reason": (
                "No vision-capable model is available. Install one with "
                "`ollama pull gemma3` (or llava / llama3.2-vision)."
            ),
        }

    async def available(self) -> Dict[str, Any]:
        """Whether vision can actually run right now, and why not if it can't."""
        choice = await self._pick_provider()
        if not choice["ok"]:
            return {"ok": False, "reason": choice["reason"]}
        return {
            "ok": True,
            "provider": choice["name"],
            "model": choice["model"],
            "local": choice["local"],
        }

    # ==================================================================
    async def capture(self) -> Dict[str, Any]:
        """Take a screenshot and downscale it for the model."""
        loop = asyncio.get_event_loop()
        destination = Path(self.settings.DATA_DIR) / "screenshots"
        result = await loop.run_in_executor(None, control.screenshot, destination)
        if not result.get("ok"):
            return result

        path = Path(result["path"])
        shrunk = await loop.run_in_executor(None, _downscale, path, MAX_EDGE)
        return {**result, "path": str(shrunk), "original": str(path)}

    # ==================================================================
    async def look(
        self,
        task: str = "describe",
        question: str = "",
        image_path: Optional[str] = None,
        purpose: str = "vision",
    ) -> Dict[str, Any]:
        """Capture (or load) an image and ask the vision model about it."""
        gate = await self.available()
        if not gate["ok"]:
            return {"ok": False, "message": gate["reason"]}

        if image_path:
            path = Path(image_path)
            if not path.exists():
                return {"ok": False, "message": f"No image at '{image_path}'."}
            captured = {"path": str(path)}
        else:
            captured = await self.capture()
            if not captured.get("ok", True) and "path" not in captured:
                return {"ok": False, "message": captured.get("message", "Screen capture failed.")}

        prompt = question.strip() or PROMPTS.get(task, PROMPTS["describe"])
        if question and task in PROMPTS and task != "describe":
            prompt = f"{PROMPTS[task]}\n\nThe user also asks: {question}"

        try:
            answer = await self._ask_image([Path(captured["path"])], prompt, purpose)
        except Exception as exc:
            logger.exception("vision request failed")
            return {"ok": False, "message": f"Vision request failed: {exc}"}

        app = ""
        if self.awareness:
            app = (self.awareness.snapshot() or {}).get("active_app") or ""

        observation_id = new_id("obs")
        await self.db.execute(
            "INSERT INTO observations (id, ts, kind, path, summary, detail, app)"
            " VALUES (?,?,?,?,?,?,?)",
            (observation_id, now_iso(), task, captured["path"], truncate(answer, 2000),
             dumps({"prompt": truncate(prompt, 500)}), app),
        )
        await self.activity.record(f"Looked at the screen ({task}).", source="vision")

        return {
            "ok": True,
            "task": task,
            "answer": answer,
            "image": captured["path"],
            "active_app": app,
            "observation_id": observation_id,
            # Which model actually looked, so "was that sent to the cloud?" has
            # a factual answer.
            **self._last_provider,
        }

    # ==================================================================
    async def compare(self, before: str, after: Optional[str] = None,
                      question: str = "") -> Dict[str, Any]:
        """Compare two images -- typically a before/after pair."""
        gate = await self.available()
        if not gate["ok"]:
            return {"ok": False, "message": gate["reason"]}

        before_path = Path(before)
        if not before_path.exists():
            return {"ok": False, "message": f"No image at '{before}'."}

        if after:
            after_path = Path(after)
            if not after_path.exists():
                return {"ok": False, "message": f"No image at '{after}'."}
        else:
            captured = await self.capture()
            if "path" not in captured:
                return {"ok": False, "message": captured.get("message", "Capture failed.")}
            after_path = Path(captured["path"])

        prompt = (
            "These are two screenshots: the first is BEFORE, the second is AFTER. "
            "List what actually changed between them. Be specific and ignore cosmetic noise "
            "like clock digits. If nothing meaningful changed, say so plainly."
        )
        if question:
            prompt += f"\n\nThe user also asks: {question}"

        try:
            answer = await self._ask_image([before_path, after_path], prompt, "vision")
        except Exception as exc:
            return {"ok": False, "message": f"Comparison failed: {exc}"}

        return {"ok": True, "answer": answer, "before": str(before_path),
                "after": str(after_path), **self._last_provider}

    # ==================================================================
    async def _ask_image(self, paths: List[Path], prompt: str, purpose: str) -> str:
        """Build a multimodal message and send it straight to the provider."""
        blocks: List[Dict[str, Any]] = []
        for path in paths:
            data = path.read_bytes()
            if len(data) > MAX_BYTES:
                shrunk = await asyncio.get_event_loop().run_in_executor(
                    None, _downscale, path, 900
                )
                data = Path(shrunk).read_bytes()
            blocks.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": base64.b64encode(data).decode("ascii"),
                },
            })
        blocks.append({"type": "text", "text": prompt})

        choice = await self._pick_provider()
        if not choice["ok"]:
            raise RuntimeError(choice["reason"])

        system = (
            "You are looking at the user's own screen at their request. Report only what you "
            "can actually see. If something is unreadable or ambiguous, say so rather than "
            "guessing. Never invent UI that is not visible."
        )
        message = [{"role": "user", "content": blocks}]

        attempts = [choice]
        # A present-but-rejected API key is the common case, so fall back to a
        # local vision model the same way the conversation path does.
        if not choice["local"]:
            local = await self._local_choice()
            if local:
                attempts.append(local)

        last_error: Optional[Exception] = None
        for attempt in attempts:
            try:
                completion = await attempt["provider"].complete(
                    messages=message, system=system, tools=None,
                    model=attempt["model"], max_tokens=1600, temperature=0.2,
                )
            except Exception as exc:
                last_error = exc
                logger.warning("vision via %s failed: %s", attempt["name"], exc)
                if attempt is not attempts[-1]:
                    await self.activity.record(
                        f"Vision provider {attempt['name']} unavailable -- falling back to "
                        f"the local model.",
                        level="warn", source="vision",
                    )
                continue

            await self.orchestrator._record_usage(completion, purpose)
            self._last_provider = {"provider": attempt["name"], "model": attempt["model"],
                                   "local": attempt["local"]}
            return completion.text

        raise RuntimeError(str(last_error) if last_error else "No vision provider responded.")

    async def _local_choice(self) -> Optional[Dict[str, Any]]:
        ollama = self.orchestrator.providers.get("ollama")
        if ollama is None:
            return None
        try:
            if not await ollama.available():
                return None
            model = await ollama.vision_model()
        except Exception:
            return None
        if not model:
            return None
        return {"ok": True, "provider": ollama, "name": "ollama", "model": model, "local": True}

    # ==================================================================
    async def observations(self, limit: int = 30) -> List[Dict[str, Any]]:
        rows = await self.db.fetch_all(
            "SELECT * FROM observations ORDER BY ts DESC LIMIT ?", (limit,))
        for row in rows:
            row["detail"] = loads(row.get("detail"), {})
        return rows

    async def purge(self, keep_days: int = 7) -> Dict[str, int]:
        """Screenshots are the most sensitive thing JARVIS stores, so they are
        pruned aggressively and the files are removed with the rows."""
        stale = await self.db.fetch_all(
            "SELECT id, path FROM observations WHERE ts < ?", (since(keep_days * 86400),))
        removed = 0
        for row in stale:
            try:
                Path(row["path"]).unlink(missing_ok=True)
                removed += 1
            except OSError:
                continue
        rows = await self.db.execute(
            "DELETE FROM observations WHERE ts < ?", (since(keep_days * 86400),))
        return {"rows_deleted": rows, "files_deleted": removed}


# ---------------------------------------------------------------------------
def _downscale(path: Path, max_edge: int) -> Path:
    """Shrink an image to `max_edge` on its long side.

    Uses Pillow when present; otherwise System.Drawing via PowerShell on
    Windows. If neither is available the original is returned unchanged --
    correct behaviour, just more tokens."""
    try:
        from PIL import Image  # type: ignore

        with Image.open(path) as image:
            if max(image.size) <= max_edge:
                return path
            ratio = max_edge / max(image.size)
            resized = image.resize(
                (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
                Image.LANCZOS,
            )
            target = path.with_name(f"{path.stem}_small.png")
            resized.save(target, "PNG", optimize=True)
            return target
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("Pillow downscale failed: %s", exc)

    if platform.system() != "Windows":
        return path

    target = path.with_name(f"{path.stem}_small.png")
    script = (
        "Add-Type -AssemblyName System.Drawing; "
        f"$img = [System.Drawing.Image]::FromFile('{path.as_posix()}'); "
        f"$max = {max_edge}; "
        "if ([Math]::Max($img.Width, $img.Height) -le $max) { $img.Dispose(); exit 2 } "
        "$r = $max / [Math]::Max($img.Width, $img.Height); "
        "$w = [int]($img.Width * $r); $h = [int]($img.Height * $r); "
        "$bmp = New-Object System.Drawing.Bitmap $w, $h; "
        "$g = [System.Drawing.Graphics]::FromImage($bmp); "
        "$g.InterpolationMode = 'HighQualityBicubic'; "
        "$g.DrawImage($img, 0, 0, $w, $h); "
        f"$bmp.Save('{target.as_posix()}', [System.Drawing.Imaging.ImageFormat]::Png); "
        "$g.Dispose(); $bmp.Dispose(); $img.Dispose()"
    )
    try:
        result = _run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and target.exists():
            return target
    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("PowerShell downscale failed: %s", exc)
    return path
