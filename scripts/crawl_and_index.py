#!/usr/bin/env python
"""
scripts/crawl_and_index.py
--------------------------
CLI entrypoint to crawl a target URL and index it into MongoDB Atlas.

Usage:
    python scripts/crawl_and_index.py --url https://docs.example.com \
        --depth 3 --max-pages 500

The script:
  1. Creates a CrawlJob record in MySQL
  2. Runs the async crawler
  3. Chunks, embeds, and upserts to MongoDB Atlas
  4. Prints a summary
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import uuid
from datetime import datetime
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_settings
from crawler.async_crawler import AsyncCrawler
from crawler.models import CrawlConfig
from db.models_sql import CrawlJob, CrawlStatus
from db.mysql_client import build_engine, build_session_factory, get_db_session
from db.models_sql import Base
from db.mongo_client import get_mongo_client, ensure_indexes
from ingestion.chunker import PageChunker
from ingestion.embedder import ChunkEmbedder
from ingestion.vector_store import MongoVectorStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
logger = logging.getLogger("crawl_and_index")


async def main(args: argparse.Namespace) -> None:
    cfg = get_settings()

    # --- Bootstrap DBs ---
    engine = build_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    get_mongo_client()
    await ensure_indexes()

    job_id = str(uuid.uuid4())
    logger.info("Starting ingest job: id=%s url=%s", job_id, args.url)

    # Create MySQL job record
    async with get_db_session() as session:
        job = CrawlJob(
            id=job_id,
            target_url=args.url,
            status=CrawlStatus.RUNNING,
            max_depth=args.depth,
            max_pages=args.max_pages,
            started_at=datetime.utcnow(),
        )
        session.add(job)

    config = CrawlConfig(
        start_url=args.url,
        job_id=job_id,
        max_depth=args.depth,
        max_pages=args.max_pages,
        checkpoint_dir=cfg.crawler_checkpoint_dir,
        exclude_path_patterns=args.exclude or [],
    )

    crawler = AsyncCrawler(config)
    chunker = PageChunker()
    embedder = ChunkEmbedder()
    store = MongoVectorStore()

    pages_crawled = 0
    chunks_indexed = 0

    try:
        async for page in crawler.crawl():
            pages_crawled += 1
            chunks = chunker.chunk_page(page, job_id)
            if not chunks:
                continue
            chunk_embeddings = await embedder.embed_chunks(chunks)
            n = await store.upsert_chunks(chunk_embeddings)
            chunks_indexed += n

            if pages_crawled % 10 == 0:
                logger.info(
                    "Progress: pages=%d chunks=%d", pages_crawled, chunks_indexed
                )

        # Mark completed
        from sqlalchemy import text
        async with get_db_session() as session:
            await session.execute(
                text(
                    "UPDATE crawl_jobs SET status='completed', completed_at=NOW(), "
                    "pages_crawled=:p, chunks_indexed=:c WHERE id=:id"
                ).bindparams(p=pages_crawled, c=chunks_indexed, id=job_id)
            )

        summary = crawler.summary()
        print("\n" + "=" * 60)
        print(f"  Ingest Complete")
        print("=" * 60)
        print(f"  Job ID:         {job_id}")
        print(f"  Target URL:     {args.url}")
        print(f"  Pages crawled:  {pages_crawled}")
        print(f"  Pages failed:   {summary.pages_failed}")
        print(f"  Chunks indexed: {chunks_indexed}")
        print(f"  Elapsed:        {summary.elapsed_seconds:.1f}s")
        print("=" * 60)

    except Exception as exc:
        logger.error("Ingest failed: %s", exc, exc_info=True)
        from sqlalchemy import text
        async with get_db_session() as session:
            await session.execute(
                text(
                    "UPDATE crawl_jobs SET status='failed', completed_at=NOW(), "
                    "error_message=:e WHERE id=:id"
                ).bindparams(e=str(exc)[:500], id=job_id)
            )
        sys.exit(1)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crawl a website and index it into Pulse.")
    parser.add_argument("--url", required=True, help="Root URL to crawl")
    parser.add_argument("--depth", type=int, default=3, help="Max crawl depth (default: 3)")
    parser.add_argument("--max-pages", type=int, default=500, help="Max pages to crawl (default: 500)")
    parser.add_argument(
        "--exclude",
        nargs="*",
        default=[],
        metavar="PATTERN",
        help="URL path patterns to exclude (e.g. /admin /login)",
    )
    return parser.parse_args()


if __name__ == "__main__":
    asyncio.run(main(parse_args()))