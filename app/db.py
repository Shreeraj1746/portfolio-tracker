from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """Base class for SQLAlchemy models."""


connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, class_=Session
)


def init_db() -> None:
    """Create all configured tables if they do not exist."""
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _ensure_transaction_columns()


def _ensure_transaction_columns() -> None:
    """Backfill columns for existing SQLite databases without migrations."""
    inspector = inspect(engine)
    if "transactions" not in inspector.get_table_names():
        return

    existing_columns = {column["name"] for column in inspector.get_columns("transactions")}
    if "invested_override" in existing_columns:
        return

    with engine.begin() as conn:
        conn.execute(text("ALTER TABLE transactions ADD COLUMN invested_override FLOAT"))


def get_db() -> Generator[Session, None, None]:
    """Yield a request-scoped SQLAlchemy session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
