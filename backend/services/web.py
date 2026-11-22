"""
Web search and page reading.

Search uses DuckDuckGo through the `ddgs` package -- no API key, so this works
out of the box. Page text is extracted with a small stdlib HTML parser rather
than pulling in BeautifulSoup, and results are cached briefly so a research
task re-reading the same source doesn't hammer it.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from html import unescape
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional
from urllib.parse import urlparse

logger = logging.getLogger("jarvis.web")

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) JARVIS/1.0 (personal assistant)"
_CACHE_TTL = 600.0

_BLOCKED_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "metadata.google.internal"}


class _TextExtractor(HTMLParser):
    """Strips markup, keeps readable text, records the title."""

    SKIP = {"script", "style", "noscript", "svg", "head", "nav", "footer", "form"}
    BLOCK = {"p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1
        elif tag == "title":
            self._in_title = True
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._in_title:
            self.title += data.strip()
            return
        text = data.strip()
        if text:
            self.parts.append(text + " ")

    def text(self) -> str:
        raw = "".join(self.parts)
        raw = re.sub(r"[ \t]+", " ", raw)
        raw = re.sub(r"\n\s*\n\s*\n+", "\n\n", raw)
        return raw.strip()


class WebService:
    def __init__(self, settings=None):
        self.settings = settings
        self._client = None
        self._cache: Dict[str, Any] = {}

    def _http(self):
        if self._client is None:
            import httpx

            self._client = httpx.AsyncClient(
                timeout=25.0,
                follow_redirects=True,
                headers={"User-Agent": USER_AGENT, "Accept-Language": "en"},
            )
        return self._client

    # ==================================================================
    async def search(self, query: str, max_results: int = 6,
                     region: str = "") -> List[Dict[str, Any]]:
        cache_key = f"search:{query}:{max_results}"
        cached = self._from_cache(cache_key)
        if cached is not None:
            return cached

        # An empty region means "use the configured one", not "worldwide".
        chosen_region = region or getattr(self.settings, "SEARCH_REGION", "us-en")

        def _run() -> List[Dict[str, Any]]:
            try:
                from ddgs import DDGS
            except ImportError:
                try:
                    from duckduckgo_search import DDGS  # type: ignore
                except ImportError:
                    raise RuntimeError(
                        "Web search needs the 'ddgs' package (pip install ddgs)."
                    )
            with DDGS() as engine:
                raw = list(engine.text(query, region=chosen_region,
                                       max_results=max_results))
            return [
                {
                    "title": item.get("title", ""),
                    "url": item.get("href") or item.get("url", ""),
                    "snippet": item.get("body", ""),
                }
                for item in raw
            ]

        try:
            results = await asyncio.get_event_loop().run_in_executor(None, _run)
        except Exception as exc:
            logger.warning("web search failed: %s", exc)
            raise RuntimeError(f"Web search failed: {exc}")

        self._to_cache(cache_key, results)
        return results

    async def news(self, query: str, max_results: int = 8) -> List[Dict[str, Any]]:
        def _run() -> List[Dict[str, Any]]:
            try:
                from ddgs import DDGS
            except ImportError:
                from duckduckgo_search import DDGS  # type: ignore
            with DDGS() as engine:
                raw = list(engine.news(
                    query, region=getattr(self.settings, 'SEARCH_REGION', 'us-en'),
                    max_results=max_results))
            return [
                {
                    "title": item.get("title", ""),
                    "url": item.get("url", ""),
                    "source": item.get("source", ""),
                    "date": item.get("date", ""),
                    "snippet": item.get("body", ""),
                }
                for item in raw
            ]

        try:
            return await asyncio.get_event_loop().run_in_executor(None, _run)
        except Exception as exc:
            raise RuntimeError(f"News search failed: {exc}")

    # ==================================================================
    async def fetch(self, url: str, max_chars: int = 8000) -> Dict[str, Any]:
        """Fetch a page and return readable text. Never raises -- returns
        `{"ok": False, "error": ...}` so callers can record failures."""
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        parsed = urlparse(url)
        if parsed.hostname in _BLOCKED_HOSTS or (parsed.hostname or "").endswith(".local"):
            return {"ok": False, "url": url, "error": "Refusing to fetch a local/internal address."}

        cache_key = f"fetch:{url}:{max_chars}"
        cached = self._from_cache(cache_key)
        if cached is not None:
            return cached

        try:
            response = await self._http().get(url)
            response.raise_for_status()
        except Exception as exc:
            return {"ok": False, "url": url, "error": f"{type(exc).__name__}: {exc}"}

        content_type = response.headers.get("content-type", "")
        if "html" not in content_type and "text" not in content_type:
            return {
                "ok": False,
                "url": url,
                "error": f"Unsupported content type '{content_type}'. I can only read text and HTML pages.",
            }

        parser = _TextExtractor()
        try:
            parser.feed(response.text)
        except Exception:
            pass
        text = parser.text()

        result = {
            "ok": True,
            "url": str(response.url),
            "title": unescape(parser.title)[:200],
            "text": text[:max_chars],
            "truncated": len(text) > max_chars,
            "chars": len(text),
        }
        self._to_cache(cache_key, result)
        return result

    async def fetch_raw(self, url: str, max_bytes: int = 200_000) -> Dict[str, Any]:
        """A text file as it is -- markdown, code, YAML -- without the
        HTML-to-text conversion `fetch` applies. Same refusal of local and
        internal addresses. Never raises."""
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "url": url, "error": "Only http and https addresses."}
        parsed = urlparse(url)
        if parsed.hostname in _BLOCKED_HOSTS or (parsed.hostname or "").endswith(".local"):
            return {"ok": False, "url": url, "error": "Refusing to fetch a local/internal address."}
        try:
            response = await self._http().get(url)
            response.raise_for_status()
        except Exception as exc:
            return {"ok": False, "url": url, "error": f"{type(exc).__name__}: {exc}"}
        content_type = response.headers.get("content-type", "")
        if content_type and not any(kind in content_type for kind in
                                    ("text", "markdown", "yaml", "json", "octet-stream")):
            return {"ok": False, "url": url, "error": f"Not a text file ({content_type})."}
        body = response.content[:max_bytes]
        return {"ok": True, "url": str(response.url),
                "text": body.decode("utf-8", errors="replace"),
                "truncated": len(response.content) > max_bytes}

    # ==================================================================
    def _from_cache(self, key: str) -> Optional[Any]:
        entry = self._cache.get(key)
        if entry and time.monotonic() - entry[0] < _CACHE_TTL:
            return entry[1]
        return None

    def _to_cache(self, key: str, value: Any) -> None:
        self._cache[key] = (time.monotonic(), value)
        if len(self._cache) > 200:
            oldest = sorted(self._cache.items(), key=lambda kv: kv[1][0])[:50]
            for stale_key, _ in oldest:
                self._cache.pop(stale_key, None)

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
