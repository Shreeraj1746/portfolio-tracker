from __future__ import annotations

from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings


class Base(DeclarativeBase):
    """Base class for SQLAlchemy models."""


connect_args = {}
_is_sqlite = settings.database_url.startswith("sqlite")
if _is_sqlite:
    busy_timeout_ms = max(settings.sqlite_busy_timeout_ms, 0)
    connect_args = {
        "check_same_thread": False,
        "timeout": busy_timeout_ms / 1000.0,
    }

engine = create_engine(settings.database_url, connect_args=connect_args, future=True)
SessionLocal = sessionmaker(
    bind=engine, autoflush=False, autocommit=False, class_=Session
)

if _is_sqlite:
    _ALLOWED_JOURNAL_MODES = {
        "DELETE",
        "TRUNCATE",
        "PERSIST",
        "MEMORY",
        "WAL",
        "OFF",
    }

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection, _connection_record) -> None:
        busy_timeout_ms = max(settings.sqlite_busy_timeout_ms, 0)
        journal_mode = settings.sqlite_journal_mode.strip().upper() or "WAL"
        if journal_mode not in _ALLOWED_JOURNAL_MODES:
            journal_mode = "WAL"

        cursor = dbapi_connection.cursor()
        cursor.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
        cursor.execute(f"PRAGMA journal_mode={journal_mode}")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.close()


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
