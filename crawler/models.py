"""
crawler/models.py
-----------------
Dataclasses for the crawler subsystem.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class PageStatus(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    ROBOTS_BLOCKED = "robots_blocked"


@dataclass
class CrawlConfig:
    """Runtime configuration for a single crawl job."""
    start_url: str
    job_id: str
    max_depth: int = 3
    max_pages: int = 500
    allowed_domains: list[str] = field(default_factory=list)
    # URL path prefixes to include (empty = all)
    include_path_prefixes: list[str] = field(default_factory=list)
    # URL path substrings to exclude (e.g. "/login", "/admin")
    exclude_path_patterns: list[str] = field(default_factory=list)
    respect_robots: bool = True
    checkpoint_dir: str = "data/checkpoints"


@dataclass
class CrawledPage:
    """Result of crawling a single page."""
    url: str
    title: str
    content: str          # cleaned text content
    raw_html: str         # kept briefly; not persisted
    depth: int
    status: PageStatus
    status_code: Optional[int] = None
    error: Optional[str] = None
    crawled_at: datetime = field(default_factory=datetime.utcnow)
    links_found: int = 0


@dataclass
class CrawlSummary:
    """Returned when a crawl job completes."""
    job_id: str
    target_url: str
    pages_crawled: int
    pages_failed: int
    pages_skipped: int
    elapsed_seconds: float
    started_at: datetime
    completed_at: datetime