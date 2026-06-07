"""
api/middleware/rate_limiter.py
-------------------------------
Sliding-window rate limiter implemented with a deque + timestamps.
No external dependency (Redis-free). Suitable for single-instance
deployments; for multi-instance, replace with Redis ZSET sliding window.

Used as a FastAPI dependency, not ASGI middleware, so it can access
the resolved API key's per-key rate limit tier.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock
from typing import Callable, Optional

from fastapi import Depends, HTTPException, Request, status

from config.settings import get_settings
from db.models_sql import APIKey
from api.middleware.auth import require_api_key


_cfg = get_settings()

# { key_id: deque of request timestamps (float, epoch) }
_windows: dict[str, deque[float]] = defaultdict(deque)
_lock = Lock()


def check_rate_limit(
    request: Request,
    api_key: APIKey = Depends(require_api_key),
) -> APIKey:
    """
    FastAPI dependency. Enforces sliding-window rate limit per API key.
    Raises HTTP 429 if the key has exceeded its RPM quota.
    Returns the API key on success (so routes can chain dependencies).
    """
    rpm = api_key.rate_limit_rpm or _cfg.rate_limit_rpm
    key_id = api_key.id
    now = time.monotonic()
    window_start = now - 60.0  # 60-second sliding window

    with _lock:
        dq = _windows[key_id]
        # Evict timestamps outside the window
        while dq and dq[0] < window_start:
            dq.popleft()

        if len(dq) >= rpm:
            oldest = dq[0]
            retry_after = int(60 - (now - oldest)) + 1
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded. Retry after {retry_after} seconds.",
                headers={"Retry-After": str(retry_after)},
            )

        dq.append(now)

    return api_key