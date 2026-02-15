from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from app.config import settings
from app.db import SessionLocal, init_db
from app.routes import assets, auth, baskets, dashboard
from app.services.portfolio import ensure_default_portfolio
from app.services.pricing import PricingService, QuoteProvider, YFinanceProvider


class _UnavailableProvider:
    """Fallback provider used if yfinance cannot be initialized."""

    def get_latest_quote(self, symbol: str) -> Any:
        raise RuntimeError("Quote provider is unavailable")

    def get_historical_daily(self, symbol: str, start: Any, end: Any) -> list[Any]:
        return []


def _currency(value: Any) -> str:
    if value is None:
        return "-"
    return f"${float(value):,.2f}"


def _number(value: Any, digits: int = 4) -> str:
    if value is None:
        return "-"
    return f"{float(value):,.{digits}f}"


def _pct(value: Any) -> str:
    if value is None:
        return "-"
    return f"{float(value):.2f}%"


def _dt(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S UTC")
    return str(value)


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(title=settings.app_name)

    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.secret_key,
        session_cookie=settings.session_cookie_name,
        same_site="lax",
        https_only=settings.session_https_only,
        max_age=60 * 60 * 24 * 7,
    )

    static_dir = Path("app/static")
    templates = Jinja2Templates(directory="app/templates")
    templates.env.filters["currency"] = _currency
    templates.env.filters["number"] = _number
    templates.env.filters["pct"] = _pct
    templates.env.filters["dt"] = _dt

    app.state.templates = templates

    try:
        provider: QuoteProvider = YFinanceProvider()
    except Exception:
        provider = _UnavailableProvider()  # type: ignore[assignment]
    app.state.pricing_service = PricingService(provider=provider, ttl_seconds=settings.quote_ttl_seconds)

    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(assets.router)
    app.include_router(baskets.router)

    @app.on_event("startup")
    def startup() -> None:
        init_db()
        with SessionLocal() as db:
            ensure_default_portfolio(db)
            db.commit()

    return app
