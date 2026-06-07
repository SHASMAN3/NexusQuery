"""
rag/confidence.py
-----------------
Extracts confidence signals from hybrid search results and decides
whether to proceed with RAG generation or trigger fallback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from config.settings import get_settings

logger = logging.getLogger(__name__)


@dataclass
class ConfidenceResult:
    """Outcome of the confidence check."""
    top_score: float          # Highest vector similarity score (0.0–1.0)
    mean_score: float         # Mean across top-k results
    above_threshold: bool     # True → proceed with RAG
    threshold_used: float
    num_results: int


def evaluate_confidence(
    search_results: list[dict[str, Any]],
    threshold: float | None = None,
) -> ConfidenceResult:
    """
    Evaluate the retrieval confidence from hybrid search results.

    Uses `vector_score` (cosine similarity, 0–1) as the primary signal.
    Falls back to `hybrid_score` if `vector_score` is absent (pure-text hits).

    Decision rule:
        If top vector_score >= threshold → RAG generation
        Else                            → fallback (FAQ / keyword)
    """
    cfg = get_settings()
    thresh = threshold if threshold is not None else cfg.confidence_threshold

    if not search_results:
        return ConfidenceResult(
            top_score=0.0,
            mean_score=0.0,
            above_threshold=False,
            threshold_used=thresh,
            num_results=0,
        )

    scores = [
        doc.get("vector_score") or doc.get("hybrid_score") or 0.0
        for doc in search_results
    ]
    top = max(scores)
    mean = sum(scores) / len(scores)
    above = top >= thresh

    if not above:
        logger.info(
            "Confidence BELOW threshold: top=%.4f mean=%.4f threshold=%.4f → fallback",
            top, mean, thresh,
        )
    else:
        logger.debug(
            "Confidence OK: top=%.4f mean=%.4f threshold=%.4f → RAG",
            top, mean, thresh,
        )

    return ConfidenceResult(
        top_score=round(top, 6),
        mean_score=round(mean, 6),
        above_threshold=above,
        threshold_used=thresh,
        num_results=len(search_results),
    )