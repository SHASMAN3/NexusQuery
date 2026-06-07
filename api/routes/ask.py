"""
api/routes/ask.py
-----------------
POST /ask  — the core RAG Q&A endpoint.

Flow:
  1. Auth + rate-limit (dependencies)
  2. Validate request (Pydantic)
  3. Invoke RAGPipeline
  4. Write audit log (background task)
  5. Record metrics
  6. Return structured JSON response
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, BackgroundTasks, Depends, Request, status
from fastapi.responses import JSONResponse

from api.middleware.dependencies import get_rag_pipeline
from api.middleware.rate_limiter import check_rate_limit
from api.middleware.schemas import AskRequest, AskResponse, ErrorResponse, SourceDoc
from config.settings import get_settings
from db.models_sql import APIKey
from monitoring.audit_log import write_audit_log
from monitoring.langsmith_tracer import get_langsmith_tags, get_run_metadata
from monitoring.metrics import get_metrics
from rag.pipeline import RAGPipeline

logger = logging.getLogger(__name__)
router = APIRouter()
_cfg = get_settings()


@router.post(
    "/ask",
    response_model=AskResponse,
    responses={
        400: {"model": ErrorResponse},
        401: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Ask a question",
    description=(
        "Submit a natural-language question and receive an answer grounded "
        "in the indexed help documentation."
    ),
    tags=["Q&A"],
)
async def ask(
    body: AskRequest,
    request: Request,
    background_tasks: BackgroundTasks,
    api_key: APIKey = Depends(check_rate_limit),
    pipeline: RAGPipeline = Depends(get_rag_pipeline),
) -> AskResponse:
    request_id: str = getattr(request.state, "request_id", "unknown")
    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("user-agent", "")

    logger.info(
        "POST /ask request_id=%s api_key=%s question_len=%d",
        request_id,
        api_key.name,
        len(body.question),
    )

    # LangSmith metadata
    ls_metadata = get_run_metadata(
        request_id=request_id,
        api_key_id=api_key.id,
        extra={"session_id": body.session_id},
    )
    ls_tags = get_langsmith_tags()

    try:
        result = await pipeline.run(
            question=body.question,
            request_id=request_id,
            langsmith_extra={**ls_metadata, "tags": ls_tags},
        )
    except Exception as exc:
        logger.error("Pipeline error: request_id=%s err=%s", request_id, exc, exc_info=True)
        get_metrics().record_error("pipeline_exception")
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"error": "An internal error occurred.", "request_id": request_id},
        )

    # Background: audit log + metrics (never blocks response)
    background_tasks.add_task(
        write_audit_log,
        result=result,
        request_id=request_id,
        api_key_id=api_key.id,
        client_ip=client_ip,
        user_agent=user_agent,
    )
    background_tasks.add_task(
        _record_metrics,
        response_type=result.response_type.value,
        total_ms=result.total_ms,
        injection_detected=result.injection_detected,
    )

    sources = [
        SourceDoc(url=url, title="")
        for url in result.source_urls
        if url
    ]

    return AskResponse(
        answer=result.answer,
        response_type=result.response_type.value,
        confidence_score=round(result.top_score, 4) if result.top_score > 0 else None,
        sources=sources,
        request_id=request_id,
        latency_ms=result.total_ms,
    )


def _record_metrics(response_type: str, total_ms: int, injection_detected: bool) -> None:
    get_metrics().record_request(
        response_type=response_type,
        total_ms=total_ms,
        injection_detected=injection_detected,
    )