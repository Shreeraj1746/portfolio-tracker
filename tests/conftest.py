from __future__ import annotations

import re
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

# Ensure the repository root is importable in pytest runs.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app import create_app
from app.db import Base, get_db
from app.models import User
from app.security import hash_password
from app.services.portfolio import ensure_default_portfolio
from app.services.pricing import HistoricalPoint, PricingService, QuoteResult


class MockQuoteProvider:
    """Deterministic in-memory quote provider for tests."""

    def __init__(self) -> None:
        self.latest: dict[str, float] = {}
        self.history: dict[str, list[tuple[date, float]]] = {}

    def set_latest(self, symbol: str, price: float) -> None:
        self.latest[symbol.upper()] = float(price)

    def set_history(self, symbol: str, points: list[tuple[date, float]]) -> None:
        self.history[symbol.upper()] = points

    def get_latest_quote(self, symbol: str) -> QuoteResult:
        key = symbol.upper()
        if key not in self.latest:
            raise RuntimeError(f"No latest quote for {key}")
        return QuoteResult(
            symbol=key,
            price=self.latest[key],
            fetched_at=datetime.now(timezone.utc),
        )

    def get_historical_daily(self, symbol: str, start: date, end: date) -> list[HistoricalPoint]:
        key = symbol.upper()
        rows = self.history.get(key, [])
        return [
            HistoricalPoint(date=point_date, close=close)
            for point_date, close in rows
            if start <= point_date <= end
        ]


@pytest.fixture()
def test_env():
    provider = MockQuoteProvider()

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
        future=True,
    )
    SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, class_=Session)

    Base.metadata.create_all(bind=engine)

    app = create_app(pricing_service=PricingService(provider=provider, ttl_seconds=60), enable_startup_init=False)

    def override_get_db():
        db = SessionLocal()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db

    with SessionLocal() as db:
        ensure_default_portfolio(db)
        db.add(User(username="tester", password_hash=hash_password("password123")))
        db.commit()

    with TestClient(app) as client:
        yield {
            "app": app,
            "client": client,
            "session_factory": SessionLocal,
            "provider": provider,
        }

    engine.dispose()


@pytest.fixture()
def app(test_env):
    return test_env["app"]


@pytest.fixture()
def client(test_env):
    return test_env["client"]


@pytest.fixture()
def db_session_factory(test_env):
    return test_env["session_factory"]


@pytest.fixture()
def mock_provider(test_env):
    return test_env["provider"]


def extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', html)
    assert match, "csrf token not found in page"
    return match.group(1)


@pytest.fixture()
def csrf_token_for(client):
    def _get(path: str) -> str:
        response = client.get(path)
        assert response.status_code == 200
        return extract_csrf(response.text)

    return _get


@pytest.fixture()
def authed_client(client, csrf_token_for):
    token = csrf_token_for("/login")
    response = client.post(
        "/login",
        data={
            "username": "tester",
            "password": "password123",
            "csrf_token": token,
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    return client
