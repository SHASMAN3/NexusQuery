"""
rag/fallback.py
----------------
Structured fallback logic triggered when vector search confidence is low.

Two-stage fallback:
  Stage 1 — FAQ table lookup (MySQL faq_entries, FULLTEXT match)
  Stage 2 — Keyword matching against in-memory FAQ keyword lists

Returns a FallbackResult indicating which stage matched (or no match).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import func, select, text

from db.models_sql import FAQEntry, ResponseType
from db.mysql_client import get_db_session

logger = logging.getLogger(__name__)


@dataclass
class FallbackResult:
    matched: bool
    answer: str
    response_type: ResponseType
    faq_id: Optional[str] = None
    matched_keyword: Optional[str] = None


_NO_MATCH = FallbackResult(
    matched=False,
    answer=(
        "I'm sorry, I don't have specific information to answer your question. "
        "Please visit our help centre or contact support for assistance."
    ),
    response_type=ResponseType.NO_ANSWER,
)


async def run_fallback(question: str) -> FallbackResult:
    """
    Attempt to answer `question` via structured fallback.

    1. MySQL FULLTEXT search on faq_entries (question + keywords)
    2. Token-level keyword matching against faq_entries.keywords
    3. Return no-answer result if nothing matches
    """
    question_lower = question.lower()

    # Stage 1: FULLTEXT search
    result = await _fulltext_faq_search(question)
    if result:
        return result

    # Stage 2: keyword scan (loads active FAQs into memory — cached in production)
    result = await _keyword_faq_search(question_lower)
    if result:
        return result

    logger.info("Fallback: no match for question='%s'", question[:80])
    return _NO_MATCH


# ------------------------------------------------------------------ #
# Stage 1 — MySQL FULLTEXT
# ------------------------------------------------------------------ #

async def _fulltext_faq_search(question: str) -> FallbackResult | None:
    """
    Use MySQL FULLTEXT MATCH … AGAINST for semantic keyword overlap.
    Requires the ft_faq_keywords FULLTEXT index created in 001_init.sql.
    """
    try:
        async with get_db_session() as session:
            # Sanitise question for FULLTEXT boolean mode (remove operators)
            safe_q = re.sub(r'[+\-><()\*~"@]', " ", question).strip()
            if not safe_q:
                return None

            stmt = (
                select(FAQEntry)
                .where(FAQEntry.is_active == True)  # noqa: E712
                .where(
                    text(
                        "MATCH(keywords, question_pattern) "
                        "AGAINST(:q IN BOOLEAN MODE)"
                    ).bindparams(q=safe_q)
                )
                .order_by(FAQEntry.priority.desc())
                .limit(1)
            )
            result = await session.execute(stmt)
            faq: FAQEntry | None = result.scalar_one_or_none()

            if faq:
                # Increment hit counter asynchronously
                await session.execute(
                    text("UPDATE faq_entries SET hit_count = hit_count + 1 WHERE id = :id")
                    .bindparams(id=faq.id)
                )
                logger.info("FAQ FULLTEXT match: id=%s category=%s", faq.id, faq.category)
                return FallbackResult(
                    matched=True,
                    answer=faq.answer,
                    response_type=ResponseType.FAQ_FALLBACK,
                    faq_id=faq.id,
                )
    except Exception as exc:
        logger.error("FULLTEXT FAQ search failed: %s", exc)

    return None


# ------------------------------------------------------------------ #
# Stage 2 — keyword token matching
# ------------------------------------------------------------------ #

async def _keyword_faq_search(question_lower: str) -> FallbackResult | None:
    """
    Load all active FAQs and scan keyword lists for token overlap.
    Selects the FAQ with the most keyword hits (tie-break: priority).
    """
    try:
        async with get_db_session() as session:
            stmt = (
                select(FAQEntry)
                .where(FAQEntry.is_active == True)  # noqa: E712
                .order_by(FAQEntry.priority.desc())
            )
            result = await session.execute(stmt)
            faqs: list[FAQEntry] = list(result.scalars().all())

        # Tokenise question
        question_tokens = set(re.findall(r"\b\w+\b", question_lower))

        best_faq: FAQEntry | None = None
        best_hits = 0
        best_keyword: str | None = None

        for faq in faqs:
            keywords = faq.keyword_list()
            hits = 0
            last_matched: str | None = None
            for kw in keywords:
                kw_tokens = set(re.findall(r"\b\w+\b", kw))
                if kw_tokens and kw_tokens.issubset(question_tokens):
                    hits += len(kw_tokens)
                    last_matched = kw
            if hits > best_hits:
                best_hits = hits
                best_faq = faq
                best_keyword = last_matched

        if best_faq and best_hits >= 1:
            # Increment hit counter
            async with get_db_session() as session:
                await session.execute(
                    text("UPDATE faq_entries SET hit_count = hit_count + 1 WHERE id = :id")
                    .bindparams(id=best_faq.id)
                )
            logger.info(
                "FAQ keyword match: id=%s keyword='%s' hits=%d",
                best_faq.id, best_keyword, best_hits,
            )
            return FallbackResult(
                matched=True,
                answer=best_faq.answer,
                response_type=ResponseType.KEYWORD_FALLBACK,
                faq_id=best_faq.id,
                matched_keyword=best_keyword,
            )

    except Exception as exc:
        logger.error("Keyword FAQ search failed: %s", exc)

    return None