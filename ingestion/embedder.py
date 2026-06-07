"""
ingestion/embedder.py
----------------------
Thin wrapper around LangChain's GoogleGenerativeAIEmbeddings.
Adds:
  - Configurable batch size to respect API rate limits
  - Exponential-backoff retry on transient errors
  - Logging of throughput
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Sequence

from langchain_google_genai import GoogleGenerativeAIEmbeddings

from config.settings import get_settings
from ingestion.chunker import DocumentChunk

logger = logging.getLogger(__name__)

# Google Embedding API limits (adjust if you have higher quota)
_DEFAULT_BATCH_SIZE = 50
_MAX_RETRIES = 4
_BACKOFF_BASE = 2.0
_BACKOFF_CAP = 60.0


class ChunkEmbedder:
    """
    Embeds DocumentChunks using Google's text-embedding-001 model.
    Returns a parallel list of float vectors.
    """

    def __init__(self) -> None:
        cfg = get_settings()
        self._model = GoogleGenerativeAIEmbeddings(
            model=cfg.embedding_model,
            google_api_key=cfg.google_api_key,
            task_type="retrieval_document",
        )
        self._batch_size = _DEFAULT_BATCH_SIZE
        self._dims = cfg.embedding_dimensions
        logger.debug(
            "ChunkEmbedder init: model=%s dims=%d batch=%d",
            cfg.embedding_model, self._dims, self._batch_size,
        )

    async def embed_chunks(
        self, chunks: list[DocumentChunk]
    ) -> list[tuple[DocumentChunk, list[float]]]:
        """
        Embed all chunks, returning (chunk, embedding) pairs.
        Processes in batches to stay within API rate limits.
        """
        if not chunks:
            return []

        results: list[tuple[DocumentChunk, list[float]]] = []
        total = len(chunks)
        t0 = time.perf_counter()

        for batch_start in range(0, total, self._batch_size):
            batch = chunks[batch_start : batch_start + self._batch_size]
            texts = [c.content for c in batch]

            embeddings = await self._embed_with_retry(texts)

            for chunk, vec in zip(batch, embeddings):
                results.append((chunk, vec))

            logger.debug(
                "Embedded batch %d-%d / %d",
                batch_start + 1,
                min(batch_start + self._batch_size, total),
                total,
            )

        elapsed = time.perf_counter() - t0
        logger.info(
            "Embedded %d chunks in %.2fs (%.1f chunks/s)",
            total, elapsed, total / max(elapsed, 0.001),
        )
        return results

    async def embed_query(self, text: str) -> list[float]:
        """Embed a single query string for retrieval."""
        cfg = get_settings()
        query_model = GoogleGenerativeAIEmbeddings(
            model=cfg.embedding_model,
            google_api_key=cfg.google_api_key,
            task_type="retrieval_query",
        )
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, query_model.embed_query, text)

    async def _embed_with_retry(self, texts: list[str]) -> list[list[float]]:
        """Run embedding with exponential-backoff retry."""
        loop = asyncio.get_event_loop()
        last_exc: Exception | None = None

        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                vecs: list[list[float]] = await loop.run_in_executor(
                    None, self._model.embed_documents, texts
                )
                return vecs
            except Exception as exc:
                last_exc = exc
                if attempt < _MAX_RETRIES:
                    wait = min(_BACKOFF_CAP, _BACKOFF_BASE ** attempt) + random.uniform(0, 1)
                    logger.warning(
                        "Embedding attempt %d/%d failed: %s — retrying in %.1fs",
                        attempt, _MAX_RETRIES, exc, wait,
                    )
                    await asyncio.sleep(wait)

        raise RuntimeError(f"Embedding failed after {_MAX_RETRIES} attempts") from last_exc