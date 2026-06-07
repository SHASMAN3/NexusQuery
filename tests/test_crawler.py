"""
tests/test_crawler.py
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from crawler.models import CrawlConfig, PageStatus
from crawler.url_filter import URLFilter
from crawler.async_crawler import AsyncCrawler


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #

@pytest.fixture
def basic_config():
    return CrawlConfig(
        start_url="https://docs.example.com/",
        job_id="test-job-001",
        max_depth=2,
        max_pages=10,
    )


@pytest.fixture
def url_filter(basic_config):
    return URLFilter(basic_config)


# ------------------------------------------------------------------ #
# URLFilter — normalise
# ------------------------------------------------------------------ #

class TestURLFilterNormalise:

    def test_strips_fragment(self, url_filter):
        result = url_filter.normalise("https://docs.example.com/page#section")
        assert result == "https://docs.example.com/page"

    def test_strips_query(self, url_filter):
        result = url_filter.normalise("https://docs.example.com/page?utm_source=google")
        assert result == "https://docs.example.com/page"

    def test_resolves_relative(self, url_filter):
        result = url_filter.normalise("/about", "https://docs.example.com/")
        assert result == "https://docs.example.com/about"

    def test_rejects_mailto(self, url_filter):
        result = url_filter.normalise("mailto:test@example.com")
        assert result is None

    def test_rejects_javascript(self, url_filter):
        result = url_filter.normalise("javascript:void(0)")
        assert result is None

    def test_lowercase_scheme_host(self, url_filter):
        result = url_filter.normalise("HTTPS://DOCS.EXAMPLE.COM/page")
        assert result is not None
        assert result.startswith("https://docs.example.com")

    def test_trailing_slash_stripped(self, url_filter):
        result = url_filter.normalise("https://docs.example.com/page/")
        assert not result.endswith("/") or result == "https://docs.example.com/"


# ------------------------------------------------------------------ #
# URLFilter — is_allowed
# ------------------------------------------------------------------ #

class TestURLFilterIsAllowed:

    def test_same_domain_allowed(self, url_filter):
        assert url_filter.is_allowed("https://docs.example.com/guide") is True

    def test_external_domain_blocked(self, url_filter):
        assert url_filter.is_allowed("https://otherdomain.com/page") is False

    def test_image_extension_blocked(self, url_filter):
        assert url_filter.is_allowed("https://docs.example.com/logo.png") is False

    def test_pdf_blocked(self, url_filter):
        assert url_filter.is_allowed("https://docs.example.com/file.pdf") is False

    def test_seen_url_blocked(self, url_filter):
        url_filter.mark_seen("https://docs.example.com/seen")
        assert url_filter.is_allowed("https://docs.example.com/seen") is False

    def test_unseen_url_allowed(self, url_filter):
        assert url_filter.is_allowed("https://docs.example.com/new-page") is True

    def test_exclude_pattern(self):
        config = CrawlConfig(
            start_url="https://docs.example.com/",
            job_id="test",
            exclude_path_patterns=["/admin", "/login"],
        )
        f = URLFilter(config)
        assert f.is_allowed("https://docs.example.com/admin/users") is False
        assert f.is_allowed("https://docs.example.com/docs") is True


# ------------------------------------------------------------------ #
# AsyncCrawler — content extraction
# ------------------------------------------------------------------ #

class TestContentExtraction:

    def test_strips_nav_footer(self, basic_config):
        crawler = AsyncCrawler(basic_config)
        html = """
        <html>
        <head><title>Test Page</title></head>
        <body>
          <nav>Navigation links</nav>
          <main><h1>Main Content</h1><p>This is the real content.</p></main>
          <footer>Footer text</footer>
        </body>
        </html>
        """
        title, content = crawler._extract_content(html)
        assert title == "Test Page"
        assert "Main Content" in content
        assert "This is the real content" in content
        assert "Navigation links" not in content
        assert "Footer text" not in content

    def test_extracts_title(self, basic_config):
        crawler = AsyncCrawler(basic_config)
        html = "<html><head><title>My Doc Page</title></head><body><p>Content</p></body></html>"
        title, _ = crawler._extract_content(html)
        assert title == "My Doc Page"

    def test_removes_scripts(self, basic_config):
        crawler = AsyncCrawler(basic_config)
        html = """
        <html><body>
        <script>alert('xss')</script>
        <p>Clean content here</p>
        </body></html>
        """
        _, content = crawler._extract_content(html)
        assert "alert" not in content
        assert "Clean content" in content

    def test_collapses_blank_lines(self, basic_config):
        crawler = AsyncCrawler(basic_config)
        html = "<html><body><p>Line 1</p><p></p><p></p><p>Line 2</p></body></html>"
        _, content = crawler._extract_content(html)
        # Should not have multiple consecutive blank lines
        assert "\n\n\n" not in content


# ------------------------------------------------------------------ #
# Backoff
# ------------------------------------------------------------------ #

class TestBackoff:

    def test_backoff_within_range(self, basic_config):
        crawler = AsyncCrawler(basic_config)
        for attempt in range(1, 4):
            wait = crawler._backoff(attempt)
            assert 0 <= wait <= 30.0

    def test_backoff_capped(self, basic_config):
        crawler = AsyncCrawler(basic_config)
        wait = crawler._backoff(100)  # Very high attempt
        assert wait <= 30.0