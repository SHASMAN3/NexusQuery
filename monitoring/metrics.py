"""
monitoring/metrics.py
----------------------
In-process metrics counters exposed at GET /metrics.

Uses a simple thread-safe counter dict (no external dependency).
In production, swap these for `prometheus_client` gauges/histograms
and expose via a /metrics Prometheus endpoint.
"""

from __future__ import annotations

import time
from collections import defaultdict
from threading import Lock
from typing import Any


class MetricsStore:
    """
    Simple in-process metrics registry.
    Tracks:
      - Request counts by response_type
      - Latency histogram buckets (ms)
      - Fallback rate
      - Injection attempt count
    """

    _LATENCY_BUCKETS = [50, 100, 200, 500, 1000, 2000, 5000]

    def __init__(self) -> None:
        self._lock = Lock()
        self._counters: dict[str, int] = defaultdict(int)
        self._latency_sum_ms: int = 0
        self._latency_buckets: dict[str, int] = {
            f"le_{b}": 0 for b in self._LATENCY_BUCKETS
        }
        self._latency_buckets["le_inf"] = 0
        self._started_at = time.time()

    # ------------------------------------------------------------------ #
    # Record a completed request
    # ------------------------------------------------------------------ #

    def record_request(
        self,
        response_type: str,
        total_ms: int,
        injection_detected: bool = False,
    ) -> None:
        with self._lock:
            self._counters["requests_total"] += 1
            self._counters[f"response_type_{response_type}"] += 1
            self._latency_sum_ms += total_ms

            for bucket in self._LATENCY_BUCKETS:
                if total_ms <= bucket:
                    self._latency_buckets[f"le_{bucket}"] += 1
            self._latency_buckets["le_inf"] += 1

            if injection_detected:
                self._counters["injection_attempts_total"] += 1

    def record_ingest(self, chunks_indexed: int) -> None:
        with self._lock:
            self._counters["ingest_jobs_total"] += 1
            self._counters["chunks_indexed_total"] += chunks_indexed

    def record_error(self, error_type: str) -> None:
        with self._lock:
            self._counters[f"errors_{error_type}"] += 1

    # ------------------------------------------------------------------ #
    # Snapshot
    # ------------------------------------------------------------------ #

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total = self._counters.get("requests_total", 0)
            fallback = (
                self._counters.get("response_type_faq_fallback", 0)
                + self._counters.get("response_type_keyword_fallback", 0)
                + self._counters.get("response_type_no_answer", 0)
            )
            avg_latency = (
                round(self._latency_sum_ms / total, 1) if total > 0 else 0.0
            )
            fallback_rate = round(fallback / total, 4) if total > 0 else 0.0

            return {
                "uptime_seconds": round(time.time() - self._started_at, 1),
                "requests_total": total,
                "avg_latency_ms": avg_latency,
                "fallback_rate": fallback_rate,
                "response_types": {
                    "rag": self._counters.get("response_type_rag", 0),
                    "faq_fallback": self._counters.get("response_type_faq_fallback", 0),
                    "keyword_fallback": self._counters.get("response_type_keyword_fallback", 0),
                    "no_answer": self._counters.get("response_type_no_answer", 0),
                },
                "latency_histogram_ms": dict(self._latency_buckets),
                "injection_attempts_total": self._counters.get("injection_attempts_total", 0),
                "ingest_jobs_total": self._counters.get("ingest_jobs_total", 0),
                "chunks_indexed_total": self._counters.get("chunks_indexed_total", 0),
                "errors": {
                    k: v for k, v in self._counters.items() if k.startswith("errors_")
                },
            }


# Module-level singleton
_store = MetricsStore()


def get_metrics() -> MetricsStore:
    return _store