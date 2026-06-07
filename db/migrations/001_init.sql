-- migrations/001_init.sql
-- ---------------------------------------------------------------------------
-- Pulse WebQA Agent — initial schema
-- Run once against an empty `pulse` database.
-- Compatible with MySQL 8.0+
-- ---------------------------------------------------------------------------

CREATE DATABASE IF NOT EXISTS pulse
    CHARACTER SET utf8mb4
    COLLATE utf8mb4_unicode_ci;

USE pulse;

-- ---------------------------------------------------------------------------
-- crawl_jobs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS crawl_jobs (
    id           CHAR(36)     NOT NULL DEFAULT (UUID()),
    target_url   VARCHAR(2048) NOT NULL,
    status       ENUM('pending','running','completed','failed','cancelled')
                              NOT NULL DEFAULT 'pending',
    max_depth    INT          NOT NULL DEFAULT 3,
    max_pages    INT          NOT NULL DEFAULT 500,
    pages_crawled INT         NOT NULL DEFAULT 0,
    chunks_indexed INT        NOT NULL DEFAULT 0,
    error_message TEXT        NULL,
    started_at   DATETIME     NULL,
    completed_at DATETIME     NULL,
    created_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP
                              ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    INDEX ix_crawl_jobs_status  (status),
    INDEX ix_crawl_jobs_created (created_at)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- api_keys
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS api_keys (
    id              CHAR(36)    NOT NULL DEFAULT (UUID()),
    name            VARCHAR(255) NOT NULL,
    key_hash        CHAR(64)    NOT NULL COMMENT 'SHA-256 hex of the raw key',
    rate_limit_rpm  INT         NULL COMMENT 'NULL = use global default',
    is_active       TINYINT(1)  NOT NULL DEFAULT 1,
    last_used_at    DATETIME    NULL,
    revoked_at      DATETIME    NULL,
    created_at      DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE INDEX ix_api_keys_hash   (key_hash),
    INDEX  ix_api_keys_active       (is_active)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- faq_entries
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS faq_entries (
    id               CHAR(36)    NOT NULL DEFAULT (UUID()),
    keywords         TEXT        NOT NULL COMMENT 'Comma-separated trigger keywords',
    question_pattern TEXT        NOT NULL,
    answer           LONGTEXT    NOT NULL,
    category         VARCHAR(128) NULL,
    priority         INT         NOT NULL DEFAULT 0,
    is_active        TINYINT(1)  NOT NULL DEFAULT 1,
    hit_count        BIGINT      NOT NULL DEFAULT 0,
    created_at       DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP
                                 ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    INDEX ix_faq_active   (is_active),
    FULLTEXT INDEX ft_faq_keywords (keywords, question_pattern)
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- audit_logs
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS audit_logs (
    id                    CHAR(36)   NOT NULL DEFAULT (UUID()),
    request_id            CHAR(36)   NOT NULL,
    api_key_id            CHAR(36)   NULL,
    question              TEXT       NOT NULL,
    answer                LONGTEXT   NOT NULL,
    response_type         ENUM('rag','faq_fallback','keyword_fallback','no_answer')
                                     NOT NULL,
    confidence_score      FLOAT      NULL,
    retrieval_latency_ms  INT        NULL,
    generation_latency_ms INT        NULL,
    total_latency_ms      INT        NOT NULL,
    chunks_retrieved      INT        NULL,
    source_urls           TEXT       NULL COMMENT 'JSON array of URLs',
    was_sanitised         TINYINT(1) NOT NULL DEFAULT 0,
    injection_detected    TINYINT(1) NOT NULL DEFAULT 0,
    client_ip             VARCHAR(45) NULL,
    user_agent            VARCHAR(512) NULL,
    created_at            DATETIME   NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    INDEX ix_audit_request_id    (request_id),
    INDEX ix_audit_api_key_id    (api_key_id),
    INDEX ix_audit_response_type (response_type),
    INDEX ix_audit_created_at    (created_at),
    CONSTRAINT fk_audit_api_key
        FOREIGN KEY (api_key_id) REFERENCES api_keys(id)
        ON DELETE SET NULL
) ENGINE=InnoDB
  DEFAULT CHARSET=utf8mb4
  COLLATE=utf8mb4_unicode_ci;

-- ---------------------------------------------------------------------------
-- Seed FAQ entries
-- ---------------------------------------------------------------------------
INSERT IGNORE INTO faq_entries (id, keywords, question_pattern, answer, category, priority)
VALUES
(UUID(),
 'pricing,price,cost,plan,subscription,free,paid',
 'What are the pricing plans?',
 'We offer a Free tier (up to 1,000 queries/month), a Pro plan at $29/month (50,000 queries), and an Enterprise plan with custom pricing. Visit our pricing page for details.',
 'billing', 10),

(UUID(),
 'reset,forgot,password,login,sign in,access',
 'How do I reset my password?',
 'Click "Forgot password" on the login page, enter your email address, and check your inbox for a reset link valid for 30 minutes.',
 'account', 10),

(UUID(),
 'contact,support,help,ticket,email,chat',
 'How can I contact support?',
 'You can reach our support team at support@example.com, via live chat (Mon–Fri 9am–6pm UTC), or by submitting a ticket in your dashboard.',
 'support', 10),

(UUID(),
 'api,api key,token,authenticate,authentication,bearer',
 'How do I authenticate API requests?',
 'Pass your API key in the X-API-Key header with every request. You can generate keys from Settings → API Keys in your dashboard.',
 'api', 9),

(UUID(),
 'refund,cancel,cancellation,money back,billing',
 'Can I get a refund?',
 'We offer a 14-day money-back guarantee on all paid plans. Contact billing@example.com within 14 days of your first charge.',
 'billing', 8);