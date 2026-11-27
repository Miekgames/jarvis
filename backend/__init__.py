"""JARVIS backend package."""
import os

# ---------------------------------------------------------------------------
# Intel OpenMP duplicate-runtime guard.
#
# Several of the audio dependencies ship their own copy of Intel's OpenMP
# runtime: ctranslate2 (behind faster-whisper) and onnxruntime (behind the
# wake-word spotter) both do. When a second copy is loaded into a process that
# already has one, libiomp calls abort() -- it does not warn and continue, it
# kills the process.
#
# On this machine that made `import faster_whisper` fatal on its own, so any
# request that reached speech-to-text took the whole backend down with it.
# Setting this before any of them load is Intel's documented escape hatch.
#
# Their warning about "incorrect results" applies to genuinely mismatched
# runtimes. These are the same build, and transcription was verified correct
# with the flag set, so the real choice here is between a working transcriber
# and a backend that aborts.
#
# It must be set before the first import of any of those packages, which is
# why it lives here rather than in config: `backend` is imported first by
# everything, including `python -m backend`.
os.environ.setdefault("KMP_DUPLICATE_LIB_OK", "TRUE")

__version__ = "1.2.0"
