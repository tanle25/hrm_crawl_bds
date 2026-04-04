import argparse
import json
from pathlib import Path

from dotenv import load_dotenv

from db import connect_db, ensure_schema, get_database_url
from vectorizer import (
    DEFAULT_EMBEDDING_BASE_URL,
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_PROVIDER,
    chunk_hash,
    embed_text,
    embed_text_batch,
    vector_to_sql_literal,
)


ROOT_DIR = Path(__file__).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build vector chunks for chatbot retrieval.")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--provider", default=DEFAULT_EMBEDDING_PROVIDER)
    parser.add_argument("--model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--dimensions", type=int, default=DEFAULT_EMBEDDING_DIM)
    parser.add_argument("--base-url", default=DEFAULT_EMBEDDING_BASE_URL)
    parser.add_argument("--limit", type=int, default=100)
    return parser


def fetch_candidates(conn, limit: int):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cp.id,
                   cp.primary_post_url,
                   cp.representative_author,
                   cp.representative_datetime,
                   cp.representative_content,
                   cp.source_group_url,
                   cp.representative_images,
                   latest.id AS analysis_id,
                   latest.result_json
            FROM canonical_posts cp
            JOIN LATERAL (
                SELECT id, result_json
                FROM llm_post_analyses
                WHERE canonical_post_id = cp.id
                  AND status = 'completed'
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            ) latest ON TRUE
            WHERE NOT EXISTS (
                SELECT 1
                FROM search_chunks sc
                WHERE sc.canonical_post_id = cp.id
            )
            ORDER BY cp.last_seen_at DESC, cp.id DESC
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def fetch_candidate_by_post_id(conn, canonical_post_id: int):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT cp.id,
                   cp.primary_post_url,
                   cp.representative_author,
                   cp.representative_datetime,
                   cp.representative_content,
                   cp.source_group_url,
                   cp.representative_images,
                   latest.id AS analysis_id,
                   latest.result_json
            FROM canonical_posts cp
            JOIN LATERAL (
                SELECT id, result_json
                FROM llm_post_analyses
                WHERE canonical_post_id = cp.id
                  AND status = 'completed'
                ORDER BY created_at DESC, id DESC
                LIMIT 1
            ) latest ON TRUE
            WHERE cp.id = %s
            """,
            (canonical_post_id,),
        )
        return cur.fetchone()


def build_chunk_specs(row) -> list[dict]:
    (
        canonical_post_id,
        primary_post_url,
        author,
        datetime_text,
        content,
        source_group_url,
        representative_images,
        analysis_id,
        result_json,
    ) = row

    metadata_base = {
        "canonical_post_id": canonical_post_id,
        "post_url": primary_post_url,
        "author": author,
        "datetime": datetime_text,
        "group_url": source_group_url,
        "images": representative_images or [],
    }

    chunks = []

    # 1. raw_content — original post content
    if (content or "").strip():
        chunks.append({
            "canonical_post_id": canonical_post_id,
            "source_analysis_id": analysis_id,
            "chunk_type": "raw_content",
            "chunk_text": content,
            "metadata": metadata_base,
        })

    if result_json:
        summary = result_json.get("summary")
        highlights = result_json.get("highlights") or []

        # 2. summary — LLM summary
        if summary:
            chunks.append({
                "canonical_post_id": canonical_post_id,
                "source_analysis_id": analysis_id,
                "chunk_type": "summary",
                "chunk_text": summary,
                "metadata": {**metadata_base, "analysis": result_json},
            })

        # 3. highlights — key bullet points
        if highlights:
            chunks.append({
                "canonical_post_id": canonical_post_id,
                "source_analysis_id": analysis_id,
                "chunk_type": "highlights",
                "chunk_text": "\n".join(f"- {item}" for item in highlights),
                "metadata": {**metadata_base, "analysis": result_json},
            })

        # 4. structured_facts — key-value facts
        facts = {
            "is_ban": result_json.get("is_ban"),
            "is_mua": result_json.get("is_mua"),
            "is_cho_thue": result_json.get("is_cho_thue"),
            "post_intent": result_json.get("post_intent"),
            "property_type": result_json.get("property_type"),
            "price_text": result_json.get("price_text"),
            "price_value_vnd": result_json.get("price_value_vnd"),
            "price_per_sqm_text": result_json.get("price_per_sqm_text"),
            "area_text": result_json.get("area_text"),
            "area_sqm": result_json.get("area_sqm"),
            "address_text": result_json.get("address_text"),
            "district": result_json.get("district"),
            "province": result_json.get("province"),
            "road_width_text": result_json.get("road_width_text"),
            "has_road_access": result_json.get("has_road_access"),
            "facing_direction": result_json.get("facing_direction"),
            "legal_status": result_json.get("legal_status"),
            "phones": result_json.get("phones"),
            "contact_name": result_json.get("contact_name"),
        }
        facts = {key: value for key, value in facts.items()
                  if value not in (None, [], "")}

        if facts:
            fact_lines = [
                f"[PHAN LOAI] Tin ban: {facts.get('is_ban', False)}, "
                f"Tin mua: {facts.get('is_mua', False)}, "
                f"Cho thue: {facts.get('is_cho_thue', False)}, "
                f"Intent: {facts.get('post_intent', '')}",
                f"Loai BDS: {facts.get('property_type', '')}" if "property_type" in facts else None,
                f"Gia: {facts.get('price_text', '')}" if "price_text" in facts else None,
                f"Gia/m2: {facts.get('price_per_sqm_text', '')}" if "price_per_sqm_text" in facts else None,
                f"Dien tich: {facts.get('area_text', '')}" if "area_text" in facts else None,
                f"Dia chi: {facts.get('address_text', '')}" if "address_text" in facts else None,
                f"Quan/huyen: {facts.get('district', '')}" if "district" in facts else None,
                f"Tinh: {facts.get('province', '')}" if "province" in facts else None,
                f"Mat tien: {facts.get('road_width_text', '')}" if "road_width_text" in facts else None,
                f"Huong: {facts.get('facing_direction', '')}" if "facing_direction" in facts else None,
                f"Phap ly: {facts.get('legal_status', '')}" if "legal_status" in facts else None,
                f"SDT: {', '.join(facts['phones'])}" if "phones" in facts else None,
                f"Nguoi LH: {facts.get('contact_name', '')}" if "contact_name" in facts else None,
            ]
            fact_text = "\n".join(line for line in fact_lines if line)
            chunks.append({
                "canonical_post_id": canonical_post_id,
                "source_analysis_id": analysis_id,
                "chunk_type": "structured_facts",
                "chunk_text": fact_text,
                "metadata": {**metadata_base, "analysis": result_json},
            })

            # 5. enriched_content — full enriched text (most semantic-rich chunk)
            intent_label = ""
            if result_json.get("is_ban"):
                intent_label = "TIN BAN"
            elif result_json.get("is_mua"):
                intent_label = "TIN MUA"
            elif result_json.get("is_cho_thue"):
                intent_label = "TIN CHO THUE"
            else:
                intent_label = f"TIN {result_json.get('post_intent', 'UNKNOWN')}"

            enriched_parts = [
                f"[{intent_label}] {result_json.get('title', '')}",
                f"Mo ta: {summary}" if summary else None,
                fact_text,
            ]
            enriched_text = "\n".join(part for part in enriched_parts if part)
            if enriched_text:
                chunks.append({
                    "canonical_post_id": canonical_post_id,
                    "source_analysis_id": analysis_id,
                    "chunk_type": "enriched_content",
                    "chunk_text": enriched_text,
                    "metadata": {**metadata_base, "analysis": result_json},
                })

        # 6. detailed_listing — summary + facts + raw (full content)
        detailed_parts = [
            result_json.get("title"),
            summary,
            fact_text if facts else None,
            content,
        ]
        detailed_text = "\n\n".join(part for part in detailed_parts if part)
        if detailed_text:
            chunks.append({
                "canonical_post_id": canonical_post_id,
                "source_analysis_id": analysis_id,
                "chunk_type": "detailed_listing",
                "chunk_text": detailed_text,
                "metadata": {**metadata_base, "analysis": result_json},
            })

    return [chunk for chunk in chunks if (chunk["chunk_text"] or "").strip()]


def insert_chunk(conn, chunk: dict, embedding: list[float], provider: str, model: str) -> None:
    vector_literal = vector_to_sql_literal(embedding)
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO search_chunks (
                canonical_post_id,
                source_analysis_id,
                chunk_type,
                chunk_text,
                chunk_hash,
                embedding_provider,
                embedding_model,
                embedding,
                metadata
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::vector, %s::jsonb)
            ON CONFLICT (chunk_hash) DO UPDATE
            SET chunk_text = EXCLUDED.chunk_text,
                embedding_provider = EXCLUDED.embedding_provider,
                embedding_model = EXCLUDED.embedding_model,
                embedding = EXCLUDED.embedding,
                metadata = EXCLUDED.metadata,
                updated_at = NOW()
            """,
            (
                chunk["canonical_post_id"],
                chunk.get("source_analysis_id"),
                chunk["chunk_type"],
                chunk["chunk_text"],
                chunk_hash(chunk),
                provider,
                model,
                vector_literal,
                json.dumps(chunk["metadata"], ensure_ascii=False),
            ),
        )


def rebuild_embeddings_for_post(
    conn,
    canonical_post_id: int,
    provider: str = DEFAULT_EMBEDDING_PROVIDER,
    model: str = DEFAULT_EMBEDDING_MODEL,
    dimensions: int = DEFAULT_EMBEDDING_DIM,
    base_url: str = DEFAULT_EMBEDDING_BASE_URL,
) -> int:
    row = fetch_candidate_by_post_id(conn, canonical_post_id)
    with conn.cursor() as cur:
        cur.execute("DELETE FROM search_chunks WHERE canonical_post_id = %s", (canonical_post_id,))
    if not row:
        return 0
    chunks = build_chunk_specs(row)
    if not chunks:
        return 0
    texts = [chunk["chunk_text"] for chunk in chunks]
    embeddings = embed_text_batch(
        texts,
        provider=provider,
        model=model,
        dimensions=dimensions,
        base_url=base_url,
    )
    for chunk, embedding in zip(chunks, embeddings):
        insert_chunk(conn, chunk, embedding, provider=provider, model=model)
    return len(chunks)


def main() -> int:
    load_dotenv(ROOT_DIR / ".env")
    args = build_parser().parse_args()
    with connect_db(get_database_url(args.database_url)) as conn:
        ensure_schema(conn)
        candidates = fetch_candidates(conn, args.limit)
        inserted = 0
        for row in candidates:
            chunks = build_chunk_specs(row)
            if not chunks:
                continue
            texts = [chunk["chunk_text"] for chunk in chunks]
            embeddings = embed_text_batch(
                texts,
                provider=args.provider,
                model=args.model,
                dimensions=args.dimensions,
                base_url=args.base_url,
            )
            for chunk, embedding in zip(chunks, embeddings):
                insert_chunk(conn, chunk, embedding, provider=args.provider, model=args.model)
                inserted += 1
        conn.commit()
    print(f"Inserted/updated {inserted} chunks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
