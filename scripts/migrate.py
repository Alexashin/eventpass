from __future__ import annotations

import sys
from pathlib import Path

# `python scripts/migrate.py` sets sys.path[0] to /app/scripts. Add the project
# root explicitly so this command behaves the same locally and in Docker.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from alembic import command
from alembic.config import Config
from sqlalchemy import inspect

from app.db import engine

EXPECTED_LEGACY_TABLES = {"users", "tickets", "purchases", "audit_logs"}


def main() -> None:
    cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    tables = set(inspect(engine).get_table_names())
    if not tables:
        command.upgrade(cfg, "head")
        return
    if "alembic_version" in tables:
        command.upgrade(cfg, "head")
        return
    if EXPECTED_LEGACY_TABLES.issubset(tables):
        # v3 created this schema with SQLAlchemy create_all. Stamp it once so
        # subsequent releases can use ordinary Alembic migrations.
        command.stamp(cfg, "head")
        return
    raise RuntimeError(f"Unknown database schema, refusing automatic migration: {sorted(tables)}")


if __name__ == "__main__":
    main()
