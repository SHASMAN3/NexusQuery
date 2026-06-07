"""
api/middleware/auth.py
-----------------------
API key authentication as a FastAPI dependency (not middleware)
so it can be scoped per-route and return proper 401 JSON responses.

The raw key is hashed (SHA-256) and looked up in MySQL api_keys.
"""

from __future__ import annotations

import hashlib
import logging
from datetime import datetime
from typing import Optional

from fastapi import Depends, HTTPException, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy import select, text

from config.settings import get_settings
from db.models_sql import APIKey
from db.mysql_client import get_db_session

logger = logging.getLogger(__name__)

_cfg = get_settings()
_api_key_scheme = APIKeyHeader(name=_cfg.api_key_header, auto_error=False)


def _hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode()).hexdigest()


async def require_api_key(
    raw_key: Optional[str] = Security(_api_key_scheme),
) -> APIKey:
    """
    FastAPI dependency. Validates the API key from the request header.
    Raises HTTP 401 if missing/invalid/revoked.
    Returns the APIKey ORM object on success.
    """
    if not raw_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing API key. Provide it in the X-API-Key header.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    key_hash = _hash_key(raw_key)

    async with get_db_session() as session:
        stmt = (
            select(APIKey)
            .where(APIKey.key_hash == key_hash)
            .where(APIKey.is_active == True)  # noqa: E712
        )
        result = await session.execute(stmt)
        api_key: APIKey | None = result.scalar_one_or_none()

    if not api_key:
        logger.warning("Invalid or revoked API key attempt: hash_prefix=%s", key_hash[:8])
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or revoked API key.",
            headers={"WWW-Authenticate": "ApiKey"},
        )

    # Update last_used_at in background (fire-and-forget)
    try:
        async with get_db_session() as session:
            await session.execute(
                text("UPDATE api_keys SET last_used_at = :now WHERE id = :id")
                .bindparams(now=datetime.utcnow(), id=api_key.id)
            )
    except Exception as exc:
        logger.error("Failed to update last_used_at: %s", exc)

    return api_key