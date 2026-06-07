"""
config/settings.py
------------------
Single source of truth for all runtime configuration.
Reads from environment variables / .env file via pydantic-settings.
"""

from __future__ import annotations

import secrets
from functools import lru_cache
from typing import Literal

from pydantic import AnyHttpUrl, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ------------------------------------------------------------------ #
    # Application
    # ------------------------------------------------------------------ #
    app_name: str = "Pulse WebQA Agent"
    app_version: str = "1.0.0"
    environment: Literal["development", "staging", "production"] = "development"
    debug: bool = False
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # ------------------------------------------------------------------ #
    # API Security
    # ------------------------------------------------------------------ #
    # Comma-separated list of valid API keys (hashed in DB; plain here for bootstrap)
    api_secret_key: str = Field(default_factory=lambda: secrets.token_hex(32))
    api_key_header: str = "X-API-Key"
    # Rate limiting — requests per minute per API key
    rate_limit_rpm: int = 60
    # CORS allowed origins (comma-separated)
    allowed_origins: str = "http://localhost:3000,http://localhost:8000"

    @property
    def allowed_origins_list(self) -> list[str]:
        return [o.strip() for o in self.allowed_origins.split(",") if o.strip()]

    # ------------------------------------------------------------------ #
    # Google Gemini
    # ------------------------------------------------------------------ #
    google_api_key: str = Field(..., description="Google Generative AI API key")
    gemini_model: str = "gemini-2.5-flash"
    gemini_temperature: float = Field(default=0.2, ge=0.0, le=1.0)
    gemini_max_output_tokens: int = 1024
    embedding_model: str = "models/gemini-embedding-2"
    embedding_dimensions: int = 3072

    # ------------------------------------------------------------------ #
    # MongoDB Atlas
    # ------------------------------------------------------------------ #
    mongodb_uri: str = Field(
        ...,
        description="MongoDB Atlas connection string (mongodb+srv://...)",
    )
    mongodb_db_name: str = "pulse_db"
    mongodb_collection_docs: str = "documents"
    # Atlas Search index names — must match what you create in Atlas UI
    atlas_vector_index: str = "pulse_vector_index"
    atlas_search_index: str = "pulse_text_index"
    # Hybrid search weights (must sum to 1.0)
    hybrid_vector_weight: float = Field(default=0.7, ge=0.0, le=1.0)
    hybrid_text_weight: float = Field(default=0.3, ge=0.0, le=1.0)
    # Number of candidates for ANN pre-filter
    vector_num_candidates: int = 150
    # Final top-k after RRF fusion
    retriever_top_k: int = 5

    @field_validator("hybrid_vector_weight", "hybrid_text_weight", mode="before")
    @classmethod
    def validate_weight(cls, v: float) -> float:
        if not 0.0 <= float(v) <= 1.0:
            raise ValueError("Hybrid weights must be between 0 and 1")
        return float(v)

    @model_validator(mode="after")
    def validate_hybrid_weights_sum(self) -> "Settings":
        total = self.hybrid_vector_weight + self.hybrid_text_weight
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"hybrid_vector_weight + hybrid_text_weight must equal 1.0, got {total}"
            )
        return self

    # ------------------------------------------------------------------ #
    # MySQL
    # ------------------------------------------------------------------ #
    mysql_host: str = "localhost"
    mysql_port: int = 3306
    mysql_user: str = "pulse_user"
    mysql_password: str = Field(..., description="MySQL password")
    mysql_db: str = "pulse"
    mysql_pool_size: int = 10
    mysql_max_overflow: int = 20
    mysql_pool_timeout: int = 30
    mysql_echo: bool = False  # set True to log SQL queries

    @property
    def mysql_dsn(self) -> str:
        return (
            f"mysql+aiomysql://{self.mysql_user}:{self.mysql_password}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_db}"
            f"?charset=utf8mb4"
        )

    # ------------------------------------------------------------------ #
    # Crawler
    # ------------------------------------------------------------------ #
    crawler_max_concurrency: int = 10
    crawler_max_depth: int = 3
    crawler_max_pages: int = 500
    crawler_request_timeout: int = 20       # seconds
    crawler_retry_attempts: int = 3
    crawler_retry_backoff_base: float = 1.5  # exponential base
    crawler_retry_backoff_max: float = 30.0  # cap in seconds
    crawler_user_agent: str = (
        "PulseBot/1.0 (+https://github.com/yourname/pulse)"
    )
    crawler_checkpoint_dir: str = "data/checkpoints"
    crawler_respect_robots: bool = True

    # ------------------------------------------------------------------ #
    # Chunking
    # ------------------------------------------------------------------ #
    chunk_size: int = 800
    chunk_overlap: int = 120
    chunk_min_length: int = 50  # discard chunks shorter than this

    # ------------------------------------------------------------------ #
    # RAG / Confidence
    # ------------------------------------------------------------------ #
    # Cosine similarity threshold below which fallback is triggered
    confidence_threshold: float = Field(default=0.72, ge=0.0, le=1.0)
    # Max tokens of retrieved context sent to LLM
    context_max_tokens: int = 3000

    # ------------------------------------------------------------------ #
    # LangSmith
    # ------------------------------------------------------------------ #
    langsmith_api_key: str = ""
    langsmith_project: str = "pulse-webqa"
    langsmith_endpoint: str = "https://api.smith.langchain.com"
    langchain_tracing_v2: bool = True

    # ------------------------------------------------------------------ #
    # Audit / Metrics
    # ------------------------------------------------------------------ #
    audit_log_dir: str = "data/audit_logs"
    metrics_enabled: bool = True


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return a cached Settings singleton. Call this everywhere."""
    return Settings()