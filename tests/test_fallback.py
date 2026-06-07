"""
tests/test_fallback.py
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from db.models_sql import ResponseType
from rag.fallback import FallbackResult, _NO_MATCH, run_fallback


class TestFallback:

    @pytest.mark.asyncio
    async def test_returns_no_match_when_db_empty(self):
        """When no FAQ entries match, return NO_ANSWER."""
        with (
            patch("rag.fallback._fulltext_faq_search", new_callable=AsyncMock, return_value=None),
            patch("rag.fallback._keyword_faq_search", new_callable=AsyncMock, return_value=None),
        ):
            result = await run_fallback("completely unknown query xyz123")
            assert result.matched is False
            assert result.response_type == ResponseType.NO_ANSWER

    @pytest.mark.asyncio
    async def test_fulltext_match_wins(self):
        """If FULLTEXT matches, return its result without keyword scan."""
        faq_result = FallbackResult(
            matched=True,
            answer="FULLTEXT answer",
            response_type=ResponseType.FAQ_FALLBACK,
            faq_id="faq-001",
        )
        with (
            patch("rag.fallback._fulltext_faq_search", new_callable=AsyncMock, return_value=faq_result),
            patch("rag.fallback._keyword_faq_search", new_callable=AsyncMock) as mock_kw,
        ):
            result = await run_fallback("pricing question")
            assert result.answer == "FULLTEXT answer"
            assert result.response_type == ResponseType.FAQ_FALLBACK
            # Keyword scan should NOT be called when FULLTEXT matched
            mock_kw.assert_not_called()

    @pytest.mark.asyncio
    async def test_keyword_scan_used_when_fulltext_misses(self):
        """If FULLTEXT misses, keyword scan should be attempted."""
        kw_result = FallbackResult(
            matched=True,
            answer="Keyword answer",
            response_type=ResponseType.KEYWORD_FALLBACK,
            faq_id="faq-002",
            matched_keyword="pricing",
        )
        with (
            patch("rag.fallback._fulltext_faq_search", new_callable=AsyncMock, return_value=None),
            patch("rag.fallback._keyword_faq_search", new_callable=AsyncMock, return_value=kw_result),
        ):
            result = await run_fallback("Tell me about pricing")
            assert result.response_type == ResponseType.KEYWORD_FALLBACK
            assert result.matched_keyword == "pricing"

    @pytest.mark.asyncio
    async def test_no_match_has_helpful_message(self):
        with (
            patch("rag.fallback._fulltext_faq_search", new_callable=AsyncMock, return_value=None),
            patch("rag.fallback._keyword_faq_search", new_callable=AsyncMock, return_value=None),
        ):
            result = await run_fallback("xkcd1337")
            assert len(result.answer) > 20  # Not empty
            assert result.matched is False