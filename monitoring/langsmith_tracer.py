"""
monitoring/langsmith_tracer.py
-------------------------------
LangSmith tracing setup for the RAG pipeline.

Wires LangSmith as a LangChain callback handler (not a wrapper)
so it captures token counts, latencies, and chain steps automatically.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from config.settings import get_settings

logger = logging.getLogger(__name__)


def configure_langsmith() -> bool:
    """
    Set environment variables required by LangChain's built-in LangSmith
    integration and verify connectivity.

    Returns True if LangSmith is enabled and configured, False otherwise.
    """
    cfg = get_settings()

    if not cfg.langsmith_api_key:
        logger.info("LangSmith disabled: LANGSMITH_API_KEY not set")
        return False

    os.environ["LANGCHAIN_TRACING_V2"] = "true" if cfg.langchain_tracing_v2 else "false"
    os.environ["LANGCHAIN_API_KEY"] = cfg.langsmith_api_key
    os.environ["LANGCHAIN_PROJECT"] = cfg.langsmith_project
    os.environ["LANGCHAIN_ENDPOINT"] = cfg.langsmith_endpoint

    logger.info(
        "LangSmith configured: project=%s endpoint=%s",
        cfg.langsmith_project,
        cfg.langsmith_endpoint,
    )
    return True


def get_run_metadata(
    request_id: str,
    api_key_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """
    Build a metadata dict to attach to LangChain chain invocations.
    This appears in the LangSmith trace UI under "Metadata".
    """
    cfg = get_settings()
    metadata: dict[str, Any] = {
        "request_id": request_id,
        "environment": cfg.environment,
        "app_version": cfg.app_version,
    }
    if api_key_id:
        metadata["api_key_id"] = api_key_id
    if extra:
        metadata.update(extra)
    return metadata


def get_langsmith_tags(response_type: str | None = None) -> list[str]:
    """
    Build tag list for LangSmith run categorisation.
    Tags appear as filterable labels in the LangSmith UI.
    """
    cfg = get_settings()
    tags = [cfg.environment, "NexusQuery-rag"]
    if response_type:
        tags.append(response_type)
    return tags