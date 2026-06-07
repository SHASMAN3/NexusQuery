"""
api/schemas.py
--------------
Pydantic v2 request/response models for all API endpoints.
"""

from __future__ import annotations

from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator


# ------------------------------------------------------------------ #
# /ask
# ------------------------------------------------------------------ #

class AskRequest(BaseModel):
    question: str = Field(
        ...,
        min_length=3,
        max_length=1000,
        description="The user's question to answer from the help documentation.",
        examples=["How do I reset my password?"],
    )
    # Optional: client can pass a session/conversation ID for multi-turn correlation
    session_id: Optional[str] = Field(
        default=None,
        max_length=128,
        description="Optional session ID for multi-turn conversation tracking.",
    )

    @field_validator("question")
    @classmethod
    def question_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("Question must not be blank")
        return v.strip()


class SourceDoc(BaseModel):
    url: str
    title: str = ""


class AskResponse(BaseModel):
    answer: str
    response_type: str = Field(
        description="One of: rag, faq_fallback, keyword_fallback, no_answer"
    )
    confidence_score: Optional[float] = Field(
        default=None,
        description="Top cosine similarity score (0–1). Present for RAG and fallback responses.",
    )
    sources: list[SourceDoc] = Field(default_factory=list)
    request_id: str
    latency_ms: int = Field(description="Total end-to-end API latency in milliseconds")


# ------------------------------------------------------------------ #
# /ingest
# ------------------------------------------------------------------ #

class IngestRequest(BaseModel):
    target_url: str = Field(
        ...,
        description="The root URL to crawl and index.",
        examples=["https://docs.example.com"],
    )
    max_depth: int = Field(default=3, ge=1, le=10)
    max_pages: int = Field(default=500, ge=1, le=5000)
    allowed_domains: list[str] = Field(
        default_factory=list,
        description="Additional domains allowed during crawl. Target domain is always included.",
    )
    exclude_path_patterns: list[str] = Field(
        default_factory=list,
        description="URL path substrings to exclude (e.g. '/login', '/admin').",
    )

    @field_validator("target_url")
    @classmethod
    def validate_url(cls, v: str) -> str:
        from urllib.parse import urlparse
        parsed = urlparse(v)
        if parsed.scheme not in ("http", "https"):
            raise ValueError("target_url must start with http:// or https://")
        if not parsed.netloc:
            raise ValueError("target_url must include a valid domain")
        return v.rstrip("/")


class IngestResponse(BaseModel):
    job_id: str
    status: str
    message: str


# ------------------------------------------------------------------ #
# /health
# ------------------------------------------------------------------ #

class HealthResponse(BaseModel):
    status: str = Field(description="'healthy' or 'degraded'")
    version: str
    environment: str
    checks: dict[str, Any]


# ------------------------------------------------------------------ #
# Error envelope
# ------------------------------------------------------------------ #

class ErrorResponse(BaseModel):
    error: str
    detail: Optional[str] = None
    request_id: Optional[str] = None