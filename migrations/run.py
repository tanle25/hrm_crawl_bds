"""
BDS Agent migration runner.

Ensures the schema_migrations tracking table exists, then applies pending
migrations in version order. Each migration is idempotent (can run multiple
times safely) and records itself in schema_migrations.

Usage:
    python migrations/run.py
    python migrations/run.py --dry-run
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parent.parent
load_dotenv(ROOT_DIR / ".env")

# Add project root to sys.path so 'db' and other modules are importable
sys.path.insert(0, str(ROOT_DIR))

MIGRATIONS_DIR = ROOT_DIR / "migrations"
MARKER_SQL = """
-- Migration: {version}
-- Description: {description}
-- Direction: {direction}

{up_sql}

INSERT INTO schema_migrations (version) VALUES ('{version}')
ON CONFLICT (version) DO NOTHING;
"""


def connect_db():
    import os
    from db import connect_db as _connect_db
    return _connect_db(os.getenv("DATABASE_URL"))


def ensure_tracker_table(conn) -> None:
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


def get_applied_versions(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT version FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def parse_migration_file(path: Path) -> tuple[str, str, str]:
    """Extract version, description, and SQL from a .sql file."""
    content = path.read_text(encoding="utf-8")
    version = path.stem  # "0001_initial"
    description = ""
    up_sql = content

    # Strip -- Migration: X lines and -- Description: Y lines
    lines = content.splitlines()
    clean_lines: list[str] = []
    for line in lines:
        if line.startswith("-- Migration:") or line.startswith("-- Description:") or line.startswith("-- Direction:"):
            if "Description:" in line:
                description = line.split("Description:", 1)[1].strip()
            continue
        clean_lines.append(line)

    # Remove trailing empty lines
    while clean_lines and not clean_lines[-1].strip():
        clean_lines.pop()
    up_sql = "\n".join(clean_lines).strip()

    return version, description, up_sql


def migrate(dry_run: bool = False) -> list[str]:
    """Apply pending migrations. Returns list of applied versions."""
    conn = connect_db()
    ensure_tracker_table(conn)
    applied = get_applied_versions(conn)
    print(f"[migrations] Already applied: {sorted(applied) or '(none)'}")

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    applied_versions: list[str] = []

    for path in migration_files:
        version, description, up_sql = parse_migration_file(path)
        if version in applied:
            print(f"  {version} — already applied, skipping")
            continue

        print(f"  {version} — {description or '(no description)'}")

        if dry_run:
            print(f"    [DRY RUN] Would execute {len(up_sql)} bytes of SQL")
            applied_versions.append(version)
            continue

        try:
            with conn.cursor() as cur:
                cur.execute(up_sql)
            conn.commit()
            applied_versions.append(version)
            print(f"    ✓ {version} applied successfully")
        except Exception as exc:
            conn.rollback()
            print(f"    ✗ {version} FAILED: {exc}")
            raise

    return applied_versions


def rollback(version: str) -> None:
    """Roll back a specific migration by version (caller implements down-SQL)."""
    print(f"[migrations] Rollback of '{version}' — implement down-SQL manually or re-init schema.")
    # Downward migrations are application-specific; for vector dimension changes
    # the safest approach is: dump data → re-init schema → restore.
    sys.exit(1)


def main() -> int:
    parser = argparse.ArgumentParser(description="BDS Agent migration runner")
    parser.add_argument("--dry-run", action="store_true", help="Show what would be applied without executing")
    parser.add_argument("--rollback", metavar="VERSION", help="Roll back a specific migration version")
    args = parser.parse_args()

    if args.rollback:
        rollback(args.rollback)

    applied = migrate(dry_run=args.dry_run)
    if applied:
        print(f"\n[migrations] Done. Applied {len(applied)} migration(s): {applied}")
    else:
        print("\n[migrations] Done. No pending migrations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
