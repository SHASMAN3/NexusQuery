"""
crawler/url_filter.py
----------------------
Stateful URL filter that handles:
  - Domain scoping (stay within allowed domains)
  - Path prefix allow/exclude patterns
  - Query-string stripping for dedup
  - Fragment removal
  - Robots.txt compliance via robotparser
  - Seen-URL set (in-memory, also persisted via checkpoint)
"""

from __future__ import annotations

import logging
import urllib.robotparser
from urllib.parse import urljoin, urlparse, urlunparse

import aiohttp

from config.settings import get_settings
from crawler.models import CrawlConfig

logger = logging.getLogger(__name__)

# Common non-content extensions to skip
_SKIP_EXTENSIONS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".svg", ".webp", ".ico",
        ".pdf", ".zip", ".tar", ".gz", ".exe", ".dmg", ".pkg",
        ".mp4", ".mp3", ".avi", ".mov", ".css", ".js", ".woff",
        ".woff2", ".ttf", ".eot", ".map", ".xml", ".json",
    }
)


class URLFilter:
    """Thread-safe (asyncio-safe) URL filter for the crawler."""

    def __init__(self, config: CrawlConfig) -> None:
        self.config = config
        self._seen: set[str] = set()
        self._robots_parsers: dict[str, urllib.robotparser.RobotFileParser] = {}
        self._settings = get_settings()

        # Derive allowed domains from the start URL if not explicitly set
        parsed = urlparse(config.start_url)
        self._base_domain = parsed.netloc
        self._allowed_domains: set[str] = set(config.allowed_domains) or {self._base_domain}

    # ------------------------------------------------------------------ #
    # Public interface
    # ------------------------------------------------------------------ #

    def normalise(self, url: str, base_url: str | None = None) -> str | None:
        """
        Normalise a URL:
        - resolve relative URLs against base_url
        - strip fragments
        - strip tracking query params
        - lowercase scheme + host
        Returns None if the URL should be discarded entirely.
        """
        try:
            if base_url:
                url = urljoin(base_url, url)
            parsed = urlparse(url)

            # Only http / https
            if parsed.scheme not in ("http", "https"):
                return None

            # Strip fragment
            normalised = urlunparse(
                (
                    parsed.scheme.lower(),
                    parsed.netloc.lower(),
                    parsed.path.rstrip("/") or "/",
                    "",   # params
                    "",   # query — strip for dedup; revisit if needed
                    "",   # fragment
                )
            )
            return normalised
        except Exception:
            return None

    def is_allowed(self, url: str) -> bool:
        """
        Returns True if the URL should be crawled.
        Checks: scheme, extension, domain, path patterns, seen set.
        Does NOT check robots.txt (async — use `is_robots_allowed` separately).
        """
        parsed = urlparse(url)

        # Extension check
        path_lower = parsed.path.lower()
        if any(path_lower.endswith(ext) for ext in _SKIP_EXTENSIONS):
            return False

        # Domain check
        if parsed.netloc not in self._allowed_domains:
            return False

        # Include-path-prefix filter (if configured)
        if self.config.include_path_prefixes:
            if not any(parsed.path.startswith(p) for p in self.config.include_path_prefixes):
                return False

        # Exclude patterns
        if any(pattern in parsed.path for pattern in self.config.exclude_path_patterns):
            return False

        # Already seen
        if url in self._seen:
            return False

        return True

    def mark_seen(self, url: str) -> None:
        self._seen.add(url)

    def is_seen(self, url: str) -> bool:
        return url in self._seen

    def restore_seen(self, urls: list[str]) -> None:
        """Restore seen set from a checkpoint."""
        self._seen.update(urls)

    @property
    def seen_count(self) -> int:
        return len(self._seen)

    async def is_robots_allowed(
        self,
        url: str,
        session: aiohttp.ClientSession,
    ) -> bool:
        """
        Check robots.txt for the given URL.
        Caches one RobotFileParser per origin.
        """
        if not self.config.respect_robots:
            return True

        parsed = urlparse(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"

        if origin not in self._robots_parsers:
            rp = urllib.robotparser.RobotFileParser()
            robots_url = f"{origin}/robots.txt"
            try:
                async with session.get(
                    robots_url,
                    timeout=aiohttp.ClientTimeout(total=10),
                ) as resp:
                    if resp.status == 200:
                        text = await resp.text()
                        rp.parse(text.splitlines())
                    else:
                        # No robots.txt — allow everything
                        rp.allow_all = True
            except Exception:
                rp.allow_all = True  # Network error — be permissive
            self._robots_parsers[origin] = rp

        rp = self._robots_parsers[origin]
        user_agent = self._settings.crawler_user_agent
        return rp.can_fetch(user_agent, url)

    def extract_links(self, html: str, base_url: str) -> list[str]:
        """
        Extract and normalise all <a href> links from raw HTML.
        Returns only URLs that pass domain/extension checks.
        Does NOT mark as seen.
        """
        from bs4 import BeautifulSoup

        soup = BeautifulSoup(html, "lxml")
        links: list[str] = []

        for tag in soup.find_all("a", href=True):
            href = tag["href"].strip()
            if not href or href.startswith(("mailto:", "tel:", "javascript:")):
                continue
            normalised = self.normalise(href, base_url)
            if normalised and self.is_allowed(normalised):
                links.append(normalised)

        return list(dict.fromkeys(links))  # dedup while preserving order