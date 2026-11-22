"""Run the backend: `python -m backend`."""
from __future__ import annotations

import sys

import uvicorn

from .config import settings


def main() -> int:
    reload_enabled = "--reload" in sys.argv
    uvicorn.run(
        "backend.app:app",
        host=settings.HOST,
        port=settings.PORT,
        reload=reload_enabled,
        log_config=None,      # our own logging setup owns formatting
        access_log=False,
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
