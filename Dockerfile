# =============================================================================
# Dockerfile — Pulse WebQA Agent
# Multi-stage build: builder (compiles deps) → runtime (slim image)
# Final image size: ~280MB
# =============================================================================

# --------------------------------------------------------------------------- #
# Stage 1: builder
# Install all Python dependencies including compiled extensions (aiomysql, etc.)
# --------------------------------------------------------------------------- #
FROM python:3.11-slim AS builder

# Build-time deps for wheels that need compilation
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    g++ \
    libffi-dev \
    libssl-dev \
    default-libmysqlclient-dev \
    pkg-config \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy requirements first to leverage layer cache
COPY requirements.txt .

# Install into a prefix directory we'll copy to the runtime stage
RUN pip install --upgrade pip \
 && pip install --prefix=/install --no-cache-dir -r requirements.txt


# --------------------------------------------------------------------------- #
# Stage 2: runtime
# Minimal image — no build tools, no caches
# --------------------------------------------------------------------------- #
FROM python:3.11-slim AS runtime

# Runtime deps only (mysql client shared lib needed by aiomysql)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libssl3 \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user for security
RUN groupadd --gid 1001 pulse \
 && useradd --uid 1001 --gid pulse --shell /bin/bash --create-home pulse

WORKDIR /app

# Copy installed packages from builder
COPY --from=builder /install /usr/local

# Copy application source
COPY --chown=pulse:pulse . .

# Create runtime directories
RUN mkdir -p data/checkpoints data/audit_logs \
 && chown -R pulse:pulse data

USER pulse

# Cloud Run expects the app to listen on $PORT (default 8080)
ENV PORT=8080
EXPOSE 8080

# Health check — Cloud Run uses this for readiness
HEALTHCHECK --interval=30s --timeout=10s --start-period=15s --retries=3 \
    CMD curl -f http://localhost:${PORT}/api/v1/health || exit 1

# Uvicorn with 2 workers; tune for Cloud Run (1 vCPU = 2 workers is optimal)
CMD ["sh", "-c", \
    "uvicorn api.main:app \
        --host 0.0.0.0 \
        --port ${PORT} \
        --workers 2 \
        --loop uvloop \
        --http h11 \
        --log-level info \
        --access-log \
        --no-server-header"]