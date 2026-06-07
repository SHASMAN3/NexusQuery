"""
api/dependencies.py
--------------------
FastAPI dependency injection providers.
Singletons are created once at startup and reused across requests.
"""

from __future__ import annotations

from functools import lru_cache

from rag.pipeline import RAGPipeline
from ingestion.vector_store import MongoVectorStore


@lru_cache(maxsize=1)
def get_rag_pipeline() -> RAGPipeline:
    """Return the singleton RAGPipeline instance."""
    return RAGPipeline()


@lru_cache(maxsize=1)
def get_vector_store() -> MongoVectorStore:
    """Return the singleton MongoVectorStore instance."""
    return MongoVectorStore()