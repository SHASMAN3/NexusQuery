"""
ingestion/chunker.py
---------------------
Splits crawled page content into overlapping chunks using LangChain's
RecursiveCharacterTextSplitter. Attaches rich metadata to every chunk
for later retrieval attribution.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

from langchain_text_splitters import RecursiveCharacterTextSplitter

from config.settings import get_settings
from crawler.models import CrawledPage

logger = logging.getLogger(__name__)


@dataclass
class DocumentChunk:
    """A single text chunk ready for embedding and storage."""
    chunk_id: str            # SHA-256 of (url + chunk_index)
    content: str
    chunk_index: int         # 0-based position within the page
    total_chunks: int        # total chunks for this page
    url: str
    title: str
    crawl_job_id: str
    metadata: dict[str, Any] = field(default_factory=dict)


class PageChunker:
    """
    Wraps LangChain RecursiveCharacterTextSplitter.
    Produces DocumentChunk objects with stable, deterministic IDs.
    """

    def __init__(self) -> None:
        cfg = get_settings()
        self._splitter = RecursiveCharacterTextSplitter(
            chunk_size=cfg.chunk_size,
            chunk_overlap=cfg.chunk_overlap,
            length_function=len,
            separators=["\n\n", "\n", ". ", "! ", "? ", " ", ""],
            keep_separator=False,
            strip_whitespace=True,
        )
        self._min_length = cfg.chunk_min_length
        logger.debug(
            "PageChunker init: chunk_size=%d overlap=%d min=%d",
            cfg.chunk_size,
            cfg.chunk_overlap,
            cfg.chunk_min_length,
        )

    def chunk_page(self, page: CrawledPage, crawl_job_id: str) -> list[DocumentChunk]:
        """
        Split a CrawledPage into DocumentChunks.
        Returns an empty list if the page content is too short.
        """
        if len(page.content) < self._min_length:
            logger.debug("Skipping page (too short): %s (%d chars)", page.url, len(page.content))
            return []

        raw_chunks = self._splitter.split_text(page.content)

        # Filter out degenerate chunks
        raw_chunks = [c for c in raw_chunks if len(c.strip()) >= self._min_length]

        if not raw_chunks:
            return []

        chunks: list[DocumentChunk] = []
        total = len(raw_chunks)

        for idx, text in enumerate(raw_chunks):
            chunk_id = self._make_id(page.url, idx)
            chunks.append(
                DocumentChunk(
                    chunk_id=chunk_id,
                    content=text.strip(),
                    chunk_index=idx,
                    total_chunks=total,
                    url=page.url,
                    title=page.title,
                    crawl_job_id=crawl_job_id,
                    metadata={
                        "depth": page.depth,
                        "crawled_at": page.crawled_at.isoformat(),
                    },
                )
            )

        logger.debug("Chunked %s → %d chunks", page.url, total)
        return chunks

    def chunk_pages(
        self, pages: list[CrawledPage], crawl_job_id: str
    ) -> list[DocumentChunk]:
        """Batch-chunk a list of pages."""
        all_chunks: list[DocumentChunk] = []
        for page in pages:
            all_chunks.extend(self.chunk_page(page, crawl_job_id))
        logger.info(
            "Chunked %d pages → %d total chunks (job=%s)",
            len(pages),
            len(all_chunks),
            crawl_job_id,
        )
        return all_chunks

    @staticmethod
    def _make_id(url: str, chunk_index: int) -> str:
        """Deterministic SHA-256 chunk ID for deduplication."""
        raw = f"{url}::{chunk_index}"
        return hashlib.sha256(raw.encode()).hexdigest()