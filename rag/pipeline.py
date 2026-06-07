"""
rag/pipeline.py
---------------
Orchestrates the full RAG pipeline:

  1. Guardrails check (sanitise + injection detection)
  2. Embed query → hybrid search MongoDB Atlas
  3. Confidence threshold check
  4. If above threshold → LangChain chain (context + Gemini generation)
  5. If below threshold → fallback (FAQ / keyword)

Returns a PipelineResult with answer, response type, scores, and latencies.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Optional

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.output_parsers import StrOutputParser
from langchain_core.runnables import RunnablePassthrough

from config.settings import get_settings
from db.models_sql import ResponseType
from ingestion.embedder import ChunkEmbedder
from ingestion.vector_store import MongoVectorStore
from rag.confidence import ConfidenceResult, evaluate_confidence
from rag.fallback import FallbackResult, run_fallback
from rag.guardrails import GuardrailResult, run_guardrails
from rag.prompt_templates import QA_PROMPT, format_context

logger = logging.getLogger(__name__)


@dataclass
class PipelineResult:
    """Complete result returned from the RAG pipeline."""
    answer: str
    response_type: ResponseType
    question_original: str
    question_sanitised: str

    # Scores
    confidence: Optional[ConfidenceResult] = None

    # Latencies (ms)
    guardrail_ms: int = 0
    retrieval_ms: int = 0
    generation_ms: int = 0
    total_ms: int = 0

    # Retrieved context metadata
    chunks_retrieved: int = 0
    source_urls: list[str] = field(default_factory=list)
    source_urls_json: str = "[]"

    # Guardrail flags
    was_sanitised: bool = False
    injection_detected: bool = False

    # Fallback metadata
    faq_id: Optional[str] = None

    @property
    def top_score(self) -> float:
        return self.confidence.top_score if self.confidence else 0.0


class RAGPipeline:
    """
    Stateless RAG pipeline. Instantiate once and reuse across requests.
    Thread/async-safe — all state is per-call.
    """

    def __init__(self) -> None:
        cfg = get_settings()
        self._cfg = cfg
        self._embedder = ChunkEmbedder()
        self._vector_store = MongoVectorStore()
        self._llm = ChatGoogleGenerativeAI(
            model=cfg.gemini_model,
            google_api_key=cfg.google_api_key,
            temperature=cfg.gemini_temperature,
            max_output_tokens=cfg.gemini_max_output_tokens,
            convert_system_message_to_human=False,
        )
        self._chain = QA_PROMPT | self._llm | StrOutputParser()
        logger.info(
            "RAGPipeline initialised: model=%s threshold=%.2f",
            cfg.gemini_model, cfg.confidence_threshold,
        )

    async def run(
        self,
        question: str,
        request_id: str = "",
        langsmith_extra: dict[str, Any] | None = None,
    ) -> PipelineResult:
        """
        Execute the full RAG pipeline for a user question.

        Args:
            question:        Raw user question (pre-sanitisation)
            request_id:      Trace ID for logging correlation
            langsmith_extra: Additional metadata passed to LangSmith tracer
        """
        total_start = time.perf_counter()

        # ---- 1. Guardrails ------------------------------------------ #
        t0 = time.perf_counter()
        guard: GuardrailResult = run_guardrails(question)
        guardrail_ms = int((time.perf_counter() - t0) * 1000)

        if guard.should_block:
            logger.warning(
                "Injection detected: request_id=%s pattern=%s",
                request_id, guard.injection.matched_pattern,
            )
            return PipelineResult(
                answer=(
                    "Your question could not be processed due to a security policy. "
                    "Please rephrase and try again."
                ),
                response_type=ResponseType.NO_ANSWER,
                question_original=question,
                question_sanitised=guard.safe_text,
                guardrail_ms=guardrail_ms,
                total_ms=int((time.perf_counter() - total_start) * 1000),
                was_sanitised=guard.sanitised.was_modified,
                injection_detected=True,
            )

        clean_q = guard.safe_text

        # ---- 2. Embed + Hybrid Search -------------------------------- #
        t0 = time.perf_counter()
        query_embedding = await self._embedder.embed_query(clean_q)
        search_results = await self._vector_store.hybrid_search(
            query=clean_q,
            query_embedding=query_embedding,
            top_k=self._cfg.retriever_top_k,
        )
        retrieval_ms = int((time.perf_counter() - t0) * 1000)

        # ---- 3. Confidence threshold --------------------------------- #
        confidence = evaluate_confidence(search_results)

        source_urls = list({doc.get("url", "") for doc in search_results if doc.get("url")})

        if not confidence.above_threshold:
            # ---- 4a. Fallback ---------------------------------------- #
            fallback: FallbackResult = await run_fallback(clean_q)
            total_ms = int((time.perf_counter() - total_start) * 1000)

            return PipelineResult(
                answer=fallback.answer,
                response_type=fallback.response_type,
                question_original=question,
                question_sanitised=clean_q,
                confidence=confidence,
                guardrail_ms=guardrail_ms,
                retrieval_ms=retrieval_ms,
                generation_ms=0,
                total_ms=total_ms,
                chunks_retrieved=len(search_results),
                source_urls=source_urls,
                source_urls_json=json.dumps(source_urls),
                was_sanitised=guard.sanitised.was_modified,
                injection_detected=False,
                faq_id=fallback.faq_id,
            )

        # ---- 4b. LLM Generation -------------------------------------- #
        context_str = format_context(
            search_results,
            max_tokens=self._cfg.context_max_tokens,
        )

        t0 = time.perf_counter()
        try:
            # Build config for LangSmith tracing
            chain_config: dict[str, Any] = {}
            if langsmith_extra:
                chain_config["metadata"] = langsmith_extra
            if request_id:
                chain_config.setdefault("metadata", {})["request_id"] = request_id

            answer: str = await self._chain.ainvoke(
                {"context": context_str, "question": clean_q},
                config=chain_config if chain_config else None,
            )
        except Exception as exc:
            logger.error("LLM generation failed: %s", exc, exc_info=True)
            # Graceful degradation — attempt fallback on LLM error
            fallback = await run_fallback(clean_q)
            total_ms = int((time.perf_counter() - total_start) * 1000)
            return PipelineResult(
                answer=fallback.answer,
                response_type=fallback.response_type,
                question_original=question,
                question_sanitised=clean_q,
                confidence=confidence,
                guardrail_ms=guardrail_ms,
                retrieval_ms=retrieval_ms,
                generation_ms=int((time.perf_counter() - t0) * 1000),
                total_ms=total_ms,
                chunks_retrieved=len(search_results),
                source_urls=source_urls,
                source_urls_json=json.dumps(source_urls),
                was_sanitised=guard.sanitised.was_modified,
                injection_detected=False,
                faq_id=fallback.faq_id,
            )

        generation_ms = int((time.perf_counter() - t0) * 1000)
        total_ms = int((time.perf_counter() - total_start) * 1000)

        logger.info(
            "RAG answer: request_id=%s score=%.4f retrieval=%dms gen=%dms total=%dms",
            request_id,
            confidence.top_score,
            retrieval_ms,
            generation_ms,
            total_ms,
        )

        return PipelineResult(
            answer=answer,
            response_type=ResponseType.RAG,
            question_original=question,
            question_sanitised=clean_q,
            confidence=confidence,
            guardrail_ms=guardrail_ms,
            retrieval_ms=retrieval_ms,
            generation_ms=generation_ms,
            total_ms=total_ms,
            chunks_retrieved=len(search_results),
            source_urls=source_urls,
            source_urls_json=json.dumps(source_urls),
            was_sanitised=guard.sanitised.was_modified,
            injection_detected=False,
        )