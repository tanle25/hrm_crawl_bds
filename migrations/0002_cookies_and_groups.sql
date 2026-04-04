-- Migration: 0002_cookies_and_groups
-- Description: Add cookie and group management tables for crawler configuration
-- Direction: up

-- Cookie profiles for Facebook crawling
CREATE TABLE IF NOT EXISTS fb_cookies (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL UNIQUE,
    cookies_json JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'alive',
    last_checked_at TIMESTAMPTZ,
    last_used_at TIMESTAMPTZ,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Facebook group list for crawling
CREATE TABLE IF NOT EXISTS fb_groups (
    id BIGSERIAL PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    name TEXT,
    status TEXT NOT NULL DEFAULT 'active',
    priority INTEGER NOT NULL DEFAULT 0,
    scroll_rounds INTEGER NOT NULL DEFAULT 16,
    max_posts_per_group INTEGER NOT NULL DEFAULT 120,
    last_crawled_at TIMESTAMPTZ,
    last_post_count INTEGER DEFAULT 0,
    error_message TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Crawl schedule for cron automation
CREATE TABLE IF NOT EXISTS crawl_schedules (
    id BIGSERIAL PRIMARY KEY,
    name TEXT NOT NULL,
    group_ids INTEGER[] NOT NULL DEFAULT '{}',
    cookie_ids INTEGER[] NOT NULL DEFAULT '{}',
    cron_expr TEXT NOT NULL DEFAULT '0 */4 * * *',
    enabled BOOLEAN NOT NULL DEFAULT true,
    last_run_at TIMESTAMPTZ,
    next_run_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Crawl history log
CREATE TABLE IF NOT EXISTS crawl_history (
    id BIGSERIAL PRIMARY KEY,
    schedule_id BIGINT REFERENCES crawl_schedules(id) ON DELETE SET NULL,
    group_id BIGINT REFERENCES fb_groups(id) ON DELETE SET NULL,
    cookie_id BIGINT REFERENCES fb_cookies(id) ON DELETE SET NULL,
    started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMPTZ,
    status TEXT NOT NULL DEFAULT 'running',
    posts_found INTEGER DEFAULT 0,
    posts_inserted INTEGER DEFAULT 0,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS crawl_history_started_at_idx ON crawl_history (started_at DESC);
CREATE INDEX IF NOT EXISTS crawl_history_schedule_id_idx ON crawl_history (schedule_id);

-- Crawler global settings
CREATE TABLE IF NOT EXISTS crawler_settings (
    id BIGSERIAL PRIMARY KEY,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Seed default rotation settings
INSERT INTO crawler_settings (key, value) VALUES
    ('rotation_enabled', 'false'),
    ('rotation_after_groups', '3'),
    ('default_cookie_id', '')
ON CONFLICT (key) DO NOTHING;

INSERT INTO schema_migrations (version) VALUES ('0002_cookies_and_groups')
ON CONFLICT (version) DO NOTHING;
