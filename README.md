# NexusQuery - Help Website Q&A Agent

Production-style RAG chatbot that answers natural-language questions from indexed help documentation.

Pipeline: async crawler -> chunking and embeddings -> MongoDB hybrid retrieval -> Gemini generation -> FastAPI API.

[![CI](https://github.com/yourname/NexusQuery/actions/workflows/ci.yml/badge.svg)](https://github.com/yourname/NexusQuery/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115-green.svg)](https://fastapi.tiangolo.com)
[![MongoDB](https://img.shields.io/badge/MongoDB-7.0%2B-brightgreen.svg)](https://www.mongodb.com)
[![LangSmith](https://img.shields.io/badge/LangSmith-traced-orange.svg)](https://smith.langchain.com)

---

## What Is Implemented

| Area | Implementation |
|---|---|
| API | FastAPI app with `/api/v1/ask`, `/api/v1/ingest`, `/api/v1/health`, and `/api/v1/metrics` |
| Authentication | `X-API-Key` header, SHA-256 key lookup in MySQL |
| Rate limiting | Per-key sliding-window limiter backed by in-process deques |
| Crawler | `aiohttp`, BeautifulSoup, URL filtering, robots.txt checks, retry/backoff, checkpoint resume |
| Ingestion | LangChain recursive text splitting, deterministic chunk IDs, Gemini embeddings |
| Retrieval | MongoDB `$vectorSearch` plus `$text` search, fused with reciprocal-rank fusion |
| Generation | LangChain prompt chain with Google Gemini |
| Fallback | MySQL FAQ full-text lookup, then keyword fallback, then `no_answer` |
| Guardrails | Input sanitisation, prompt-injection regex detection, grounded XML-delimited prompt |
| Observability | Request IDs, latency fields, metrics snapshot, LangSmith metadata, MySQL and JSONL audit logs |
| Packaging | Docker multi-stage build and GitHub Actions CI |

---

## Architecture

```mermaid
flowchart TD
    U[Help website URL] --> C[AsyncCrawler]
    C --> P[Clean page text]
    P --> S[Recursive text splitter]
    S --> E[Gemini embeddings]
    E --> M[(MongoDB documents)]
    C --> J[(MySQL crawl_jobs)]

    Q[POST /api/v1/ask] --> A[API key auth + rate limit]
    A --> G[Input validation + guardrails]
    G --> QE[Query embedding]
    QE --> V[MongoDB vector search]
    G --> T[MongoDB text search]
    V --> R[Reciprocal-rank fusion]
    T --> R
    R --> D{Confidence >= threshold?}
    D -->|yes| L[Gemini grounded answer]
    D -->|no| F[FAQ / keyword fallback]
    L --> O[JSON response]
    F --> O
    O --> AL[(Audit logs + metrics)]
```

---

## Query Flow

1. Client sends a question to `POST /api/v1/ask` with `X-API-Key`.
2. FastAPI validates the request and enforces API-key authentication plus rate limiting.
3. Guardrails sanitise the question and block known prompt-injection or jailbreak patterns.
4. The pipeline embeds the question and runs MongoDB vector search and text search.
5. Results are fused with weighted reciprocal-rank fusion.
6. If retrieval confidence is high enough, Gemini answers using the grounded context prompt.
7. If confidence is low, the system falls back to MySQL FAQ full-text search and keyword matching.
8. The API returns the answer, response type, confidence score, sources, request ID, and latency.
9. Audit logs and metrics are written in background tasks.

---

## Tech Stack

| Layer | Technology |
|---|---|
| API | FastAPI, Pydantic v2, Uvicorn |
| LLM | Google Gemini via `langchain-google-genai` |
| Embeddings | Gemini embedding model (`models/gemini-embedding-2`, configured as 3072 dimensions) |
| RAG Framework | LangChain |
| Vector and text search | MongoDB vector search and `$text` search |
| Structured DB | MySQL via SQLAlchemy async and aiomysql |
| Crawler | aiohttp, BeautifulSoup4, lxml |
| Monitoring | LangSmith, in-process metrics, MySQL audit table, JSONL audit files |
| CI | GitHub Actions lint, type check, tests, coverage, Docker build smoke test |
| Containerisation | Docker multi-stage build with non-root runtime user |

---

## Project Structure

```text
NexusQuery/
|-- .github/workflows/      ci.yml, deploy.yml placeholder
|-- api/                    FastAPI app, routes, middleware, schemas
|-- config/                 Pydantic settings
|-- crawler/                async crawler, checkpoints, URL filtering, models
|-- db/                     MongoDB and MySQL clients, SQLAlchemy models, migrations
|-- ingestion/              chunking, embeddings, MongoDB vector store
|-- monitoring/             audit logging, LangSmith setup, metrics
|-- rag/                    pipeline, prompt templates, confidence, fallback, guardrails
|-- scripts/                crawl/index and retrieval evaluation scripts
|-- tests/                  mocked unit and API tests
|-- Dockerfile
|-- pyproject.toml
|-- requirements.txt
`-- index.html
```

---

## Configuration

The app reads settings from environment variables or `.env` via `pydantic-settings`.

Required runtime values:

```env
GOOGLE_API_KEY=your_google_ai_key
MONGODB_URI=mongodb+srv://user:pass@cluster.example.mongodb.net/?retryWrites=true&w=majority
MYSQL_PASSWORD=your_mysql_password
```

Common optional values:

```env
ENVIRONMENT=development
MYSQL_HOST=localhost
MYSQL_PORT=3306
MYSQL_USER=NexusQuery_user
MYSQL_DB=NexusQuery
LANGSMITH_API_KEY=
LANGSMITH_PROJECT=NexusQuery-webqa
RATE_LIMIT_RPM=60
```

---

## MongoDB Indexes

The app attempts to create standard indexes and a vector search index at startup in `db/mongo_client.py`.

Vector index definition:

```json
{
  "fields": [
    {
      "type": "vector",
      "path": "embedding",
      "numDimensions": 3072,
      "similarity": "cosine"
    }
  ]
}
```

The text search path uses MongoDB's `$text` operator with a compound text index on `content` and `title`.

---

## Running Locally

Install dependencies:

```bash
python -m pip install -r requirements.txt
```

Start MySQL and MongoDB, then run:

```bash
python -m uvicorn api.main:app --host 0.0.0.0 --port 8080
```

The API will be available at:

```text
http://localhost:8080
```

The root route serves `index.html` when present.

---

## Crawl And Index

Via CLI:

```bash
python scripts/crawl_and_index.py \
  --url https://docs.example.com \
  --depth 3 \
  --max-pages 500 \
  --exclude /admin /login
```

Via API:

```bash
curl -X POST http://localhost:8080/api/v1/ingest \
  -H "X-API-Key: your_key" \
  -H "Content-Type: application/json" \
  -d "{\"target_url\":\"https://docs.example.com\",\"max_depth\":3,\"max_pages\":500}"
```

Poll job status:

```bash
curl http://localhost:8080/api/v1/ingest/<job_id> \
  -H "X-API-Key: your_key"
```

---

## Ask A Question

```bash
curl -X POST http://localhost:8080/api/v1/ask \
  -H "X-API-Key: your_key" \
  -H "Content-Type: application/json" \
  -d "{\"question\":\"How do I reset my password?\"}"
```

Example response:

```json
{
  "answer": "To reset your password, click 'Forgot password' on the login page...",
  "response_type": "rag",
  "confidence_score": 0.8923,
  "sources": [
    {
      "url": "https://docs.example.com/account/reset",
      "title": ""
    }
  ],
  "request_id": "3f8a2b1c-...",
  "latency_ms": 342
}
```

Response types:

| Type | Meaning |
|---|---|
| `rag` | Gemini answered from retrieved context |
| `faq_fallback` | MySQL FAQ full-text match |
| `keyword_fallback` | Keyword match against FAQ entries |
| `no_answer` | No reliable answer found |

---

## API Reference

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/v1/ask` | Answer a natural-language question |
| `POST` | `/api/v1/ingest` | Start a crawl, chunk, embed, and index job |
| `GET` | `/api/v1/ingest/{job_id}` | Read ingest job status |
| `GET` | `/api/v1/health` | Readiness check for MySQL and MongoDB |
| `GET` | `/api/v1/metrics` | In-process metrics snapshot |

---

## Testing

`requirements-dev.txt` is currently empty, so install test tooling explicitly before running tests:

```bash
python -m pip install pytest pytest-asyncio pytest-cov httpx ruff mypy
python -m pytest tests/ -m "not integration"
```

Run coverage:

```bash
python -m pytest tests/ --cov=. --cov-report=term-missing
```

---

## CI And Deployment Status

Implemented:

- Ruff formatting and lint checks
- mypy type check
- pytest with coverage
- Docker image build smoke test

Not implemented yet:

- `.github/workflows/deploy.yml` is empty
- Automated push to Google Artifact Registry
- Automated Cloud Run deployment

The Dockerfile is prepared for container platforms by listening on `$PORT`, using a non-root user, and exposing a health check.

---

## Security Design

| Threat | Mitigation |
|---|---|
| Prompt injection | Guardrail regex patterns block known jailbreak and instruction-override attempts |
| Hallucination | Grounded prompt instructs Gemini to answer only from retrieved context |
| API abuse | Per-key sliding-window rate limiter |
| Auth bypass | Raw API keys are hashed with SHA-256 before MySQL lookup |
| XSS-style input | HTML-sensitive characters are escaped during sanitisation |
| Secret leakage | Runtime configuration comes from environment variables; `.env` is ignored by git |
| Container privilege | Docker runtime uses a non-root user |

---

## Performance Notes

The code records retrieval, generation, and total request latency in each pipeline result and audit record. The repository does not currently include a reproducible load-test report, so this README does not claim fixed P95 latency or support-ticket impact numbers.

---

## License

MIT
