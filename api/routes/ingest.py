"""
api/routes/ingest.py
---------------------
POST /ingest  — triggers an async crawl + embed + index job.
GET  /ingest/{job_id} — returns live status / progress for a job.

The job runs in a background asyncio task so the POST endpoint returns
immediately with a job_id.  Job status is tracked in MySQL crawl_jobs.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select

from api.middleware.auth import require_api_key
from api.middleware.schemas import IngestRequest, IngestResponse
from config.settings import get_settings
from crawler.async_crawler import AsyncCrawler
from crawler.models import CrawlConfig
from db.models_sql import APIKey, CrawlJob, CrawlStatus
from db.mysql_client import get_db_session
from ingestion.chunker import PageChunker
from ingestion.embedder import ChunkEmbedder
from ingestion.vector_store import MongoVectorStore
from monitoring.metrics import get_metrics
from sqlalchemy import text

logger = logging.getLogger(__name__)
router = APIRouter()
_cfg = get_settings()


# ------------------------------------------------------------------ #
# Response schema for job status
# ------------------------------------------------------------------ #

class JobStatusResponse(BaseModel):
    job_id: str
    status: str                    # always lowercase: pending|running|completed|failed
    target_url: str
    pages_crawled: int
    chunks_indexed: int
    max_pages: int
    max_depth: int
    started_at: datetime | None
    completed_at: datetime | None
    error_message: str | None

    model_config = {"from_attributes": True}


# ------------------------------------------------------------------ #
# POST /ingest
# ------------------------------------------------------------------ #

@router.post(
    "/ingest",
    response_model=IngestResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Trigger a crawl and index job",
    tags=["Ingestion"],
)
async def ingest(
    body: IngestRequest,
    background_tasks: BackgroundTasks,
    api_key: APIKey = Depends(require_api_key),
) -> IngestResponse:
    job_id = str(uuid.uuid4())

    # Insert and COMMIT the crawl_job row before the background task runs
    # so that the GET /ingest/{job_id} endpoint (and the task itself) can
    # find it immediately.  The original code added the job inside a
    # context manager that never explicitly committed.
    async with get_db_session() as session:
        job = CrawlJob(
            id=job_id,
            target_url=body.target_url,
            status=CrawlStatus.PENDING,
            max_depth=body.max_depth,
            max_pages=body.max_pages,
        )
        session.add(job)
        await session.commit()          # ← explicit commit; row is now visible

    logger.info("Ingest job created: job_id=%s url=%s", job_id, body.target_url)

    background_tasks.add_task(
        _run_ingest_job,
        job_id=job_id,
        request=body,
    )

    return IngestResponse(
        job_id=job_id,
        status="accepted",
        message=f"Crawl job {job_id} queued. Poll GET /api/v1/ingest/{job_id} for status.",
    )


# ------------------------------------------------------------------ #
# GET /ingest/{job_id}
# ------------------------------------------------------------------ #

@router.get(
    "/ingest/{job_id}",
    response_model=JobStatusResponse,
    summary="Get crawl job status and progress",
    tags=["Ingestion"],
)
async def get_ingest_status(
    job_id: str,
    api_key: APIKey = Depends(require_api_key),
) -> JobStatusResponse:
    """
    Returns the current status and progress counters for a crawl job.

    Designed to be polled by the UI every few seconds.  The frontend
    switches from an indeterminate spinner to a real progress bar once
    ``pages_crawled`` > 0.

    Status values
    -------------
    pending    Job queued, crawler not yet started.
    running    Crawler is active; ``pages_crawled`` updates every 25 pages.
    completed  Job finished successfully.
    failed     Job encountered an unrecoverable error; see ``error_message``.
    """
    async with get_db_session() as session:
        result = await session.execute(
            select(CrawlJob).where(CrawlJob.id == job_id)
        )
        job: CrawlJob | None = result.scalar_one_or_none()

    if job is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Job {job_id!r} not found.",
        )

    # job.status is a CrawlStatus enum member; .value may be uppercase
    # depending on how the enum was declared.  Normalise to lowercase so
    # the frontend's string comparisons ("completed", "failed", …) work.
    return JobStatusResponse(
        job_id=job.id,
        status=job.status.value.lower(),
        target_url=job.target_url,
        pages_crawled=job.pages_crawled or 0,
        chunks_indexed=job.chunks_indexed or 0,
        max_pages=job.max_pages,
        max_depth=job.max_depth,
        started_at=job.started_at,
        completed_at=job.completed_at,
        error_message=job.error_message,
    )


# ------------------------------------------------------------------ #
# Background task
# ------------------------------------------------------------------ #

async def _run_ingest_job(job_id: str, request: IngestRequest) -> None:
    """Background task: crawl → chunk → embed → upsert → update MySQL."""
    chunker = PageChunker()
    embedder = ChunkEmbedder()
    store = MongoVectorStore()

    await _update_job_status(job_id, CrawlStatus.RUNNING, started_at=datetime.utcnow())

    config = CrawlConfig(
        start_url=request.target_url,
        job_id=job_id,
        max_depth=request.max_depth,
        max_pages=request.max_pages,
        allowed_domains=request.allowed_domains,
        exclude_path_patterns=request.exclude_path_patterns,
        checkpoint_dir=_cfg.crawler_checkpoint_dir,
    )

    crawler = AsyncCrawler(config)
    pages_crawled = 0
    chunks_indexed = 0

    try:
        async for page in crawler.crawl():
            pages_crawled += 1
            chunks = chunker.chunk_page(page, job_id)
            if not chunks:
                continue
            chunk_embeddings = await embedder.embed_chunks(chunks)
            indexed = await store.upsert_chunks(chunk_embeddings)
            chunks_indexed += indexed

            # Persist progress every 25 pages so the status endpoint
            # returns live counters while the crawler is running.
            if pages_crawled % 25 == 0:
                await _update_job_progress(job_id, pages_crawled, chunks_indexed)

        await _update_job_status(
            job_id,
            CrawlStatus.COMPLETED,
            completed_at=datetime.utcnow(),
            pages_crawled=pages_crawled,
            chunks_indexed=chunks_indexed,
        )
        get_metrics().record_ingest(chunks_indexed)
        logger.info(
            "Ingest job completed: job_id=%s pages=%d chunks=%d",
            job_id, pages_crawled, chunks_indexed,
        )

    except Exception as exc:
        logger.error("Ingest job failed: job_id=%s err=%s", job_id, exc, exc_info=True)
        await _update_job_status(
            job_id,
            CrawlStatus.FAILED,
            completed_at=datetime.utcnow(),
            error_message=str(exc)[:1000],
            pages_crawled=pages_crawled,
            chunks_indexed=chunks_indexed,
        )


# ------------------------------------------------------------------ #
# DB helpers
# ------------------------------------------------------------------ #

async def _update_job_status(
    job_id: str,
    new_status: CrawlStatus,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    pages_crawled: int = 0,
    chunks_indexed: int = 0,
    error_message: str | None = None,
) -> None:
    """Update crawl_jobs via ORM so SQLAlchemy handles enum coercion correctly.

    Using raw text() SQL and passing new_status.value bypasses the ORM's
    type processors, which can write a value (e.g. 'completed') that the
    ORM cannot read back when the DB column ENUM was declared with a
    different case (e.g. 'COMPLETED').  An ORM-level update avoids this
    entirely — SQLAlchemy serialises and deserialises the enum consistently.
    """
    async with get_db_session() as session:
        result = await session.execute(
            select(CrawlJob).where(CrawlJob.id == job_id)
        )
        job: CrawlJob | None = result.scalar_one_or_none()
        if job is None:
            logger.warning("_update_job_status: job %s not found", job_id)
            return

        job.status = new_status
        if started_at:
            job.started_at = started_at
        if completed_at:
            job.completed_at = completed_at
        if pages_crawled:
            job.pages_crawled = pages_crawled
        if chunks_indexed:
            job.chunks_indexed = chunks_indexed
        if error_message is not None:
            job.error_message = error_message

        await session.commit()


async def _update_job_progress(
    job_id: str, pages_crawled: int, chunks_indexed: int
) -> None:
    async with get_db_session() as session:
        result = await session.execute(
            select(CrawlJob).where(CrawlJob.id == job_id)
        )
        job: CrawlJob | None = result.scalar_one_or_none()
        if job is None:
            return
        job.pages_crawled = pages_crawled
        job.chunks_indexed = chunks_indexed
        await session.commit()