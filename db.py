import hashlib
import json
import os
import re
import unicodedata
from pathlib import Path
from typing import Any

import psycopg


ROOT_DIR = Path(__file__).resolve().parent
DEFAULT_DATABASE_URL = "postgresql:///bds_agent"
SCHEMA_FILE = ROOT_DIR / "schema.sql"


def get_database_url(explicit_url: str | None = None) -> str:
    return explicit_url or DEFAULT_DATABASE_URL


def connect_db(database_url: str | None = None) -> psycopg.Connection:
    return psycopg.connect(get_database_url(database_url), autocommit=False)


def ensure_schema(conn: psycopg.Connection) -> None:
    """Run any pending migrations via the migration runner.

    Safe to call on every connection — migrations are idempotent and
    check the schema_migrations table to skip already-applied steps.
    """
    migrations_dir = ROOT_DIR / "migrations"
    if not migrations_dir.exists():
        # Fallback: apply raw schema.sql (legacy behaviour for existing installs)
        _ensure_schema_raw(conn)
        return

    # Ensure the tracker table exists first
    with conn.cursor() as cur:
        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
            """
        )
    conn.commit()

    # Get already-applied versions
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations")
        applied: set[str] = {row[0] for row in cur.fetchall()}

    # Apply pending migrations in order
    for mig_path in sorted(migrations_dir.glob("*.sql")):
        version = mig_path.stem
        if version in applied:
            continue

        sql = mig_path.read_text(encoding="utf-8")
        # Strip migration header comments
        lines = [
            l for l in sql.splitlines()
            if not l.startswith("-- Migration:")
            and not l.startswith("-- Description:")
            and not l.startswith("-- Direction:")
        ]
        up_sql = "\n".join(lines).strip()
        if not up_sql:
            continue

        with conn.cursor() as cur:
            cur.execute(up_sql)
        conn.commit()

        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO schema_migrations (version) VALUES (%s) ON CONFLICT (version) DO NOTHING",
                (version,),
            )
        conn.commit()


def _ensure_schema_raw(conn: psycopg.Connection) -> None:
    """Legacy schema init for installs that predate the migration system."""
    schema_sql = SCHEMA_FILE.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(schema_sql)
    conn.commit()


def normalize_for_dedupe(value: str | None) -> str:
    text = (value or "").strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("m²", "m2")
    text = re.sub(r"https?://\S+", " ", text)
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def create_crawl_run(conn: psycopg.Connection, group_url: str) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO crawl_runs (group_url, status)
            VALUES (%s, 'running')
            RETURNING id
            """,
            (group_url,),
        )
        crawl_run_id = cur.fetchone()[0]
    conn.commit()
    return crawl_run_id


def complete_crawl_run(conn: psycopg.Connection, crawl_run_id: int, status: str, stats: dict[str, Any]) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE crawl_runs
            SET status = %s,
                completed_at = NOW(),
                stats = %s::jsonb
            WHERE id = %s
            """,
            (status, json.dumps(stats, ensure_ascii=False), crawl_run_id),
        )
    conn.commit()


def _find_existing_observation(
    cur: psycopg.Cursor,
    group_url: str,
    post_url: str | None,
    content_sha256: str,
) -> tuple[int, int] | None:
    if post_url:
        cur.execute(
            """
            SELECT id, canonical_post_id
            FROM post_observations
            WHERE group_url = %s
              AND post_url = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (group_url, post_url),
        )
    else:
        cur.execute(
            """
            SELECT id, canonical_post_id
            FROM post_observations
            WHERE group_url = %s
              AND post_url IS NULL
              AND content_sha256 = %s
            ORDER BY id DESC
            LIMIT 1
            """,
            (group_url, content_sha256),
        )
    return cur.fetchone()


def _find_canonical_post(
    cur: psycopg.Cursor,
    post_url: str | None,
    content_sha256: str,
    normalized_content: str,
    near_duplicate_threshold: float,
) -> tuple[int, str, float | None] | None:
    if post_url:
        cur.execute(
            """
            SELECT id
            FROM canonical_posts
            WHERE primary_post_url = %s
            LIMIT 1
            """,
            (post_url,),
        )
        row = cur.fetchone()
        if row:
            return row[0], "exact_post_url", 1.0

    cur.execute(
        """
        SELECT id
        FROM canonical_posts
        WHERE content_sha256 = %s
        LIMIT 1
        """,
        (content_sha256,),
    )
    row = cur.fetchone()
    if row:
        return row[0], "exact_content_hash", 1.0

    cur.execute(
        """
        SELECT id, similarity(normalized_content, %s) AS score
        FROM canonical_posts
        WHERE char_length(normalized_content) >= 40
          AND similarity(normalized_content, %s) >= %s
        ORDER BY score DESC, id DESC
        LIMIT 1
        """,
        (normalized_content, normalized_content, near_duplicate_threshold),
    )
    row = cur.fetchone()
    if row:
        return row[0], "near_duplicate", float(row[1])
    return None


def ingest_post(
    conn: psycopg.Connection,
    crawl_run_id: int,
    post: dict[str, Any],
    near_duplicate_threshold: float = 0.88,
) -> dict[str, Any]:
    content = (post.get("content") or "").strip()
    normalized_content = normalize_for_dedupe(content)
    content_sha256 = sha256_text(normalized_content or content)
    post_url = post.get("post_url")
    images = post.get("images") or []

    with conn.cursor() as cur:
        existing_observation = _find_existing_observation(
            cur,
            group_url=post["group_url"],
            post_url=post_url,
            content_sha256=content_sha256,
        )
        if existing_observation:
            observation_id, canonical_post_id = existing_observation
            cur.execute(
                """
                UPDATE canonical_posts
                SET last_seen_at = NOW()
                WHERE id = %s
                """,
                (canonical_post_id,),
            )
            conn.commit()
            return {
                "observation_id": observation_id,
                "canonical_post_id": canonical_post_id,
                "inserted": False,
                "dedupe_method": "existing_observation",
                "dedupe_score": 1.0,
            }

        canonical_match = _find_canonical_post(
            cur,
            post_url=post_url,
            content_sha256=content_sha256,
            normalized_content=normalized_content,
            near_duplicate_threshold=near_duplicate_threshold,
        )

        if canonical_match:
            canonical_post_id, dedupe_method, dedupe_score = canonical_match
            cur.execute(
                """
                UPDATE canonical_posts
                SET last_seen_at = NOW(),
                    source_count = source_count + 1,
                    primary_post_url = COALESCE(primary_post_url, %s)
                WHERE id = %s
                """,
                (post_url, canonical_post_id),
            )
        else:
            dedupe_method = "new"
            dedupe_score = None
            cur.execute(
                """
                INSERT INTO canonical_posts (
                    primary_post_url,
                    representative_author,
                    representative_author_id,
                    representative_datetime,
                    representative_content,
                    representative_images,
                    normalized_content,
                    content_sha256,
                    source_group_url,
                    dedupe_method
                )
                VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    post_url,
                    post.get("author"),
                    post.get("author_id"),
                    post.get("datetime"),
                    content,
                    json.dumps(images, ensure_ascii=False),
                    normalized_content,
                    content_sha256,
                    post["group_url"],
                    dedupe_method,
                ),
            )
            canonical_post_id = cur.fetchone()[0]
            cur.execute(
                """
                INSERT INTO llm_enrichment_queue (canonical_post_id, status)
                VALUES (%s, 'pending')
                ON CONFLICT (canonical_post_id) DO NOTHING
                """,
                (canonical_post_id,),
            )

        cur.execute(
            """
            INSERT INTO post_observations (
                canonical_post_id,
                crawl_run_id,
                group_url,
                post_url,
                author,
                author_id,
                datetime_text,
                content,
                normalized_content,
                content_sha256,
                images,
                dedupe_method,
                dedupe_score
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
            RETURNING id
            """,
            (
                canonical_post_id,
                crawl_run_id,
                post["group_url"],
                post_url,
                post.get("author"),
                post.get("author_id"),
                post.get("datetime"),
                content,
                normalized_content,
                content_sha256,
                json.dumps(images, ensure_ascii=False),
                dedupe_method,
                dedupe_score,
            ),
        )
        observation_id = cur.fetchone()[0]
    conn.commit()
    return {
        "observation_id": observation_id,
        "canonical_post_id": canonical_post_id,
        "inserted": True,
        "dedupe_method": dedupe_method,
        "dedupe_score": dedupe_score,
    }


def claim_enrichment_job(conn: psycopg.Connection) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            WITH picked AS (
                SELECT q.id
                FROM llm_enrichment_queue q
                WHERE q.status IN ('pending', 'retry')
                ORDER BY CASE WHEN q.status = 'retry' THEN 0 ELSE 1 END, q.updated_at ASC, q.id ASC
                LIMIT 1
                FOR UPDATE SKIP LOCKED
            )
            UPDATE llm_enrichment_queue q
            SET status = 'processing',
                attempts = attempts + 1,
                locked_at = NOW(),
                updated_at = NOW()
            FROM picked
            WHERE q.id = picked.id
            RETURNING q.id, q.canonical_post_id, q.attempts
            """,
        )
        row = cur.fetchone()
        if not row:
            conn.commit()
            return None
        queue_id, canonical_post_id, attempts = row
        cur.execute(
            """
            SELECT cp.id,
                   cp.primary_post_url,
                   cp.representative_author,
                   cp.representative_author_id,
                   cp.representative_datetime,
                   cp.representative_content,
                   cp.representative_images,
                   po.id AS source_observation_id
            FROM canonical_posts cp
            LEFT JOIN LATERAL (
                SELECT id
                FROM post_observations
                WHERE canonical_post_id = cp.id
                ORDER BY observed_at DESC, id DESC
                LIMIT 1
            ) po ON TRUE
            WHERE cp.id = %s
            """,
            (canonical_post_id,),
        )
        details = cur.fetchone()
    conn.commit()
    if not details:
        return None
    return {
        "queue_id": queue_id,
        "attempts": attempts,
        "canonical_post_id": details[0],
        "primary_post_url": details[1],
        "author": details[2],
        "author_id": details[3],
        "datetime": details[4],
        "content": details[5],
        "images": details[6] or [],
        "source_observation_id": details[7],
    }


def save_enrichment_result(
    conn: psycopg.Connection,
    job: dict[str, Any],
    provider: str,
    model: str,
    prompt_version: str,
    input_content: str,
    raw_response: str,
    result_json: dict[str, Any] | None,
    error_message: str | None = None,
) -> None:
    status = "completed" if error_message is None else "failed"
    queue_status = "completed" if error_message is None else "retry"
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO llm_post_analyses (
                canonical_post_id,
                source_observation_id,
                provider,
                model,
                prompt_version,
                status,
                input_content,
                result_json,
                raw_response,
                error_message,
                completed_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, NOW())
            """,
            (
                job["canonical_post_id"],
                job.get("source_observation_id"),
                provider,
                model,
                prompt_version,
                status,
                input_content,
                json.dumps(result_json, ensure_ascii=False) if result_json is not None else None,
                raw_response,
                error_message,
            ),
        )
        cur.execute(
            """
            UPDATE llm_enrichment_queue
            SET status = %s,
                last_error = %s,
                updated_at = NOW()
            WHERE id = %s
            """,
            (queue_status, error_message, job["queue_id"]),
        )
    conn.commit()


# ── Cookie management ─────────────────────────────────────────────────────────

def list_facebook_cookies(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, status, cookies_json, last_checked_at, last_used_at, error_message, created_at
            FROM fb_cookies
            ORDER BY last_used_at DESC NULLS LAST, created_at DESC
            """
        )
        return [
            {
                "id": row[0], "name": row[1], "status": row[2],
                "cookies_json": row[3],
                "last_checked_at": row[4].isoformat() if row[4] else None,
                "last_used_at": row[5].isoformat() if row[5] else None,
                "error_message": row[6],
                "created_at": row[7].isoformat() if row[7] else None,
            }
            for row in cur.fetchall()
        ]


def upsert_facebook_cookie(conn: psycopg.Connection, name: str, cookies_json: list[dict]) -> int:
    import json as _json
    json_str = _json.dumps(cookies_json, ensure_ascii=False)
    with conn.cursor() as cur:
        # Try UPDATE first (if profile with this name exists)
        cur.execute(
            """
            UPDATE fb_cookies
            SET cookies_json = %s::jsonb,
                status = 'alive',
                last_checked_at = NOW(),
                error_message = NULL,
                updated_at = NOW()
            WHERE name = %s
            RETURNING id
            """,
            (json_str, name),
        )
        row = cur.fetchone()
        if row:
            cookie_id = row[0]
            conn.commit()
            return cookie_id
        # Not found → INSERT new
        cur.execute(
            """
            INSERT INTO fb_cookies (name, cookies_json, status, last_checked_at, updated_at)
            VALUES (%s, %s::jsonb, 'alive', NOW(), NOW())
            RETURNING id
            """,
            (name, json_str),
        )
        cookie_id = cur.fetchone()[0]
    conn.commit()
    return cookie_id


def mark_cookie_dead(conn: psycopg.Connection, cookie_id: int, error_message: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE fb_cookies SET status = 'dead', error_message = %s, updated_at = NOW() WHERE id = %s",
            (error_message, cookie_id),
        )
    conn.commit()


def mark_cookie_alive(conn: psycopg.Connection, cookie_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE fb_cookies SET status = 'alive', error_message = NULL, last_checked_at = NOW(), updated_at = NOW() WHERE id = %s",
            (cookie_id,),
        )
    conn.commit()


def get_alive_cookie(conn: psycopg.Connection) -> tuple[int, list[dict]] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, cookies_json FROM fb_cookies
            WHERE status = 'alive'
            ORDER BY last_used_at ASC NULLS FIRST
            LIMIT 1
            FOR UPDATE SKIP LOCKED
            """
        )
        row = cur.fetchone()
        if not row:
            conn.commit()
            return None
        cookie_id, cookies_json = row[0], row[1]
        cur.execute(
            "UPDATE fb_cookies SET last_used_at = NOW(), updated_at = NOW() WHERE id = %s",
            (cookie_id,),
        )
    conn.commit()
    return cookie_id, (cookies_json or [])


def get_live_cookie_json(conn: psycopg.Connection) -> list[dict] | None:
    """Return the cookies JSON of the first alive cookie, or None."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT cookies_json FROM fb_cookies WHERE status = 'alive' ORDER BY last_used_at ASC NULLS FIRST LIMIT 1"
        )
        row = cur.fetchone()
    return row[0] if row else None


def get_cookie_by_id(conn: psycopg.Connection, cookie_id: int) -> dict[str, Any] | None:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, status, cookies_json, last_checked_at, last_used_at, error_message, created_at
            FROM fb_cookies WHERE id = %s
            """,
            (cookie_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "id": row[0], "name": row[1], "status": row[2],
            "cookies_json": row[3],
            "last_checked_at": row[4].isoformat() if row[4] else None,
            "last_used_at": row[5].isoformat() if row[5] else None,
            "error_message": row[6],
            "created_at": row[7].isoformat() if row[7] else None,
        }


def delete_facebook_cookie(conn: psycopg.Connection, cookie_id: int) -> bool:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM fb_cookies WHERE id = %s", (cookie_id,))
    conn.commit()
    return cur.rowcount > 0


# ── Group management ────────────────────────────────────────────────────────

def list_facebook_groups(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, url, name, status, priority, scroll_rounds, max_posts_per_group,
                   last_crawled_at, last_post_count, error_message, created_at
            FROM fb_groups
            ORDER BY priority DESC, id ASC
            """
        )
        return [
            {
                "id": row[0], "url": row[1], "name": row[2], "status": row[3],
                "priority": row[4], "scroll_rounds": row[5], "max_posts_per_group": row[6],
                "last_crawled_at": row[7].isoformat() if row[7] else None,
                "last_post_count": row[8], "error_message": row[9],
                "created_at": row[10].isoformat() if row[10] else None,
            }
            for row in cur.fetchall()
        ]


def upsert_facebook_group(
    conn: psycopg.Connection,
    url: str,
    name: str | None = None,
    priority: int = 0,
    scroll_rounds: int = 16,
    max_posts_per_group: int = 120,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO fb_groups (url, name, priority, scroll_rounds, max_posts_per_group)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (url) DO UPDATE SET
                name = EXCLUDED.name,
                priority = EXCLUDED.priority,
                scroll_rounds = EXCLUDED.scroll_rounds,
                max_posts_per_group = EXCLUDED.max_posts_per_group,
                updated_at = NOW()
            RETURNING id
            """,
            (url, name, priority, scroll_rounds, max_posts_per_group),
        )
        row = cur.fetchone()
    conn.commit()
    return row[0]


def update_group_crawled(
    conn: psycopg.Connection,
    group_id: int,
    posts_inserted: int,
    error_message: str | None = None,
) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE fb_groups
            SET last_crawled_at = NOW(),
                last_post_count = %s,
                error_message = %s,
                updated_at = NOW()
            WHERE id = %s
            """,
            (posts_inserted, error_message, group_id),
        )
    conn.commit()


def get_active_groups(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, url, scroll_rounds, max_posts_per_group
            FROM fb_groups
            WHERE status = 'active'
            ORDER BY priority DESC, id ASC
            """
        )
        return [
            {"id": row[0], "url": row[1], "scroll_rounds": row[2], "max_posts_per_group": row[3]}
            for row in cur.fetchall()
        ]


# ── Crawl schedule ───────────────────────────────────────────────────────────

def list_crawl_schedules(conn: psycopg.Connection) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, name, group_ids, cookie_ids, cron_expr, enabled, last_run_at, next_run_at
            FROM crawl_schedules
            ORDER BY id ASC
            """
        )
        return [
            {
                "id": row[0], "name": row[1], "group_ids": row[2] or [],
                "cookie_ids": row[3] or [], "cron_expr": row[4],
                "enabled": row[5], "last_run_at": row[6].isoformat() if row[6] else None,
                "next_run_at": row[7].isoformat() if row[7] else None,
            }
            for row in cur.fetchall()
        ]


def upsert_crawl_schedule(
    conn: psycopg.Connection,
    name: str,
    group_ids: list[int],
    cookie_ids: list[int],
    cron_expr: str,
    enabled: bool = True,
) -> int:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO crawl_schedules (name, group_ids, cookie_ids, cron_expr, enabled)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT DO NOTHING
            """,
            (name, group_ids, cookie_ids, cron_expr, enabled),
        )
        if cur.rowcount == 0:
            cur.execute(
                """
                UPDATE crawl_schedules
                SET group_ids = %s, cookie_ids = %s, cron_expr = %s, enabled = %s, updated_at = NOW()
                WHERE name = %s
                RETURNING id
                """,
                (group_ids, cookie_ids, cron_expr, enabled, name),
            )
            row = cur.fetchone()
            if row:
                return row[0]
        cur.execute("SELECT id FROM crawl_schedules WHERE name = %s LIMIT 1", (name,))
        row = cur.fetchone()
    conn.commit()
    return row[0] if row else 0


def delete_crawl_schedule(conn: psycopg.Connection, schedule_id: int) -> None:
    with conn.cursor() as cur:
        cur.execute("DELETE FROM crawl_schedules WHERE id = %s", (schedule_id,))
    conn.commit()


# ── Crawler settings ─────────────────────────────────────────────────────────

def get_crawler_settings(conn: psycopg.Connection) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT key, value FROM crawler_settings")
        return {row[0]: row[1] for row in cur.fetchall()}


def set_crawler_setting(conn: psycopg.Connection, key: str, value: str) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO crawler_settings (key, value) VALUES (%s, %s)
            ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()
            """,
            (key, value),
        )
    conn.commit()
