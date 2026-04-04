"""Data access layer — fetch posts, details, and stats from PostgreSQL."""
from __future__ import annotations

from typing import Any

from db import connect_db, ensure_schema, get_database_url
from services.analysis import build_display_preview, shape_analysis


def _where_type_filter(type_filter: str | None) -> tuple[str, list]:
    """Return SQL WHERE clause fragment and params for type filtering."""
    if type_filter == "ban":
        return ("AND (latest.result_json->>'is_ban')::text = 'true'", [])
    if type_filter == "can_mua":
        return ("AND (latest.result_json->>'is_mua')::text = 'true'", [])
    if type_filter == "cho_thue":
        return ("AND (latest.result_json->>'is_cho_thue')::text = 'true'", [])
    return ("", [])


def fetch_posts(
    limit: int,
    offset: int,
    q: str | None = None,
    type_filter: str | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """
    Fetch posts with optional type filter.
    Returns (items, total_count).
    """
    type_sql, type_params = _where_type_filter(type_filter)

    with connect_db(get_database_url(None)) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            base_from = """
                FROM canonical_posts cp
                JOIN LATERAL (
                    SELECT result_json
                    FROM llm_post_analyses
                    WHERE canonical_post_id = cp.id
                      AND status = 'completed'
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                ) latest ON TRUE
            """
            count_sql = f"SELECT COUNT(*) {base_from} WHERE 1=1 {type_sql}"
            params = type_params[:]

            if q:
                count_sql += (
                    " AND (cp.representative_content ILIKE %s"
                    " OR cp.representative_author ILIKE %s"
                    " OR latest.result_json::text ILIKE %s)"
                )
                params += [f"%{q}%", f"%{q}%", f"%{q}%"]

            cur.execute(count_sql, params)
            total = cur.fetchone()[0]

            select_sql = f"""
                SELECT cp.id,
                       cp.representative_author,
                       cp.representative_datetime,
                       cp.primary_post_url,
                       cp.source_group_url,
                       cp.source_count,
                       cp.first_seen_at,
                       cp.last_seen_at,
                       LEFT(cp.representative_content, 500),
                       cp.representative_images,
                       latest.result_json
                {base_from}
                WHERE 1=1 {type_sql}
            """
            query_params = type_params[:]
            if q:
                select_sql += (
                    " AND (cp.representative_content ILIKE %s"
                    " OR cp.representative_author ILIKE %s"
                    " OR latest.result_json::text ILIKE %s)"
                )
                query_params += [f"%{q}%", f"%{q}%", f"%{q}%"]

            select_sql += " ORDER BY cp.last_seen_at DESC, cp.id DESC LIMIT %s OFFSET %s"
            query_params += [limit, offset]

            cur.execute(select_sql, query_params)
            rows = cur.fetchall()

    items = [
        {
            "id": row[0],
            "author": row[1],
            "datetime": row[2],
            "post_url": row[3],
            "group_url": row[4],
            "source_count": row[5],
            "first_seen_at": row[6].isoformat() if row[6] else None,
            "last_seen_at": row[7].isoformat() if row[7] else None,
            "content_preview": row[8],
            "display_preview": build_display_preview(row[8], row[10]),
            "images": row[9] or [],
            "analysis": shape_analysis(row[10]),
        }
        for row in rows
    ]
    return items, total


def fetch_post_detail(post_id: int) -> dict[str, Any] | None:
    with connect_db(get_database_url(None)) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT cp.id,
                       cp.representative_author,
                       cp.representative_author_id,
                       cp.representative_datetime,
                       cp.primary_post_url,
                       cp.source_group_url,
                       cp.source_count,
                       cp.first_seen_at,
                       cp.last_seen_at,
                       cp.representative_content,
                       cp.representative_images,
                       latest.result_json
                FROM canonical_posts cp
                JOIN LATERAL (
                    SELECT result_json
                    FROM llm_post_analyses
                    WHERE canonical_post_id = cp.id
                      AND status = 'completed'
                    ORDER BY created_at DESC, id DESC
                    LIMIT 1
                ) latest ON TRUE
                WHERE cp.id = %s
                """,
                (post_id,),
            )
            row = cur.fetchone()
    if not row:
        return None
    return {
        "id": row[0],
        "author": row[1],
        "author_id": row[2],
        "datetime": row[3],
        "post_url": row[4],
        "group_url": row[5],
        "source_count": row[6],
        "first_seen_at": row[7].isoformat() if row[7] else None,
        "last_seen_at": row[8].isoformat() if row[8] else None,
        "content": row[9],
        "images": row[10] or [],
        "analysis": shape_analysis(row[11]),
    }


def fetch_stats() -> dict[str, Any]:
    with connect_db(get_database_url(None)) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                  (SELECT count(*) FROM canonical_posts),
                  (SELECT count(*) FROM post_observations),
                  (SELECT count(*) FROM llm_post_analyses WHERE status = 'completed'),
                  (SELECT count(DISTINCT canonical_post_id) FROM llm_post_analyses WHERE status = 'completed'),
                  (SELECT count(*) FROM search_chunks)
                """
            )
            row = cur.fetchone()
    return {
        "canonical_posts": row[0],
        "observations": row[1],
        "analyses_completed": row[2],
        "processed_posts": row[3],
        "search_chunks": row[4],
    }
