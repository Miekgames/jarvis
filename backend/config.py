"""
Central configuration for JARVIS.

Every value is read from the environment (loaded from the project-root .env).
Nothing secret is ever hardcoded here, and nothing secret is ever sent to the
frontend -- see `public_settings()` for the subset the UI is allowed to see.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path
from typing import Any, Dict, List, Optional

# ---------------------------------------------------------------------------
# .env loading (python-dotenv if available, otherwise a tiny built-in parser so
# the app still boots on a bare interpreter)
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent


def _load_env(path: Path) -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(path)
        return
    except ImportError:
        pass
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


_load_env(ROOT / ".env")


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")



def _ollama_url(raw: str) -> str:
    """Turn whatever OLLAMA_HOST holds into a URL a client can actually use.

    Accepts "0.0.0.0", "localhost:11434", "http://box:11434" and "" alike.
    A wildcard bind address means "listening everywhere", so as a client
    destination it is rewritten to loopback rather than failing at request time.
    """
    value = (raw or "").strip()
    if not value:
        return "http://127.0.0.1:11434"
    if "://" not in value:
        value = "http://" + value
    scheme, _, remainder = value.partition("://")
    hostport, slash, path = remainder.partition("/")

    # Check the whole authority first: "::" and "[::]" contain colons, so
    # splitting on ':' before testing for a wildcard mangles them.
    wildcards = ("0.0.0.0", "::", "[::]", "*")
    if hostport in wildcards:
        host, port = "127.0.0.1", "11434"
    else:
        host, colon, port = hostport.rpartition(":")
        if not colon:                      # no port given
            host, port = hostport, "11434"
        if host in wildcards:
            host = "127.0.0.1"
        if not port.isdigit():
            host, port = hostport, "11434"

    return "{}://{}:{}{}{}".format(scheme, host, port, slash, path)


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except ValueError:
        return default


def _list(name: str, default: str) -> List[str]:
    """A comma-separated environment variable, falling back on the default.

    A variable that is *present but empty* falls back too, which is the whole
    point of the second read. `os.getenv` only returns the default when the
    name is absent, so a bare `JARVIS_FILE_ROOTS=` line in .env -- the way
    anyone writes "leave this one alone" -- came back as "" and produced an
    empty list rather than the intended default.

    That is not a harmless difference. An empty FILE_ROOTS is not "no
    restriction", it is "no folder is permitted", and every file tool then
    failed: writing a file by a relative name crashed on `roots[0]` with
    `IndexError: list index out of range`, which tells the person nothing
    about the blank line in their configuration that caused it.
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        raw = default
    return [p.strip() for p in raw.split(",") if p.strip()]


class Settings:
    """Immutable process configuration. Runtime-editable preferences live in the
    database (see `backend.db.settings_store`), not here."""

    # -- paths ---------------------------------------------------------------
    ROOT: Path = ROOT
    DATA_DIR: Path = Path(os.getenv("JARVIS_DATA_DIR", str(ROOT / "data")))
    BACKUP_DIR: Path = Path(os.getenv("JARVIS_BACKUP_DIR", str(ROOT / "backups")))
    DB_PATH: Path = DATA_DIR / "jarvis.db"

    # -- server --------------------------------------------------------------
    HOST: str = os.getenv("JARVIS_HOST", "127.0.0.1")
    PORT: int = _int("JARVIS_PORT", 8000)
    FRONTEND_PORT: int = _int("JARVIS_FRONTEND_PORT", 3000)
    CORS_ORIGINS: List[str] = _list(
        "JARVIS_CORS_ORIGINS",
        "http://localhost:3000,http://127.0.0.1:3000,http://localhost:8000,http://127.0.0.1:8000",
    )

    # -- security ------------------------------------------------------------
    # "local"    -> loopback clients are trusted and auto-issued an owner session
    # "password" -> everybody must POST /api/auth/login with JARVIS_PASSWORD
    AUTH_MODE: str = os.getenv("JARVIS_AUTH_MODE", "local").strip().lower()
    PASSWORD: str = os.getenv("JARVIS_PASSWORD", "")
    SECRET_KEY: str = os.getenv("JARVIS_SECRET_KEY", "")
    SESSION_HOURS: int = _int("JARVIS_SESSION_HOURS", 24 * 14)

    # -- AI providers --------------------------------------------------------
    # "ollama"  -- local only. Nothing is sent to a cloud API, ever, and no
    #              cloud provider is registered even if a key is present.
    # "auto"    -- cloud first (needs ANTHROPIC_API_KEY), local as fallback.
    # Local-only is the default: JARVIS runs entirely on this machine unless
    # you deliberately opt into a cloud provider.
    AI_PROVIDER: str = os.getenv("JARVIS_AI_PROVIDER", "auto").strip().lower()
    ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")
    OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
    # JARVIS_OLLAMA_HOST wins over OLLAMA_HOST on purpose. OLLAMA_HOST is
    # Ollama's own *server bind* variable and is very commonly set to
    # "0.0.0.0", which is a valid address to listen on and a useless one to
    # connect to. _ollama_url repairs that and supplies a missing scheme.
    OLLAMA_HOST: str = _ollama_url(
        os.getenv("JARVIS_OLLAMA_HOST") or os.getenv("OLLAMA_HOST", ""))
    # Empty means "pick whichever installed model looks best" at first use.
    OLLAMA_MODEL: str = os.getenv("JARVIS_OLLAMA_MODEL", "")
    # Per-purpose local overrides. Anything left blank is chosen automatically
    # from the capabilities Ollama reports for each installed model.
    OLLAMA_ROUTING: Dict[str, str] = {
        "conversation": os.getenv("JARVIS_OLLAMA_CONVERSATION", ""),
        "fast": os.getenv("JARVIS_OLLAMA_FAST", ""),
        "reasoning": os.getenv("JARVIS_OLLAMA_REASONING", ""),
        "coding": os.getenv("JARVIS_OLLAMA_CODING", ""),
        "vision": os.getenv("JARVIS_OLLAMA_VISION", ""),
    }

    # Model routing. Each role can be overridden independently.
    MODEL_PRIMARY: str = os.getenv("JARVIS_MODEL_PRIMARY", "claude-opus-5-5")
    MODEL_FAST: str = os.getenv("JARVIS_MODEL_FAST", "claude-haiku-5-5")
    MODEL_REASONING: str = os.getenv("JARVIS_MODEL_REASONING", "claude-opus-5-5")
    MODEL_CODING: str = os.getenv("JARVIS_MODEL_CODING", "claude-opus-5-5")
    MODEL_VISION: str = os.getenv("JARVIS_MODEL_VISION", "claude-opus-5-5")
    MAX_TOKENS: int = _int("JARVIS_MAX_TOKENS", 4096)

    # Self-editing uses a model of its own, kept separate from the one
    # holding the conversation. Blank = route to the coding model.
    SELFEDIT_PROVIDER: str = os.getenv("JARVIS_SELFEDIT_PROVIDER", "")
    SELFEDIT_MODEL: str = os.getenv("JARVIS_SELFEDIT_MODEL", "")
    # How much of a conversation the model may hold at once, in tokens.
    #
    # Ollama does not use a model's full context unless told to: left alone it
    # applies a modest default and silently drops everything past it. qwen2.5
    # advertises 32768 and was effectively getting a few thousand, so JARVIS
    # lost the thread of long conversations no matter how much history was
    # sent to it.
    #
    # The cost is memory -- the key/value cache grows with this, roughly half
    # a gigabyte at 8192 for a 7B model -- so it is a dial, not simply the
    # model's maximum. Raise it if you have the RAM and want longer memory.
    OLLAMA_NUM_CTX: int = _int("JARVIS_OLLAMA_NUM_CTX", 16384)
    # How long Ollama keeps the model in memory. "-1" holds it indefinitely.
    # Measured on this machine: resident 0.30s per reply, cold 12.18s, of
    # which 11.98s is reloading the weights. Ollama's own default is five
    # minutes, so any ordinary pause lands the next reply on the slow side.
    OLLAMA_KEEP_ALIVE: str = os.getenv("JARVIS_OLLAMA_KEEP_ALIVE", "-1").strip()

    # Messages of history to send. Deliberately generous now that the context
    # window above is large enough to hold them; the orchestrator trims to fit
    # rather than relying on this number alone.
    MAX_HISTORY_TURNS: int = _int("JARVIS_MAX_HISTORY_TURNS", 120)

    # -- task engine ---------------------------------------------------------
    WORKER_COUNT: int = _int("JARVIS_WORKERS", 3)
    TASK_MAX_RETRIES: int = _int("JARVIS_TASK_MAX_RETRIES", 3)
    SCHEDULER_TICK_SECONDS: int = _int("JARVIS_SCHEDULER_TICK", 20)
    METRICS_INTERVAL_SECONDS: int = _int("JARVIS_METRICS_INTERVAL", 3)
    METRICS_RETENTION_HOURS: int = _int("JARVIS_METRICS_RETENTION_HOURS", 48)

    # -- voice ---------------------------------------------------------------
    # base.en, not base: the English-only model is more accurate on
    # English for the same size, and multilingual buys nothing here.
    WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "base.en")
    TTS_VOICE: str = os.getenv("JARVIS_TTS_VOICE", "en-GB-RyanNeural")
    WAKE_WORD: str = os.getenv("JARVIS_WAKE_WORD", "jarvis")
    PICOVOICE_ACCESS_KEY: str = os.getenv("PICOVOICE_ACCESS_KEY", "")

    # -- filesystem sandbox --------------------------------------------------
    # Tools that touch the filesystem are confined to these roots.
    FILE_ROOTS: List[str] = _list(
        "JARVIS_FILE_ROOTS",
        str(Path.home()),
    )

    # -- integrations (all optional; absence is reported honestly in the UI) --
    GOOGLE_CLIENT_ID: str = os.getenv("GOOGLE_CLIENT_ID", "")
    GOOGLE_CLIENT_SECRET: str = os.getenv("GOOGLE_CLIENT_SECRET", "")
    GITHUB_TOKEN: str = os.getenv("GITHUB_TOKEN", "")
    HOME_ASSISTANT_URL: str = os.getenv("HOME_ASSISTANT_URL", "")
    HOME_ASSISTANT_TOKEN: str = os.getenv("HOME_ASSISTANT_TOKEN", "")
    SPOTIFY_CLIENT_ID: str = os.getenv("SPOTIFY_CLIENT_ID", "")
    SPOTIFY_CLIENT_SECRET: str = os.getenv("SPOTIFY_CLIENT_SECRET", "")
    WEATHER_LOCATION: str = os.getenv("JARVIS_WEATHER_LOCATION", "")
    # Which region web search asks for.
    #
    # Not "wt-wt" (DuckDuckGo's "no region"), tempting though it is: the
    # search library turns the language half of the region into a Wikipedia
    # subdomain, and `wt.wikipedia.org` does not exist -- so every single
    # search began with a DNS lookup that could never succeed. A real region
    # is also about twice as fast.
    SEARCH_REGION: str = os.getenv("JARVIS_SEARCH_REGION", "us-en")

    # -- misc ----------------------------------------------------------------
    OWNER_NAME: str = os.getenv("JARVIS_OWNER_NAME", "")
    ASSISTANT_NAME: str = os.getenv("JARVIS_ASSISTANT_NAME", "JARVIS")
    LOG_LEVEL: str = os.getenv("JARVIS_LOG_LEVEL", "INFO").upper()
    TIMEZONE: str = os.getenv("JARVIS_TIMEZONE", "")
    PRIVACY_MODE: bool = _bool("JARVIS_PRIVACY_MODE", False)

    def __init__(self) -> None:
        self.DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        (self.DATA_DIR / "screenshots").mkdir(exist_ok=True)
        (self.DATA_DIR / "exports").mkdir(exist_ok=True)
        self.SECRET_KEY = self._ensure_secret()

    # ------------------------------------------------------------------
    def _ensure_secret(self) -> str:
        """Session-signing key. Generated once and persisted with 0600-ish perms
        so restarts don't invalidate every session."""
        if self.SECRET_KEY:
            return self.SECRET_KEY
        keyfile = self.DATA_DIR / ".secret_key"
        if keyfile.exists():
            value = keyfile.read_text(encoding="utf-8").strip()
            if value:
                return value
        value = secrets.token_hex(32)
        keyfile.write_text(value, encoding="utf-8")
        try:
            os.chmod(keyfile, 0o600)
        except OSError:
            pass  # best-effort on Windows
        return value

    # ------------------------------------------------------------------
    @property
    def local_only(self) -> bool:
        """True when every model call must go to Ollama."""
        return self.AI_PROVIDER != "auto"

    def has_ai_provider(self) -> bool:
        """Local-only always has a provider — Ollama. Its reachability is a
        health question, checked at runtime, not a configuration one."""
        if self.local_only:
            return True
        return bool(self.ANTHROPIC_API_KEY or self.OPENAI_API_KEY)

    def public_settings(self) -> Dict[str, Any]:
        """Non-secret configuration safe to expose to the browser."""
        return {
            "assistant_name": self.ASSISTANT_NAME,
            "owner_name": self.OWNER_NAME,
            "auth_mode": self.AUTH_MODE,
            "frontend_port": self.FRONTEND_PORT,
            "backend_port": self.PORT,
            "wake_word": self.WAKE_WORD,
            "tts_voice": self.TTS_VOICE,
            "privacy_mode": self.PRIVACY_MODE,
            "file_roots": self.FILE_ROOTS,
            "models": {
                "primary": self.MODEL_PRIMARY,
                "fast": self.MODEL_FAST,
                "reasoning": self.MODEL_REASONING,
                "coding": self.MODEL_CODING,
                "vision": self.MODEL_VISION,
            },
            "ai_provider": self.AI_PROVIDER,
            "local_only": self.local_only,
            "providers": {
                "anthropic": bool(self.ANTHROPIC_API_KEY) and not self.local_only,
                "openai": bool(self.OPENAI_API_KEY) and not self.local_only,
                "ollama": True,  # probed at runtime by the health service
            },
        }


settings = Settings()


def get_secret(name: str) -> Optional[str]:
    """Read a secret by env name. Used by integrations so no module has to
    import the raw environment itself."""
    value = os.getenv(name, "")
    return value or None
