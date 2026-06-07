"""
tests/test_chunker.py
"""

from __future__ import annotations

import pytest
from datetime import datetime

from crawler.models import CrawledPage, PageStatus
from ingestion.chunker import DocumentChunk, PageChunker


@pytest.fixture
def chunker():
    return PageChunker()


@pytest.fixture
def sample_page():
    return CrawledPage(
        url="https://docs.example.com/guide",
        title="Getting Started Guide",
        content=(
            "Welcome to our platform. This guide will help you get started.\n\n"
            "Step 1: Create an account\n"
            "Visit the sign-up page and enter your email address and password. "
            "You will receive a confirmation email within a few minutes.\n\n"
            "Step 2: Set up your profile\n"
            "After confirming your email, log in and complete your profile. "
            "Add your name, company, and timezone for the best experience.\n\n"
            "Step 3: Create your first project\n"
            "Navigate to the Projects tab and click 'New Project'. "
            "Give it a name and select a template to get started quickly.\n\n"
            "Step 4: Invite team members\n"
            "Go to Settings > Team and send invitations via email. "
            "Team members will receive an invitation link valid for 7 days.\n\n"
            "Step 5: Explore features\n"
            "Use the left sidebar to navigate between features. "
            "The dashboard shows an overview of your recent activity.\n\n"
        ) * 3,  # repeat to ensure chunking occurs
        raw_html="",
        depth=1,
        status=PageStatus.SUCCESS,
    )


class TestPageChunker:

    def test_returns_chunks(self, chunker, sample_page):
        chunks = chunker.chunk_page(sample_page, "job-001")
        assert len(chunks) > 0
        assert all(isinstance(c, DocumentChunk) for c in chunks)

    def test_chunk_ids_are_unique(self, chunker, sample_page):
        chunks = chunker.chunk_page(sample_page, "job-001")
        ids = [c.chunk_id for c in chunks]
        assert len(ids) == len(set(ids)), "Chunk IDs must be unique"

    def test_chunk_ids_are_deterministic(self, chunker, sample_page):
        chunks1 = chunker.chunk_page(sample_page, "job-001")
        chunks2 = chunker.chunk_page(sample_page, "job-001")
        assert [c.chunk_id for c in chunks1] == [c.chunk_id for c in chunks2]

    def test_metadata_populated(self, chunker, sample_page):
        chunks = chunker.chunk_page(sample_page, "job-001")
        for chunk in chunks:
            assert chunk.url == sample_page.url
            assert chunk.title == sample_page.title
            assert chunk.crawl_job_id == "job-001"
            assert chunk.chunk_index >= 0
            assert chunk.total_chunks == len(chunks)

    def test_chunk_content_not_empty(self, chunker, sample_page):
        chunks = chunker.chunk_page(sample_page, "job-001")
        for chunk in chunks:
            assert len(chunk.content.strip()) > 0

    def test_short_page_returns_empty(self, chunker):
        short_page = CrawledPage(
            url="https://docs.example.com/short",
            title="Short",
            content="Hi",
            raw_html="",
            depth=0,
            status=PageStatus.SUCCESS,
        )
        chunks = chunker.chunk_page(short_page, "job-001")
        assert chunks == []

    def test_chunk_index_sequence(self, chunker, sample_page):
        chunks = chunker.chunk_page(sample_page, "job-001")
        indices = [c.chunk_index for c in chunks]
        assert indices == list(range(len(chunks)))

    def test_different_urls_produce_different_ids(self, chunker):
        page1 = CrawledPage(
            url="https://docs.example.com/page1",
            title="Page 1",
            content="Content for page one " * 100,
            raw_html="",
            depth=0,
            status=PageStatus.SUCCESS,
        )
        page2 = CrawledPage(
            url="https://docs.example.com/page2",
            title="Page 2",
            content="Content for page one " * 100,  # Same content, different URL
            raw_html="",
            depth=0,
            status=PageStatus.SUCCESS,
        )
        chunks1 = chunker.chunk_page(page1, "job")
        chunks2 = chunker.chunk_page(page2, "job")
        # IDs should differ because the URL differs
        assert chunks1[0].chunk_id != chunks2[0].chunk_id