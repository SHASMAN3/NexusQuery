"""
crawler/checkpoint.py
----------------------
JSON-based crawl state persistence.
Saves the seen-URL set + queue + stats so a crawl can resume
after a crash or container restart.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class CrawlCheckpoint:
    """Manages a JSON checkpoint file for a single crawl job."""

    VERSION = 1

    def __init__(self, job_id: str, checkpoint_dir: str = "data/checkpoints") -> None:
        self.job_id = job_id
        self._path = Path(checkpoint_dir) / f"{job_id}.json"
        self._path.parent.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def save(
        self,
        seen_urls: list[str],
        queue: list[tuple[str, int]],   # (url, depth)
        stats: dict[str, Any],
    ) -> None:
        """Atomically write checkpoint to disk."""
        payload = {
            "version": self.VERSION,
            "job_id": self.job_id,
            "saved_at": datetime.utcnow().isoformat(),
            "seen_urls": seen_urls,
            "queue": queue,
            "stats": stats,
        }
        # Write to temp file then rename for atomicity
        tmp_path = self._path.with_suffix(".json.tmp")
        tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp_path.replace(self._path)
        logger.debug(
            "Checkpoint saved: job=%s seen=%d queued=%d",
            self.job_id,
            len(seen_urls),
            len(queue),
        )

    def load(self) -> dict[str, Any] | None:
        """Load checkpoint data. Returns None if no checkpoint exists."""
        if not self._path.exists():
            return None
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if data.get("version") != self.VERSION:
                logger.warning(
                    "Checkpoint version mismatch (expected %d, got %d) — ignoring",
                    self.VERSION,
                    data.get("version"),
                )
                return None
            logger.info(
                "Checkpoint loaded: job=%s seen=%d queued=%d saved_at=%s",
                self.job_id,
                len(data.get("seen_urls", [])),
                len(data.get("queue", [])),
                data.get("saved_at"),
            )
            return data
        except (json.JSONDecodeError, KeyError) as exc:
            logger.error("Failed to load checkpoint for job %s: %s", self.job_id, exc)
            return None

    def delete(self) -> None:
        """Remove the checkpoint file after a successful crawl."""
        try:
            self._path.unlink(missing_ok=True)
            logger.debug("Checkpoint deleted: job=%s", self.job_id)
        except OSError as exc:
            logger.warning("Could not delete checkpoint for job %s: %s", self.job_id, exc)

    def exists(self) -> bool:
        return self._path.exists()

    @property
    def path(self) -> Path:
        return self._path