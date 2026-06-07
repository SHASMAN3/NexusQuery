"""
tests/test_api.py
------------------
FastAPI integration tests using httpx AsyncClient.
All database and pipeline calls are mocked.
"""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, MagicMock, patch

from api.main import create_app
from db.models_sql import APIKey, ResponseType


# ------------------------------------------------------------------ #
# Fixtures
# ------------------------------------------------------------------ #

@pytest.fixture
def mock_api_key():
    key = MagicMock(spec=APIKey)
    key.id = "key-001"
    key.name = "test-key"
    key.is_active = True
    key.rate_limit_rpm = None
    return key


@pytest.fixture
def app():
    return create_app()


@pytest_asyncio.fixture
async def client(app, mock_api_key):
    """Async test client with auth + rate limit bypassed."""
    from api.middleware.rate_limiter import check_rate_limit
    from api.middleware.auth import require_api_key

    app.dependency_overrides[check_rate_limit] = lambda: mock_api_key
    app.dependency_overrides[require_api_key] = lambda: mock_api_key

    # Patch lifespan DB calls so tests don't need real DBs
    with (
        patch("api.main.build_engine"),
        patch("api.main.configure_langsmith"),
        patch("api.main.ensure_indexes", new_callable=AsyncMock),
        patch("api.main.get_mongo_client"),
        patch("api.main.close_engine", new_callable=AsyncMock),
        patch("api.main.close_mongo_client", new_callable=AsyncMock),
        patch("sqlalchemy.ext.asyncio.AsyncEngine.begin"),
    ):
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as ac:
            yield ac

    app.dependency_overrides.clear()


# ------------------------------------------------------------------ #
# POST /api/v1/ask
# ------------------------------------------------------------------ #

class TestAskEndpoint:

    @pytest.mark.asyncio
    async def test_ask_returns_200_on_rag_response(self, client):
        from rag.pipeline import PipelineResult
        mock_result = PipelineResult(
            answer="You can reset your password from the login page.",
            response_type=ResponseType.RAG,
            question_original="How do I reset my password?",
            question_sanitised="How do I reset my password?",
            total_ms=320,
            retrieval_ms=80,
            generation_ms=240,
            chunks_retrieved=3,
            source_urls=["https://docs.example.com/password"],
            source_urls_json='["https://docs.example.com/password"]',
        )

        with (
            patch("api.routes.ask.get_rag_pipeline") as mock_get_pipeline,
            patch("api.routes.ask.write_audit_log", new_callable=AsyncMock),
        ):
            mock_pipeline = AsyncMock()
            mock_pipeline.run = AsyncMock(return_value=mock_result)
            mock_get_pipeline.return_value = mock_pipeline

            resp = await client.post(
                "/api/v1/ask",
                json={"question": "How do I reset my password?"},
                headers={"X-API-Key": "test-key-value"},
            )

        assert resp.status_code == 200
        data = resp.json()
        assert data["answer"] == "You can reset your password from the login page."
        assert data["response_type"] == "rag"
        assert "request_id" in data
        assert data["latency_ms"] == 320

    @pytest.mark.asyncio
    async def test_ask_rejects_blank_question(self, client):
        resp = await client.post(
            "/api/v1/ask",
            json={"question": "   "},
            headers={"X-API-Key": "test-key-value"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_ask_rejects_missing_question(self, client):
        resp = await client.post(
            "/api/v1/ask",
            json={},
            headers={"X-API-Key": "test-key-value"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_ask_rejects_too_long_question(self, client):
        resp = await client.post(
            "/api/v1/ask",
            json={"question": "x" * 1001},
            headers={"X-API-Key": "test-key-value"},
        )
        assert resp.status_code == 422

    @pytest.mark.asyncio
    async def test_x_request_id_returned(self, client):
        from rag.pipeline import PipelineResult
        mock_result = PipelineResult(
            answer="Test answer",
            response_type=ResponseType.RAG,
            question_original="Test?",
            question_sanitised="Test?",
            total_ms=100,
        )
        with (
            patch("api.routes.ask.get_rag_pipeline") as mock_get_pipeline,
            patch("api.routes.ask.write_audit_log", new_callable=AsyncMock),
        ):
            mock_pipeline = AsyncMock()
            mock_pipeline.run = AsyncMock(return_value=mock_result)
            mock_get_pipeline.return_value = mock_pipeline

            resp = await client.post(
                "/api/v1/ask",
                json={"question": "Test question?"},
                headers={"X-API-Key": "test-key-value"},
            )
        assert "x-request-id" in resp.headers


# ------------------------------------------------------------------ #
# GET /api/v1/health
# ------------------------------------------------------------------ #

class TestHealthEndpoint:

    @pytest.mark.asyncio
    async def test_health_returns_200(self, client):
        with (
            patch("api.routes.health.ping_mysql", new_callable=AsyncMock, return_value=True),
            patch("api.routes.health.ping_mongo", new_callable=AsyncMock, return_value=True),
        ):
            resp = await client.get("/api/v1/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "healthy"
        assert "mysql" in data["checks"]
        assert "mongodb" in data["checks"]

    @pytest.mark.asyncio
    async def test_health_degraded_when_db_down(self, client):
        with (
            patch("api.routes.health.ping_mysql", new_callable=AsyncMock, return_value=False),
            patch("api.routes.health.ping_mongo", new_callable=AsyncMock, return_value=True),
        ):
            resp = await client.get("/api/v1/health")
        assert resp.status_code == 200
        assert resp.json()["status"] == "degraded"


# ------------------------------------------------------------------ #
# GET /api/v1/metrics
# ------------------------------------------------------------------ #

class TestMetricsEndpoint:

    @pytest.mark.asyncio
    async def test_metrics_returns_200(self, client):
        resp = await client.get("/api/v1/metrics")
        assert resp.status_code == 200
        data = resp.json()
        assert "requests_total" in data
        assert "fallback_rate" in data
        assert "avg_latency_ms" in data