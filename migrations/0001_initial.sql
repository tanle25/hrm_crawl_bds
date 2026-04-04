-- Migration: 0001_initial
-- Description: Bootstrap full BDS Agent schema with pgvector support
-- Direction: up

-- Extensions
CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS vector;

-- Track all migrations applied
CREATE TABLE IF NOT EXISTS schema_migrations (
    version TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Crawl run metadata
CREATE TABLE IF NOT EXISTS crawl_runs (
    id BIGSERIAL PRIMARY KEY,
    group_url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'running',
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    stats JSONB NOT NULL DEFAULT '{}'::jsonb
);

-- Canonical deduplicated posts
CREATE TABLE IF NOT EXISTS canonical_posts (
    id BIGSERIAL PRIMARY KEY,
    primary_post_url TEXT,
    representative_author TEXT,
    representative_author_id TEXT,
    representative_datetime TEXT,
    representative_content TEXT NOT NULL,
    representative_images JSONB NOT NULL DEFAULT '[]'::jsonb,
    normalized_content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    source_group_url TEXT NOT NULL,
    source_count INTEGER NOT NULL DEFAULT 1,
    first_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_seen_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    dedupe_method TEXT NOT NULL DEFAULT 'new'
);

CREATE UNIQUE INDEX IF NOT EXISTS canonical_posts_primary_post_url_unique
    ON canonical_posts (primary_post_url)
    WHERE primary_post_url IS NOT NULL;

CREATE INDEX IF NOT EXISTS canonical_posts_content_sha256_idx
    ON canonical_posts (content_sha256);

CREATE INDEX IF NOT EXISTS canonical_posts_normalized_content_trgm_idx
    ON canonical_posts
    USING GIN (normalized_content gin_trgm_ops);

-- Each crawl observation of a post
CREATE TABLE IF NOT EXISTS post_observations (
    id BIGSERIAL PRIMARY KEY,
    canonical_post_id BIGINT NOT NULL REFERENCES canonical_posts(id) ON DELETE CASCADE,
    crawl_run_id BIGINT REFERENCES crawl_runs(id) ON DELETE SET NULL,
    group_url TEXT NOT NULL,
    post_url TEXT,
    author TEXT,
    author_id TEXT,
    datetime_text TEXT,
    content TEXT NOT NULL,
    normalized_content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    images JSONB NOT NULL DEFAULT '[]'::jsonb,
    dedupe_method TEXT NOT NULL,
    dedupe_score DOUBLE PRECISION,
    observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS post_observations_canonical_post_id_idx
    ON post_observations (canonical_post_id);

CREATE INDEX IF NOT EXISTS post_observations_content_sha256_idx
    ON post_observations (content_sha256);

CREATE INDEX IF NOT EXISTS post_observations_group_url_idx
    ON post_observations (group_url);

-- LLM enrichment job queue
CREATE TABLE IF NOT EXISTS llm_enrichment_queue (
    id BIGSERIAL PRIMARY KEY,
    canonical_post_id BIGINT NOT NULL REFERENCES canonical_posts(id) ON DELETE CASCADE,
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    locked_at TIMESTAMPTZ,
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (canonical_post_id)
);

CREATE INDEX IF NOT EXISTS llm_enrichment_queue_status_idx
    ON llm_enrichment_queue (status, updated_at);

-- LLM analysis results (append-only, never overwrites raw data)
CREATE TABLE IF NOT EXISTS llm_post_analyses (
    id BIGSERIAL PRIMARY KEY,
    canonical_post_id BIGINT NOT NULL REFERENCES canonical_posts(id) ON DELETE CASCADE,
    source_observation_id BIGINT REFERENCES post_observations(id) ON DELETE SET NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    prompt_version TEXT NOT NULL DEFAULT 'v1',
    status TEXT NOT NULL,
    input_content TEXT NOT NULL,
    result_json JSONB,
    raw_response TEXT,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ
);

CREATE INDEX IF NOT EXISTS llm_post_analyses_canonical_post_id_idx
    ON llm_post_analyses (canonical_post_id, created_at DESC);

-- Vector search chunks
CREATE TABLE IF NOT EXISTS search_chunks (
    id BIGSERIAL PRIMARY KEY,
    canonical_post_id BIGINT NOT NULL REFERENCES canonical_posts(id) ON DELETE CASCADE,
    source_analysis_id BIGINT REFERENCES llm_post_analyses(id) ON DELETE SET NULL,
    chunk_type TEXT NOT NULL,
    chunk_text TEXT NOT NULL,
    chunk_hash TEXT NOT NULL UNIQUE,
    embedding_provider TEXT NOT NULL,
    embedding_model TEXT NOT NULL,
    embedding vector(1024) NOT NULL,
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS search_chunks_canonical_post_id_idx
    ON search_chunks (canonical_post_id);

-- HNSW index for cosine similarity search (pgvector >= 0.5)
CREATE INDEX IF NOT EXISTS search_chunks_embedding_hnsw_idx
    ON search_chunks
    USING hnsw (embedding vector_cosine_ops);

-- Record migration applied
INSERT INTO schema_migrations (version) VALUES ('0001_initial')
ON CONFLICT (version) DO NOTHING;
