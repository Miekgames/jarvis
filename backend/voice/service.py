"""
Voice pipeline.

Two paths, both real:

  browser  -- the Web Speech API does STT and TTS in the page. Zero latency to
              the speakers, no audio leaves the machine, works with no extra
              packages. This is the default.
  server   -- faster-whisper for STT and edge-tts for TTS, exposed over HTTP.
              Better recognition and a much better voice, at the cost of a
              round trip and two optional dependencies.

Whisper is loaded lazily on first use so startup isn't blocked by a model load
that may never be needed.
"""
from __future__ import annotations

import asyncio
import io
import logging
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..core.events import Events, bus
from ..core.util import now_iso, truncate

logger = logging.getLogger("jarvis.voice")



_EMOJI = __import__("re").compile(
    "[🀀-🫿☀-➿⬀-⯿⌀-⏿️‍⃣]+")


def without_emojis(text: str) -> str:
    """The words without the emojis, for speaking."""
    spoken = " ".join(_EMOJI.sub(" ", text or "").split())
    return __import__("re").sub(r"\s+([.,!?;:])", r"\1", spoken)


class VoiceService:
    def __init__(self, settings, settings_store, activity):
        self.settings = settings
        self.settings_store = settings_store
        self.activity = activity
        self._whisper = None
        self._whisper_error: Optional[str] = None
        self._loading = False
        self._speaking = False
        # When speech last finished. The wake-word detector uses this to
        # ignore JARVIS's own voice arriving through the speakers a beat
        # after it stops -- otherwise it wakes itself.
        self.speaking_ended_at = 0.0
        # What is being said right now, for the barge-in check.
        self.speaking_text = ""
        # (tts, stt) once probed. The probe is slow and the answer
        # cannot change while the process runs.
        self._packages_checked: Optional[tuple] = None
        # Last real synthesis outcome. `import edge_tts` succeeding says
        # nothing about whether Microsoft will serve a request -- when the
        # endpoint started refusing with 403, health still read 'online'.
        self._tts_error: Optional[str] = None
        self._tts_ok_at: float = 0.0

    # ==================================================================
    # text to speech
    # ==================================================================
    async def synthesise(self, text: str, voice: Optional[str] = None, rate: float = 1.0) -> bytes:
        """Return MP3 bytes from edge-tts. Raises with a clear message if the
        package is missing or the service is unreachable."""
        # Emojis are for reading: spoken, "✅" comes out as "check mark button".
        text = without_emojis(text)
        if not text.strip():
            raise ValueError("Nothing to say.")
        try:
            import edge_tts
        except ImportError:
            raise RuntimeError(
                "Server-side speech needs the 'edge-tts' package (pip install edge-tts). "
                "Browser speech works without it -- switch the TTS engine to 'browser' in Settings."
            )

        voice = voice or await self._voice_name()
        percent = int(round((rate - 1.0) * 100))
        rate_arg = f"{percent:+d}%"

        communicate = edge_tts.Communicate(text, voice, rate=rate_arg)
        buffer = io.BytesIO()
        try:
            async for chunk in communicate.stream():
                if chunk.get("type") == "audio":
                    buffer.write(chunk["data"])
        except Exception as exc:
            self._tts_error = str(exc)[:300]
            raise RuntimeError(f"Speech synthesis failed: {exc}")

        audio = buffer.getvalue()
        if not audio:
            self._tts_error = "the service returned no audio"
            raise RuntimeError("Speech synthesis produced no audio.")
        self._tts_error = None
        self._tts_ok_at = time.time()
        return audio

    async def _voice_name(self) -> str:
        voice_settings = await self.settings_store.get("voice", {})
        return voice_settings.get("voice") or self.settings.TTS_VOICE

    async def voices(self, language: str = "en") -> List[Dict[str, Any]]:
        try:
            import edge_tts
        except ImportError:
            return []
        try:
            catalogue = await edge_tts.list_voices()
        except Exception as exc:
            logger.warning("could not list voices: %s", exc)
            return []
        return [
            {
                "name": v["ShortName"],
                "label": v.get("FriendlyName", v["ShortName"]),
                "gender": v.get("Gender", ""),
                "locale": v.get("Locale", ""),
            }
            for v in catalogue
            if not language or v.get("Locale", "").lower().startswith(language.lower())
        ]

    # ==================================================================
    # speech to text
    # ==================================================================
    async def transcribe(self, audio_bytes: bytes, filename: str = "audio.webm") -> Dict[str, Any]:
        model = await self._get_whisper()
        if model is None:
            raise RuntimeError(
                self._whisper_error
                or "Server-side transcription needs 'faster-whisper' (pip install faster-whisper). "
                   "Browser speech recognition works without it."
            )

        suffix = Path(filename).suffix or ".webm"
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(audio_bytes)
            temp_path = handle.name

        def _run() -> Dict[str, Any]:
            segments, info = model.transcribe(temp_path, language="en", beam_size=1,
                                              condition_on_previous_text=False, vad_filter=True)
            pieces = [segment.text for segment in segments]
            return {
                "text": "".join(pieces).strip(),
                "language": getattr(info, "language", "en"),
                "duration": getattr(info, "duration", 0),
            }

        try:
            result = await asyncio.get_event_loop().run_in_executor(None, _run)
        finally:
            Path(temp_path).unlink(missing_ok=True)

        logger.info("transcribed %.1fs of audio", result.get("duration", 0))
        return result

    async def preload(self) -> None:
        """Warm the speech-to-text model in the background.

        Loading on first use means the first thing anyone says to the wake
        word disappears into a model download -- 141 MB, on a cold cache,
        while the interface says "Transcribing...". Doing it at startup costs
        nothing the user waits for.
        """
        try:
            await self._get_whisper()
        except Exception:
            logger.debug("whisper preload failed", exc_info=True)

    @property
    def stt_ready(self) -> bool:
        return self._whisper is not None

    @property
    def stt_problem(self) -> Optional[str]:
        return self._whisper_error

    async def transcribe_samples(self, samples) -> str:
        """Transcribe 16 kHz mono float32 samples that are already in memory.

        `transcribe()` takes encoded audio from the browser and round-trips it
        through a temporary file. The wake-word capture already holds raw PCM,
        so writing it to disk purely to read it back would be both slower and
        a recording of the user that there is no reason to create.
        """
        model = await self._get_whisper()
        loop = asyncio.get_running_loop()

        def _run() -> str:
            segments, _info = model.transcribe(
                samples, language="en", beam_size=1,
                vad_filter=False,            # the capture already did this
                condition_on_previous_text=False,
            )
            return " ".join(segment.text for segment in segments).strip()

        return await loop.run_in_executor(None, _run)

    async def _get_whisper(self):
        if self._whisper is not None:
            return self._whisper
        if self._whisper_error:
            return None
        if self._loading:
            for _ in range(120):
                await asyncio.sleep(0.5)
                if self._whisper is not None or self._whisper_error:
                    break
            return self._whisper

        self._loading = True
        try:
            def _load():
                from faster_whisper import WhisperModel

                return WhisperModel(self.settings.WHISPER_MODEL, device="cpu", compute_type="int8")

            logger.info("loading Whisper '%s' (first use only)", self.settings.WHISPER_MODEL)
            self._whisper = await asyncio.get_event_loop().run_in_executor(None, _load)
            logger.info("Whisper ready")
            await self.activity.record(
                f"Speech recognition model '{self.settings.WHISPER_MODEL}' loaded.", source="voice"
            )
        except ImportError as exc:
            self._whisper_error = (
                f"faster-whisper is not installed ({exc}). Browser speech recognition still works."
            )
            logger.warning(self._whisper_error)
        except Exception as exc:
            self._whisper_error = f"Whisper failed to load: {exc}"
            logger.exception("whisper load failed")
        finally:
            self._loading = False
        return self._whisper

    # ==================================================================
    # presence signalling -- drives the particle field
    # ==================================================================
    def speaking_started(self, text: str = "") -> None:
        """The browser telling us it has started speaking.

        Tagged as browser-origin. This event is also used by automations to
        *ask* the browser to speak, and without the tag the browser cannot
        tell the two apart: it hears its own report, speaks again, reports
        again, and loops forever.
        """
        self._speaking = True
        # Kept, not just broadcast. The wake-word detector needs to know what
        # is coming out of the speakers to tell "JARVIS said its own name"
        # apart from "the user spoke over it" -- which is the whole of
        # barge-in.
        self.speaking_text = text or ""
        bus.publish(Events.SPEAKING_START,
                    {"text": truncate(text, 400), "source": "browser"}, source="voice")

    def speaking_ended(self, interrupted: bool = False) -> None:
        self._speaking = False
        self.speaking_ended_at = time.time()
        self.speaking_text = ""
        bus.publish(Events.SPEAKING_END, {"interrupted": interrupted}, source="voice")

    def listening_started(self) -> None:
        bus.publish(Events.LISTENING_START, {}, source="voice")

    def listening_ended(self, transcript: str = "") -> None:
        bus.publish(Events.LISTENING_END, {"transcript": truncate(transcript, 400)}, source="voice")

    @property
    def speaking(self) -> bool:
        return self._speaking

    # ==================================================================
    async def _packages(self) -> "tuple[bool, bool]":
        """Are the speech packages importable? Answered once, off the loop.

        Importing them is not cheap -- measured here, `faster_whisper` takes
        21 seconds and `openwakeword` 13.7 -- and this used to do it inline
        during boot. That is 35 seconds of a *blocked event loop*, so uvicorn
        could not answer its own health check and the launcher gave up and
        killed a backend that was starting perfectly well.

        An import either works or it does not, so the answer is cached, and
        the first one happens in a worker thread where stalling costs nothing
        but time.
        """
        if self._packages_checked is not None:
            return self._packages_checked

        def _probe() -> "tuple[bool, bool]":
            tts = stt = False
            try:
                import edge_tts  # noqa: F401
                tts = True
            except Exception:
                pass
            try:
                import faster_whisper  # noqa: F401
                stt = True
            except Exception:
                pass
            return tts, stt

        loop = asyncio.get_running_loop()
        self._packages_checked = await loop.run_in_executor(None, _probe)
        return self._packages_checked

    async def health(self) -> Dict[str, Any]:
        voice_settings = await self.settings_store.get("voice", {})
        if not voice_settings.get("enabled", True):
            return {"status": "degraded", "detail": "voice disabled in settings", "checked_at": now_iso()}

        has_tts, has_stt = await self._packages()

        engine = voice_settings.get("tts_engine", "edge")
        if engine == "browser":
            return {"status": "online", "detail": "browser speech synthesis", "checked_at": now_iso()}
        if self._tts_error:
            return {"status": "degraded",
                    "detail": "edge-tts is installed but failing ({}); the browser voice is "
                              "being used instead".format(self._tts_error),
                    "checked_at": now_iso()}
        if has_tts and has_stt:
            return {"status": "online", "detail": "edge-tts + faster-whisper available",
                    "checked_at": now_iso()}
        if has_tts or has_stt:
            missing = "faster-whisper" if has_tts else "edge-tts"
            return {"status": "degraded", "detail": f"{missing} not installed; browser fallback in use",
                    "checked_at": now_iso()}
        return {"status": "degraded", "detail": "server voice packages not installed; using the browser",
                "checked_at": now_iso()}
