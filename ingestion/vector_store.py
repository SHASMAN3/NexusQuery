"""
ingestion/vector_store.py
--------------------------
MongoDB vector store for self-hosted MongoDB 7.0+ container.

Search strategy:
  - vector_search()  → $vectorSearch aggregation (native MongoDB 7.0+)
  - text_search()    → $text operator with compound text index
                       (replaces Atlas $search which is cloud-only)
  - hybrid_search()  → RRF fusion of both result lists
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from config.settings import get_settings
from db.mongo_client import get_documents_collection
from ingestion.chunker import DocumentChunk

logger = logging.getLogger(__name__)


class MongoVectorStore:
    """
    Wraps MongoDB collection for vector + hybrid search.

    Document schema stored in MongoDB:
    {
      _id:          chunk_id  (SHA-256 hex)
      content:      str
      embedding:    [float, ...]   (768 dims)
      url:          str
      title:        str
      chunk_index:  int
      total_chunks: int
      crawl_job_id: str
      metadata:     { depth, crawled_at }
    }
    """

    def __init__(self) -> None:
        self._cfg = get_settings()
        self._collection = get_documents_collection(self._cfg)

    # ------------------------------------------------------------------ #
    # Write
    # ------------------------------------------------------------------ #

    async def upsert_chunks(
        self,
        chunk_embeddings: list[tuple[DocumentChunk, list[float]]],
    ) -> int:
        """
        Upsert (chunk, embedding) pairs.
        Uses bulk_write with ReplaceOne + upsert=True keyed on chunk_id.
        Returns the number of documents upserted or modified.
        """
        if not chunk_embeddings:
            return 0

        from pymongo import ReplaceOne

        operations = []
        for chunk, embedding in chunk_embeddings:
            doc = {
                "_id":         chunk.chunk_id,
                "content":     chunk.content,
                "embedding":   embedding,
                "url":         chunk.url,
                "title":       chunk.title,
                "chunk_index": chunk.chunk_index,
                "total_chunks":chunk.total_chunks,
                "crawl_job_id":chunk.crawl_job_id,
                "metadata":    chunk.metadata,
            }
            operations.append(ReplaceOne({"_id": chunk.chunk_id}, doc, upsert=True))

        result = await self._collection.bulk_write(operations, ordered=False)
        count = result.upserted_count + result.modified_count
        logger.info("Upserted %d chunks into MongoDB", count)
        return count

    async def delete_by_job(self, crawl_job_id: str) -> int:
        """Remove all chunks belonging to a crawl job."""
        result = await self._collection.delete_many({"crawl_job_id": crawl_job_id})
        logger.info("Deleted %d chunks for job=%s", result.deleted_count, crawl_job_id)
        return result.deleted_count

    # ------------------------------------------------------------------ #
    # Vector Search  ($vectorSearch — MongoDB 7.0+ native)
    # ------------------------------------------------------------------ #

    async def vector_search(
        self,
        query_embedding: list[float],
        top_k: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        ANN cosine similarity search using $vectorSearch.
        Requires the 'pulse_vector_index' vectorSearch index on the embedding field.
        Returns docs with a `vector_score` field (0.0–1.0).
        """
        k = top_k or self._cfg.retriever_top_k
        pipeline = [
            {
                "$vectorSearch": {
                    "index":        self._cfg.atlas_vector_index,
                    "path":         "embedding",
                    "queryVector":  query_embedding,
                    "numCandidates": self._cfg.vector_num_candidates,
                    "limit":        k,
                }
            },
            {
                "$project": {
                    "_id":        1,
                    "content":    1,
                    "url":        1,
                    "title":      1,
                    "chunk_index":1,
                    "crawl_job_id":1,
                    "vector_score": {"$meta": "vectorSearchScore"},
                }
            },
        ]
        cursor = self._collection.aggregate(pipeline)
        results = await cursor.to_list(length=k)
        logger.debug("$vectorSearch returned %d results", len(results))
        return results

    # ------------------------------------------------------------------ #
    # Full-Text Search  ($text — standard MongoDB, no Atlas needed)
    # ------------------------------------------------------------------ #

    async def text_search(
        self,
        query: str,
        top_k: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Full-text search using MongoDB's built-in $text operator.
        Requires the compound text index on (content, title).
        Returns docs with a `text_score` field (textScore metadata).
        """
        k = top_k or self._cfg.retriever_top_k

        # $text does not support arbitrary pipelines directly — use find() with
        # projection of textScore, then sort, then limit.
        # We convert the cursor to a list manually.
        cursor = self._collection.find(
            {"$text": {"$search": query, "$language": "english"}},
            {
                "_id":        1,
                "content":    1,
                "url":        1,
                "title":      1,
                "chunk_index":1,
                "text_score": {"$meta": "textScore"},
            },
        ).sort([("text_score", {"$meta": "textScore"})]).limit(k)

        results = await cursor.to_list(length=k)
        logger.debug("$text search returned %d results", len(results))
        return results

    # ------------------------------------------------------------------ #
    # Hybrid Search  (vector + text → RRF fusion)
    # ------------------------------------------------------------------ #

    async def hybrid_search(
        self,
        query: str,
        query_embedding: list[float],
        top_k: int | None = None,
    ) -> list[dict[str, Any]]:
        """
        Hybrid search: $vectorSearch + $text fused via Reciprocal Rank Fusion.

        RRF score per document:
            RRF(d) = Σ  weight_i / (k_rrf + rank_i(d))    where k_rrf = 60

        Returns top_k results sorted by descending hybrid_score.
        vector_score is preserved for downstream confidence thresholding.
        """
        k = top_k or self._cfg.retriever_top_k

        # Run both searches concurrently
        vector_results, text_results = await asyncio.gather(
            self.vector_search(query_embedding, top_k=k * 2),
            self.text_search(query, top_k=k * 2),
        )

        fused = self._reciprocal_rank_fusion(
            vector_results,
            text_results,
            vector_weight=self._cfg.hybrid_vector_weight,
            text_weight=self._cfg.hybrid_text_weight,
            rrf_k=60,
        )

        top = fused[:k]
        logger.debug(
            "Hybrid search: vector=%d text=%d → fused top-%d",
            len(vector_results), len(text_results), len(top),
        )
        return top

    # ------------------------------------------------------------------ #
    # RRF
    # ------------------------------------------------------------------ #

    @staticmethod
    def _reciprocal_rank_fusion(
        vector_results: list[dict[str, Any]],
        text_results: list[dict[str, Any]],
        vector_weight: float,
        text_weight: float,
        rrf_k: int = 60,
    ) -> list[dict[str, Any]]:
        """
        Weighted Reciprocal Rank Fusion of two ranked result lists.
        Preserves vector_score from the vector list for confidence thresholding.
        """
        scores: dict[str, float]         = {}
        docs:   dict[str, dict[str, Any]] = {}

        for rank, doc in enumerate(vector_results, start=1):
            doc_id = str(doc["_id"])
            scores[doc_id] = scores.get(doc_id, 0.0) + vector_weight / (rrf_k + rank)
            if doc_id not in docs:
                docs[doc_id] = doc

        for rank, doc in enumerate(text_results, start=1):
            doc_id = str(doc["_id"])
            scores[doc_id] = scores.get(doc_id, 0.0) + text_weight / (rrf_k + rank)
            if doc_id not in docs:
                docs[doc_id] = doc

        sorted_ids = sorted(scores, key=lambda d: scores[d], reverse=True)
        result = []
        for doc_id in sorted_ids:
            doc = dict(docs[doc_id])
            doc["hybrid_score"] = round(scores[doc_id], 6)
            result.append(doc)

        return result

    # ------------------------------------------------------------------ #
    # Stats
    # ------------------------------------------------------------------ #

    async def count_documents(self) -> int:
        return await self._collection.count_documents({})

    async def count_by_job(self, crawl_job_id: str) -> int:
        return await self._collection.count_documents({"crawl_job_id": crawl_job_id})