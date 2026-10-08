"""SQLite engine/session wiring for FoodFriday."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.getenv("FOODFRIDAY_DATA_DIR", BASE_DIR / "data"))

DATA_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.getenv("FOODFRIDAY_DATABASE_URL", f"sqlite:///{DATA_DIR / 'foodfriday.db'}")

engine = create_engine(
    DATABASE_URL,
    echo=False,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session


def init_db() -> None:
    """Create tables and bring older databases up to date (ADD-only)."""
    from app import models  # noqa: F401  (ensure models are registered)
    from app.models import Base

    Base.metadata.create_all(engine)
    if DATABASE_URL.startswith("sqlite"):
        with engine.connect() as conn:
            conn.exec_driver_sql("PRAGMA journal_mode=WAL")
            conn.exec_driver_sql("PRAGMA foreign_keys=ON")
    _apply_column_migrations()
    _migrate_legacy_email_settings()


def _migrate_legacy_email_settings() -> None:
    """Move the old single-account Gmail settings keys into email_accounts."""
    from app.deal_scan import migrate_legacy_gmail_settings

    migrate_legacy_gmail_settings()


def _apply_column_migrations() -> None:
    """ADD any model columns missing from existing tables (SQLite only).

    Migrations only ever ADD columns, and every column stays nullable
    so ADD COLUMN is always safe.
    """
    from app.models import Base

    with engine.connect() as conn:
        for table in Base.metadata.sorted_tables:
            present = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table.name})")}
            for column in table.columns:
                if column.name not in present:
                    coltype = column.type.compile(dialect=engine.dialect)
                    default_sql = ""
                    if column.server_default is not None:
                        arg = column.server_default.arg
                        default_sql = f" DEFAULT {arg}"
                    conn.exec_driver_sql(
                        f"ALTER TABLE {table.name} ADD COLUMN {column.name} {coltype}{default_sql}"
                    )
