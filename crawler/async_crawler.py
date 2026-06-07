"""
crawler/async_crawler.py
-------------------------
Production-grade async web crawler using aiohttp + BeautifulSoup.

Features:
  - Semaphore-bounded concurrency (configurable)
  - Exponential backoff with full jitter on retries
  - robots.txt compliance
  - BFS queue with depth tracking
  - Checkpoint save every N pages for crash recovery
  - Clean text extraction (strips nav/footer/scripts)
  - MySQL crawl_job status updates
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from datetime import datetime
from typing import AsyncGenerator

import aiohttp
from bs4 import BeautifulSoup

from config.settings import get_settings
from crawler.checkpoint import CrawlCheckpoint
from crawler.models import CrawlConfig, CrawledPage, CrawlSummary, PageStatus
from crawler.url_filter import URLFilter

logger = logging.getLogger(__name__)

# Save checkpoint every this many pages
_CHECKPOINT_INTERVAL = 25


class AsyncCrawler:
    """
    Async BFS web crawler.

    Usage:
        config = CrawlConfig(start_url="https://docs.example.com", job_id="abc")
        crawler = AsyncCrawler(config)
        async for page in crawler.crawl():
            await ingest_page(page)
        summary = crawler.summary()
    """

    def __init__(self, config: CrawlConfig) -> None:
        self.config = config
        self._settings = get_settings()
        self._url_filter = URLFilter(config)
        self._checkpoint = CrawlCheckpoint(config.job_id, config.checkpoint_dir)
        self._semaphore = asyncio.Semaphore(self._settings.crawler_max_concurrency)

        # Stats
        self._pages_crawled = 0
        self._pages_failed = 0
        self._pages_skipped = 0
        self._started_at: datetime | None = None
        self._completed_at: datetime | None = None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    async def crawl(self) -> AsyncGenerator[CrawledPage, None]:
        """
        Async generator that yields CrawledPage objects as they are crawled.
        Resumes from checkpoint if one exists.
        """
        self._started_at = datetime.utcnow()
        start_url_normalised = self._url_filter.normalise(self.config.start_url)
        if not start_url_normalised:
            raise ValueError(f"Invalid start URL: {self.config.start_url}")

        # BFS queue: list of (url, depth)
        queue: list[tuple[str, int]] = [(start_url_normalised, 0)]

        # Attempt checkpoint resume
        checkpoint_data = self._checkpoint.load()
        if checkpoint_data:
            seen = checkpoint_data.get("seen_urls", [])
            queue = [(u, d) for u, d in checkpoint_data.get("queue", [])]
            stats = checkpoint_data.get("stats", {})
            self._url_filter.restore_seen(seen)
            self._pages_crawled = stats.get("pages_crawled", 0)
            self._pages_failed = stats.get("pages_failed", 0)
            self._pages_skipped = stats.get("pages_skipped", 0)
            logger.info(
                "Resumed from checkpoint: seen=%d queued=%d already_crawled=%d",
                len(seen), len(queue), self._pages_crawled,
            )

        connector = aiohttp.TCPConnector(
            limit=self._settings.crawler_max_concurrency,
            ttl_dns_cache=300,
            ssl=False,  # set True for strict TLS in prod
        )
        timeout = aiohttp.ClientTimeout(total=self._settings.crawler_request_timeout)
        headers = {"User-Agent": self._settings.crawler_user_agent}

        async with aiohttp.ClientSession(
            connector=connector, timeout=timeout, headers=headers
        ) as session:
            while queue and self._pages_crawled < self.config.max_pages:
                # Drain current queue level concurrently
                batch = queue[: self._settings.crawler_max_concurrency * 2]
                queue = queue[len(batch):]

                tasks = [
                    self._fetch_page(session, url, depth)
                    for url, depth in batch
                ]
                results: list[CrawledPage] = await asyncio.gather(*tasks)

                for page in results:
                    if page.status == PageStatus.SUCCESS:
                        self._pages_crawled += 1
                        yield page

                        # Discover new links if within depth limit
                        if page.depth < self.config.max_depth:
                            new_links = self._url_filter.extract_links(
                                page.raw_html, page.url
                            )
                            for link in new_links:
                                if not self._url_filter.is_seen(link):
                                    queue.append((link, page.depth + 1))
                                    self._url_filter.mark_seen(link)

                    elif page.status == PageStatus.FAILED:
                        self._pages_failed += 1
                    else:
                        self._pages_skipped += 1

                # Checkpoint periodically
                if (self._pages_crawled % _CHECKPOINT_INTERVAL) == 0 and self._pages_crawled > 0:
                    self._save_checkpoint(queue)

        self._completed_at = datetime.utcnow()
        self._checkpoint.delete()
        logger.info(
            "Crawl complete: job=%s crawled=%d failed=%d skipped=%d elapsed=%.1fs",
            self.config.job_id,
            self._pages_crawled,
            self._pages_failed,
            self._pages_skipped,
            (self._completed_at - self._started_at).total_seconds(),
        )

    def summary(self) -> CrawlSummary:
        now = datetime.utcnow()
        started = self._started_at or now
        completed = self._completed_at or now
        return CrawlSummary(
            job_id=self.config.job_id,
            target_url=self.config.start_url,
            pages_crawled=self._pages_crawled,
            pages_failed=self._pages_failed,
            pages_skipped=self._pages_skipped,
            elapsed_seconds=(completed - started).total_seconds(),
            started_at=started,
            completed_at=completed,
        )

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    async def _fetch_page(
        self,
        session: aiohttp.ClientSession,
        url: str,
        depth: int,
    ) -> CrawledPage:
        """Fetch a single page with retry + exponential backoff."""
        self._url_filter.mark_seen(url)

        # Robots check
        if not await self._url_filter.is_robots_allowed(url, session):
            logger.debug("robots.txt blocked: %s", url)
            return CrawledPage(
                url=url, title="", content="", raw_html="",
                depth=depth, status=PageStatus.ROBOTS_BLOCKED,
            )

        last_exc: Exception | None = None
        for attempt in range(1, self._settings.crawler_retry_attempts + 1):
            try:
                async with self._semaphore:
                    return await self._do_fetch(session, url, depth)
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                last_exc = exc
                if attempt < self._settings.crawler_retry_attempts:
                    wait = self._backoff(attempt)
                    logger.warning(
                        "Fetch failed (attempt %d/%d) url=%s err=%s retrying in %.1fs",
                        attempt,
                        self._settings.crawler_retry_attempts,
                        url,
                        exc,
                        wait,
                    )
                    await asyncio.sleep(wait)

        logger.error("Permanently failed: url=%s err=%s", url, last_exc)
        return CrawledPage(
            url=url, title="", content="", raw_html="",
            depth=depth, status=PageStatus.FAILED,
            error=str(last_exc),
        )

    async def _do_fetch(
        self,
        session: aiohttp.ClientSession,
        url: str,
        depth: int,
    ) -> CrawledPage:
        async with session.get(url, allow_redirects=True) as resp:
            if resp.status == 404:
                return CrawledPage(
                    url=url, title="", content="", raw_html="",
                    depth=depth, status=PageStatus.SKIPPED,
                    status_code=404,
                )
            resp.raise_for_status()
            content_type = resp.content_type or ""
            if "text/html" not in content_type:
                return CrawledPage(
                    url=url, title="", content="", raw_html="",
                    depth=depth, status=PageStatus.SKIPPED,
                    status_code=resp.status,
                )

            raw_html = await resp.text(errors="replace")
            title, content = self._extract_content(raw_html)

            if len(content) < self._settings.chunk_min_length:
                return CrawledPage(
                    url=url, title=title, content=content, raw_html="",
                    depth=depth, status=PageStatus.SKIPPED,
                    status_code=resp.status,
                )

            return CrawledPage(
                url=url,
                title=title,
                content=content,
                raw_html=raw_html,
                depth=depth,
                status=PageStatus.SUCCESS,
                status_code=resp.status,
            )

    @staticmethod
    def _extract_content(html: str) -> tuple[str, str]:
        """
        Extract clean text from HTML.
        Removes: <script>, <style>, <nav>, <footer>, <header>, <aside>, <form>
        Returns (title, body_text).
        """
        soup = BeautifulSoup(html, "lxml")

        # Remove noise elements
        for tag in soup(["script", "style", "nav", "footer",
                         "header", "aside", "form", "noscript",
                         "iframe", "svg", "img"]):
            tag.decompose()

        title = ""
        if soup.title and soup.title.string:
            title = soup.title.string.strip()

        # Prefer <main> or <article>, fallback to <body>
        main = soup.find("main") or soup.find("article") or soup.find("body")
        if main:
            text = main.get_text(separator="\n", strip=True)
        else:
            text = soup.get_text(separator="\n", strip=True)

        # Collapse excessive blank lines
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        return title, "\n".join(lines)

    def _backoff(self, attempt: int) -> float:
        """Full-jitter exponential backoff."""
        base = self._settings.crawler_retry_backoff_base
        cap = self._settings.crawler_retry_backoff_max
        ceiling = min(cap, base ** attempt)
        return random.uniform(0, ceiling)

    def _save_checkpoint(self, queue: list[tuple[str, int]]) -> None:
        self._checkpoint.save(
            seen_urls=list(self._url_filter._seen),
            queue=queue,
            stats={
                "pages_crawled": self._pages_crawled,
                "pages_failed": self._pages_failed,
                "pages_skipped": self._pages_skipped,
            },
        )