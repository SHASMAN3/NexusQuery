"""
db/mysql_client.py
------------------
Async SQLAlchemy engine and session factory for MySQL.
Uses aiomysql driver. Provides a FastAPI-compatible dependency
and a standalone async context manager for scripts.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import NullPool

from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)

# Module-level singletons — initialised once at app startup
_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def build_engine(settings: Settings | None = None) -> AsyncEngine:
    """Create (or reuse) the async SQLAlchemy engine."""
    global _engine
    if _engine is not None:
        return _engine

    cfg = settings or get_settings()

    connect_args = {
        "charset": "utf8mb4",
        "connect_timeout": cfg.mysql_pool_timeout,
    }

    _engine = create_async_engine(
        cfg.mysql_dsn,
        echo=cfg.mysql_echo,
        pool_size=cfg.mysql_pool_size,
        max_overflow=cfg.mysql_max_overflow,
        pool_timeout=cfg.mysql_pool_timeout,
        pool_pre_ping=True,      # heartbeat on checkout
        pool_recycle=1800,       # recycle connections every 30 min
        connect_args=connect_args,
    )
    logger.info("MySQL async engine created (host=%s db=%s)", cfg.mysql_host, cfg.mysql_db)
    return _engine


def build_session_factory(settings: Settings | None = None) -> async_sessionmaker[AsyncSession]:
    """Create (or reuse) the session factory bound to the engine."""
    global _session_factory
    if _session_factory is not None:
        return _session_factory

    engine = build_engine(settings)
    _session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
        autocommit=False,
    )
    return _session_factory


@asynccontextmanager
async def get_db_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Async context manager for use in scripts and background tasks.

    Usage:
        async with get_db_session() as session:
            result = await session.execute(...)
    """
    factory = build_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    FastAPI dependency — yields an AsyncSession per request.

    Usage in route:
        async def route(db: AsyncSession = Depends(get_session)):
    """
    factory = build_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def close_engine() -> None:
    """Dispose the engine pool — call on app shutdown."""
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
        _engine = None
        _session_factory = None
        logger.info("MySQL engine disposed")


# ------------------------------------------------------------------ #
# Health check helper
# ------------------------------------------------------------------ #
async def ping_mysql() -> bool:
    """Returns True if MySQL is reachable."""
    from sqlalchemy import text
    try:
        async with get_db_session() as session:
            await session.execute(text("SELECT 1"))
        return True
    except Exception as exc:
        logger.error("MySQL ping failed: %s", exc)
        return False