#!/usr/bin/env python
"""
scripts/eval_retrieval.py
--------------------------
Evaluates retrieval quality against a golden QA dataset.

Metrics computed:
  - Hit Rate @ K  : fraction of questions where the correct URL appears in top-K results
  - MRR @ K       : Mean Reciprocal Rank — rewards higher-ranked correct results
  - Mean confidence score for RAG vs fallback responses

Golden dataset format (JSON):
[
  {"question": "How do I reset my password?", "expected_url": "https://docs.example.com/account/reset"},
  ...
]

Usage:
    python scripts/eval_retrieval.py --golden data/golden_qa.json --k 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_settings
from db.mongo_client import get_mongo_client, ensure_indexes
from ingestion.embedder import ChunkEmbedder
from ingestion.vector_store import MongoVectorStore

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("eval_retrieval")


async def evaluate(golden_path: str, k: int) -> dict:
    get_mongo_client()
    await ensure_indexes()

    embedder = ChunkEmbedder()
    store = MongoVectorStore()

    with open(golden_path, encoding="utf-8") as f:
        golden: list[dict] = json.load(f)

    if not golden:
        raise ValueError("Golden dataset is empty")

    logger.info("Evaluating %d questions at k=%d", len(golden), k)

    hit_count = 0
    rr_sum = 0.0
    latencies: list[float] = []
    score_hits: list[float] = []
    score_misses: list[float] = []

    for i, item in enumerate(golden):
        question = item["question"]
        expected_url = item.get("expected_url", "").rstrip("/")

        t0 = time.perf_counter()
        query_embedding = await embedder.embed_query(question)
        results = await store.hybrid_search(
            query=question,
            query_embedding=query_embedding,
            top_k=k,
        )
        latency_ms = (time.perf_counter() - t0) * 1000
        latencies.append(latency_ms)

        # Check if expected URL is in top-K results
        retrieved_urls = [r.get("url", "").rstrip("/") for r in results]
        hit = expected_url in retrieved_urls
        hit_count += int(hit)

        if hit:
            rank = retrieved_urls.index(expected_url) + 1
            rr_sum += 1.0 / rank
            top_score = results[0].get("vector_score") or results[0].get("hybrid_score") or 0.0
            score_hits.append(top_score)
        else:
            top_score = results[0].get("vector_score") or results[0].get("hybrid_score") or 0.0 if results else 0.0
            score_misses.append(top_score)

        if (i + 1) % 10 == 0:
            logger.info("Progress: %d/%d", i + 1, len(golden))

    n = len(golden)
    hit_rate = hit_count / n
    mrr = rr_sum / n
    avg_latency = sum(latencies) / n
    p95_latency = sorted(latencies)[int(0.95 * n) - 1] if n >= 20 else max(latencies)

    metrics = {
        "questions_evaluated": n,
        "k": k,
        "hit_rate_at_k": round(hit_rate, 4),
        "mrr_at_k": round(mrr, 4),
        "avg_retrieval_latency_ms": round(avg_latency, 1),
        "p95_retrieval_latency_ms": round(p95_latency, 1),
        "avg_score_on_hits": round(sum(score_hits) / len(score_hits), 4) if score_hits else 0.0,
        "avg_score_on_misses": round(sum(score_misses) / len(score_misses), 4) if score_misses else 0.0,
    }

    print("\n" + "=" * 60)
    print("  Retrieval Evaluation Results")
    print("=" * 60)
    for key, val in metrics.items():
        print(f"  {key:<40} {val}")
    print("=" * 60)

    return metrics


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate NexusQuery retrieval quality.")
    parser.add_argument("--golden", required=True, help="Path to golden QA JSON file")
    parser.add_argument("--k", type=int, default=5, help="Top-K to evaluate (default: 5)")
    parser.add_argument("--output", help="Optional: write metrics JSON to this path")
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    metrics = asyncio.run(evaluate(args.golden, args.k))
    if args.output:
        Path(args.output).write_text(json.dumps(metrics, indent=2))
        logger.info("Metrics written to %s", args.output)