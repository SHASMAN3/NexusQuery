"""
db/models_sql.py
----------------
SQLAlchemy 2.x ORM models for all MySQL tables:
  - crawl_jobs
  - audit_logs
  - faq_entries
  - api_keys
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import Enum as PyEnum
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.mysql import CHAR, LONGTEXT, TINYINT
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _new_uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


# ------------------------------------------------------------------ #
# Enums
# ------------------------------------------------------------------ #

class CrawlStatus(str, PyEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ResponseType(str, PyEnum):
    RAG = "rag"                   # LLM answered from retrieved context
    FAQ_FALLBACK = "faq_fallback"  # Matched FAQ table
    KEYWORD_FALLBACK = "keyword_fallback"
    NO_ANSWER = "no_answer"       # Nothing matched


# ------------------------------------------------------------------ #
# crawl_jobs
# ------------------------------------------------------------------ #

# ------------------------------------------------------------------ #
# crawl_jobs
# ------------------------------------------------------------------ #

class CrawlJob(Base):
    __tablename__ = "crawl_jobs"
    __table_args__ = (
        Index("ix_crawl_jobs_status", "status"),
        Index("ix_crawl_jobs_created_at", "created_at"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True, default=_new_uuid)
    target_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    
    # FIX HERE: Added values_callable to look at enum values instead of names
    status: Mapped[CrawlStatus] = mapped_column(
        Enum(CrawlStatus, values_callable=lambda x: [e.value for e in x]), 
        nullable=False, 
        default=CrawlStatus.PENDING
    )
    
    max_depth: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    max_pages: Mapped[int] = mapped_column(Integer, nullable=False, default=500)
    pages_crawled: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunks_indexed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def __repr__(self) -> str:
        return f"<CrawlJob id={self.id} url={self.target_url} status={self.status}>"


# ------------------------------------------------------------------ #
# audit_logs
# ------------------------------------------------------------------ #

class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_logs_created_at", "created_at"),
        Index("ix_audit_logs_response_type", "response_type"),
        Index("ix_audit_logs_api_key_id", "api_key_id"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True, default=_new_uuid)
    request_id: Mapped[str] = mapped_column(CHAR(36), nullable=False, index=True)
    api_key_id: Mapped[Optional[str]] = mapped_column(CHAR(36), nullable=True)
    question: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(LONGTEXT, nullable=False)
    
    # FIX HERE (Defensive): Added values_callable here too
    response_type: Mapped[ResponseType] = mapped_column(
        Enum(ResponseType, values_callable=lambda x: [e.value for e in x]), 
        nullable=False
    )
    
    confidence_score: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    retrieval_latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    generation_latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    total_latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    chunks_retrieved: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    source_urls: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    was_sanitised: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    injection_detected: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    client_ip: Mapped[Optional[str]] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<AuditLog id={self.id} type={self.response_type} latency={self.total_latency_ms}ms>"

# ------------------------------------------------------------------ #
# faq_entries
# ------------------------------------------------------------------ #

class FAQEntry(Base):
    __tablename__ = "faq_entries"
    __table_args__ = (
        Index("ix_faq_entries_is_active", "is_active"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True, default=_new_uuid)
    # Comma-separated trigger keywords / phrases
    keywords: Mapped[str] = mapped_column(Text, nullable=False)
    question_pattern: Mapped[str] = mapped_column(Text, nullable=False)
    answer: Mapped[str] = mapped_column(LONGTEXT, nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    hit_count: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    def keyword_list(self) -> list[str]:
        return [k.strip().lower() for k in self.keywords.split(",") if k.strip()]

    def __repr__(self) -> str:
        return f"<FAQEntry id={self.id} category={self.category}>"


# ------------------------------------------------------------------ #
# api_keys
# ------------------------------------------------------------------ #

class APIKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        Index("ix_api_keys_key_hash", "key_hash", unique=True),
        Index("ix_api_keys_is_active", "is_active"),
        {"mysql_charset": "utf8mb4", "mysql_collate": "utf8mb4_unicode_ci"},
    )

    id: Mapped[str] = mapped_column(CHAR(36), primary_key=True, default=_new_uuid)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    # SHA-256 hex digest of the raw key — never store raw keys
    key_hash: Mapped[str] = mapped_column(CHAR(64), nullable=False, unique=True)
    # Rate limit tier (requests per minute); NULL = use global default
    rate_limit_rpm: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[Optional[datetime]] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    def __repr__(self) -> str:
        return f"<APIKey id={self.id} name={self.name} active={self.is_active}>"