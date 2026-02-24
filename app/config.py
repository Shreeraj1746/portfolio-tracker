from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Application configuration loaded from environment variables."""

    app_name: str = os.getenv("APP_NAME", "Portfolio Tracker")
    secret_key: str = os.getenv("SECRET_KEY", "change-me-in-production")
    database_url: str = os.getenv("DATABASE_URL", "sqlite:///./portfolio.db")
    quote_ttl_seconds: int = int(os.getenv("QUOTE_TTL_SECONDS", "60"))
    session_cookie_name: str = os.getenv("SESSION_COOKIE_NAME", "portfolio_session")
    session_https_only: bool = (
        os.getenv("SESSION_HTTPS_ONLY", "false").lower() == "true"
    )
    sqlite_busy_timeout_ms: int = int(os.getenv("SQLITE_BUSY_TIMEOUT_MS", "30000"))
    sqlite_journal_mode: str = os.getenv("SQLITE_JOURNAL_MODE", "WAL")


settings = Settings()
