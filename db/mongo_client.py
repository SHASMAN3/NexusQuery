"""
db/mongo_client.py
------------------
Motor-based async MongoDB client for a self-hosted MongoDB 7.0+ container.

Connection: mongodb://user:pass@localhost:27017/?authSource=admin&directConnection=true

Search strategy:
  - $vectorSearch   → supported natively in MongoDB 7.0+ community (requires a
                       vector search index created via createSearchIndex command)
  - Full-text search → standard MongoDB $text operator with a compound text index
                       (Atlas $search / BM25 is Atlas-only — NOT used here)
"""

from __future__ import annotations

import logging
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection, AsyncIOMotorDatabase
from pymongo import ASCENDING, TEXT, IndexModel
from pymongo.errors import OperationFailure

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

_client: AsyncIOMotorClient | None = None  # type: ignore[type-arg]


def get_mongo_client(settings: Settings | None = None) -> AsyncIOMotorClient:  # type: ignore[type-arg]
    """Return (or create) the module-level Motor client singleton."""
    global _client
    if _client is not None:
        return _client

    cfg = settings or get_settings()

    # directConnection=true is already encoded in the URI.
    # Passing as kwarg too ensures Motor honours it regardless of URI parser.
    _client = AsyncIOMotorClient(
        cfg.mongodb_uri,
        serverSelectionTimeoutMS=8000,
        connectTimeoutMS=8000,
        socketTimeoutMS=30_000,
        maxPoolSize=50,
        directConnection=True,   # single container node — skip RS discovery
        retryWrites=False,       # retryWrites requires a replica set
        retryReads=True,
    )
    logger.info("MongoDB Motor client created (self-hosted 7.0+, directConnection=True)")
    return _client


def get_database(settings: Settings | None = None) -> AsyncIOMotorDatabase:  # type: ignore[type-arg]
    cfg = settings or get_settings()
    return get_mongo_client(cfg)[cfg.mongodb_db_name]


def get_documents_collection(settings: Settings | None = None) -> AsyncIOMotorCollection:  # type: ignore[type-arg]
    cfg = settings or get_settings()
    return get_database(cfg)[cfg.mongodb_collection_docs]


async def close_mongo_client() -> None:
    """Close the Motor client — call on app shutdown."""
    global _client
    if _client is not None:
        _client.close()
        _client = None
        logger.info("MongoDB client closed")


async def ensure_indexes(settings: Settings | None = None) -> None:
    """
    Create all required indexes on startup. Idempotent — safe to call every boot.

    Indexes created:
      1. B-tree:  url, chunk_index, crawl_job_id  (standard lookups)
      2. TEXT:    content + title                  (for $text full-text search)
      3. Vector:  embedding field                  (for $vectorSearch, MongoDB 7.0+)
    """
    cfg = settings or get_settings()
    coll = get_documents_collection(cfg)
    db   = get_database(cfg)
    vector_index_definition: dict[str, Any] = {
        "fields": [
            {
                "type": "vector",
                "path": "embedding",
                "numDimensions": cfg.embedding_dimensions,
                "similarity": "cosine",
            }
        ]
    }

    # ---- 1 & 2: B-tree + text indexes -------------------------------- #
    try:
        await coll.create_indexes([
            IndexModel([("url", ASCENDING)],         name="ix_url"),
            IndexModel([("chunk_index", ASCENDING)], name="ix_chunk_index"),
            IndexModel([("crawl_job_id", ASCENDING)],name="ix_crawl_job_id"),
            # Compound text index — used by $text operator in text_search()
            IndexModel(
                [("content", TEXT), ("title", TEXT)],
                name="ix_text_search",
                weights={"content": 10, "title": 3},
                default_language="english",
            ),
        ])
        logger.info("Standard indexes ensured on '%s'", cfg.mongodb_collection_docs)
    except OperationFailure as exc:
        if "already exists" in str(exc).lower():
            logger.debug("Standard indexes already exist — skipping")
        else:
            logger.warning("Index creation warning: %s", exc)

    # ---- 3: Vector search index -------------------------------------- #
    try:
        await db.command(
            "createSearchIndexes",
            cfg.mongodb_collection_docs,
            indexes=[{
                "name": cfg.atlas_vector_index,
                "type": "vectorSearch",
                "definition": vector_index_definition,
            }],
        )
        logger.info("Vector search index '%s' created", cfg.atlas_vector_index)
    except OperationFailure as exc:
        if "already exists" in str(exc).lower() or exc.code in (68, 85):
            logger.debug("Vector search index '%s' already exists — skipping", cfg.atlas_vector_index)
        else:
            logger.warning(
                "Could not auto-create vector search index '%s'. "
                "Create it manually via mongosh:\n"
                "  db.%s.createSearchIndex('%s', 'vectorSearch', %s)\n"
                "Error: %s",
                cfg.atlas_vector_index,
                cfg.mongodb_collection_docs,
                cfg.atlas_vector_index,
                vector_index_definition,
                exc,
            )


# ------------------------------------------------------------------ #
# Health check
# ------------------------------------------------------------------ #
async def ping_mongo() -> bool:
    """Returns True if MongoDB is reachable."""
    try:
        client = get_mongo_client()
        await client.admin.command("ping")
        return True
    except Exception as exc:
        logger.error("MongoDB ping failed: %s", exc)
        return False
