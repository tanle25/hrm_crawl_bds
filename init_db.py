import argparse
from pathlib import Path

from dotenv import load_dotenv

from db import connect_db, ensure_schema, get_database_url


ROOT_DIR = Path(__file__).resolve().parent


def main() -> int:
    load_dotenv(ROOT_DIR / ".env")
    parser = argparse.ArgumentParser(description="Initialize PostgreSQL schema for BDS Agent.")
    parser.add_argument("--database-url", default=None)
    args = parser.parse_args()
    with connect_db(get_database_url(args.database_url)) as conn:
        ensure_schema(conn)
    print("Database schema initialized.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
