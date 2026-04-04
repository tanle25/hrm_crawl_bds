-- Migration: add_classification_fields (revised)
-- Drop the broken GENERATED ALWAYS columns and recreate as regular columns,
-- then backfill from existing result_json JSONB.

-- 1. Drop broken generated columns
ALTER TABLE llm_post_analyses
    DROP COLUMN IF EXISTS is_ban,
    DROP COLUMN IF EXISTS is_mua,
    DROP COLUMN IF EXISTS is_cho_thue,
    DROP COLUMN IF EXISTS post_intent,
    DROP COLUMN IF EXISTS property_type,
    DROP COLUMN IF EXISTS price_value_vnd,
    DROP COLUMN IF EXISTS area_sqm,
    DROP COLUMN IF EXISTS district,
    DROP COLUMN IF EXISTS province,
    DROP COLUMN IF EXISTS legal_status;

-- 2. Recreate as regular columns (not generated)
ALTER TABLE llm_post_analyses
    ADD COLUMN IF NOT EXISTS is_ban         BOOLEAN,
    ADD COLUMN IF NOT EXISTS is_mua        BOOLEAN,
    ADD COLUMN IF NOT EXISTS is_cho_thue  BOOLEAN,
    ADD COLUMN IF NOT EXISTS post_intent   TEXT,
    ADD COLUMN IF NOT EXISTS property_type TEXT,
    ADD COLUMN IF NOT EXISTS district      TEXT,
    ADD COLUMN IF NOT EXISTS province       TEXT,
    ADD COLUMN IF NOT EXISTS legal_status  TEXT;

-- 3. Backfill from existing result_json JSONB
UPDATE llm_post_analyses SET
    is_ban         = (result_json->>'is_ban')::boolean,
    is_mua        = (result_json->>'is_mua')::boolean,
    is_cho_thue  = (result_json->>'is_cho_thue')::boolean,
    post_intent   = result_json->>'post_intent',
    property_type = result_json->>'property_type',
    district      = result_json->>'district',
    province      = result_json->>'province',
    legal_status  = result_json->>'legal_status'
WHERE result_json IS NOT NULL;

-- 4. Indexes for fast filtering
CREATE INDEX IF NOT EXISTS llm_post_analyses_is_ban_idx
    ON llm_post_analyses (is_ban) WHERE is_ban = true;

CREATE INDEX IF NOT EXISTS llm_post_analyses_is_mua_idx
    ON llm_post_analyses (is_mua) WHERE is_mua = true;

CREATE INDEX IF NOT EXISTS llm_post_analyses_is_cho_thue_idx
    ON llm_post_analyses (is_cho_thue) WHERE is_cho_thue = true;

CREATE INDEX IF NOT EXISTS llm_post_analyses_property_type_idx
    ON llm_post_analyses (property_type) WHERE property_type IS NOT NULL;

CREATE INDEX IF NOT EXISTS llm_post_analyses_district_idx
    ON llm_post_analyses (district) WHERE district IS NOT NULL;

CREATE INDEX IF NOT EXISTS llm_post_analyses_province_idx
    ON llm_post_analyses (province) WHERE province IS NOT NULL;

CREATE INDEX IF NOT EXISTS llm_post_analyses_intent_idx
    ON llm_post_analyses (post_intent) WHERE post_intent IS NOT NULL;

-- 5. Record migration
INSERT INTO schema_migrations (version, applied_at)
VALUES ('add_classification_fields', NOW())
ON CONFLICT DO NOTHING;
