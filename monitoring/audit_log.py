"""
monitoring/audit_log.py
------------------------
Writes a structured audit record to MySQL `audit_logs` for every /ask request.
Also appends a JSONL line to a local file for offline analysis.

Call `write_audit_log()` at the end of each request handler.
It runs in a background task so it never adds to API latency.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime
from pathlib import Path

from db.models_sql import AuditLog
from db.mysql_client import get_db_session
from config.settings import get_settings
from rag.pipeline import PipelineResult

logger = logging.getLogger(__name__)


async def write_audit_log(
    result: PipelineResult,
    request_id: str,
    api_key_id: str | None = None,
    client_ip: str | None = None,
    user_agent: str | None = None,
) -> None:
    """
    Persist an audit record to MySQL and JSONL file.
    Designed to be called as a background task (asyncio.create_task).
    Failures are logged but never propagate to the caller.
    """
    try:
        await asyncio.gather(
            _write_to_mysql(result, request_id, api_key_id, client_ip, user_agent),
            _write_to_jsonl(result, request_id, api_key_id, client_ip, user_agent),
            return_exceptions=True,
        )
    except Exception as exc:
        logger.error("Audit log write failed (non-fatal): %s", exc)


# ------------------------------------------------------------------ #
# MySQL write
# ------------------------------------------------------------------ #

async def _write_to_mysql(
    result: PipelineResult,
    request_id: str,
    api_key_id: str | None,
    client_ip: str | None,
    user_agent: str | None,
) -> None:
    record = AuditLog(
        request_id=request_id,
        api_key_id=api_key_id,
        question=result.question_original,
        answer=result.answer,
        response_type=result.response_type,
        confidence_score=result.top_score if result.top_score > 0 else None,
        retrieval_latency_ms=result.retrieval_ms,
        generation_latency_ms=result.generation_ms,
        total_latency_ms=result.total_ms,
        chunks_retrieved=result.chunks_retrieved,
        source_urls=result.source_urls_json,
        was_sanitised=result.was_sanitised,
        injection_detected=result.injection_detected,
        client_ip=client_ip,
        user_agent=(user_agent or "")[:512],
    )
    async with get_db_session() as session:
        session.add(record)
    logger.debug("Audit record written to MySQL: request_id=%s", request_id)


# ------------------------------------------------------------------ #
# JSONL file write
# ------------------------------------------------------------------ #

async def _write_to_jsonl(
    result: PipelineResult,
    request_id: str,
    api_key_id: str | None,
    client_ip: str | None,
    user_agent: str | None,
) -> None:
    cfg = get_settings()
    log_dir = Path(cfg.audit_log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)

    # One file per day: audit_2025-01-15.jsonl
    date_str = datetime.utcnow().strftime("%Y-%m-%d")
    log_file = log_dir / f"audit_{date_str}.jsonl"

    record = {
        "ts": datetime.utcnow().isoformat(),
        "request_id": request_id,
        "api_key_id": api_key_id,
        "question": result.question_original,
        "response_type": result.response_type.value,
        "confidence_score": result.top_score,
        "retrieval_ms": result.retrieval_ms,
        "generation_ms": result.generation_ms,
        "total_ms": result.total_ms,
        "chunks_retrieved": result.chunks_retrieved,
        "source_urls": result.source_urls,
        "was_sanitised": result.was_sanitised,
        "injection_detected": result.injection_detected,
        "client_ip": client_ip,
    }

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(
        None,
        _append_jsonl,
        str(log_file),
        record,
    )


def _append_jsonl(path: str, record: dict) -> None:
    """Synchronous file append — called via executor to avoid blocking the loop."""
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")