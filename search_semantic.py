import argparse
from pathlib import Path

from dotenv import load_dotenv

from db import connect_db, ensure_schema, get_database_url
from vectorizer import (
    DEFAULT_EMBEDDING_BASE_URL,
    DEFAULT_EMBEDDING_DIM,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_EMBEDDING_PROVIDER,
    embed_text,
    vector_to_sql_literal,
)


ROOT_DIR = Path(__file__).resolve().parent


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Semantic search over crawled BDS posts.")
    parser.add_argument("query")
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--provider", default=DEFAULT_EMBEDDING_PROVIDER)
    parser.add_argument("--model", default=DEFAULT_EMBEDDING_MODEL)
    parser.add_argument("--dimensions", type=int, default=DEFAULT_EMBEDDING_DIM)
    parser.add_argument("--base-url", default=DEFAULT_EMBEDDING_BASE_URL)
    parser.add_argument("--limit", type=int, default=5)
    return parser


def main() -> int:
    load_dotenv(ROOT_DIR / ".env")
    args = build_parser().parse_args()
    query_embedding = embed_text(
        args.query,
        provider=args.provider,
        model=args.model,
        dimensions=args.dimensions,
        base_url=args.base_url,
    )
    vector_literal = vector_to_sql_literal(query_embedding)
    with connect_db(get_database_url(args.database_url)) as conn:
        ensure_schema(conn)
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT sc.id,
                       sc.canonical_post_id,
                       sc.chunk_type,
                       LEFT(sc.chunk_text, 240),
                       cp.representative_author,
                       cp.primary_post_url,
                       1 - (sc.embedding <=> %s::vector) AS similarity
                FROM search_chunks sc
                JOIN canonical_posts cp ON cp.id = sc.canonical_post_id
                ORDER BY sc.embedding <=> %s::vector
                LIMIT %s
                """,
                (vector_literal, vector_literal, args.limit),
            )
            rows = cur.fetchall()
    for row in rows:
        print(f"[{row[0]}] canonical={row[1]} type={row[2]} sim={row[6]:.4f}")
        print(f"author={row[4]} post_url={row[5]}")
        print(row[3])
        print("-" * 80)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
