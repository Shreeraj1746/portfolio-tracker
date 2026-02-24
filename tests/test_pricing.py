from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from app.models import QuoteCache
from app.services.pricing import PricingService, QuoteResult


class _StaticQuoteProvider:
    def __init__(self, latest_price: float) -> None:
        self.latest_price = latest_price

    def get_latest_quote(self, symbol: str) -> QuoteResult:
        return QuoteResult(
            symbol=symbol.upper(),
            price=self.latest_price,
            fetched_at=datetime.now(timezone.utc),
        )

    def get_historical_daily(self, symbol: str, start: date, end: date) -> list:
        _ = (symbol, start, end)
        return []


def test_get_quote_returns_stale_cache_when_flush_hits_db_lock(
    db_session_factory,
    monkeypatch,
):
    stale_price = 65000.0
    stale_at = datetime.now(timezone.utc) - timedelta(hours=2)

    with db_session_factory() as db:
        db.add(QuoteCache(symbol="BTC", price=stale_price, fetched_at=stale_at))
        db.commit()

        service = PricingService(provider=_StaticQuoteProvider(66000.0), ttl_seconds=0)
        original_flush = db.flush

        def _raise_locked_flush() -> None:
            raise OperationalError(
                "UPDATE quote_cache SET price=?, fetched_at=? WHERE id=?",
                {},
                RuntimeError("database is locked"),
            )

        monkeypatch.setattr(db, "flush", _raise_locked_flush)
        quote = service.get_quote(db, "BTC")
        monkeypatch.setattr(db, "flush", original_flush)

        assert quote is not None
        assert quote.stale is True
        assert quote.price == stale_price
        assert "cache write issue" in (quote.warning or "")

        # Rollback must have happened, otherwise this query raises PendingRollbackError.
        cached = db.scalar(select(QuoteCache).where(QuoteCache.symbol == "BTC"))
        assert cached is not None
        assert float(cached.price) == stale_price


def test_get_quote_returns_none_when_lock_occurs_without_cached_value(
    db_session_factory,
    monkeypatch,
):
    with db_session_factory() as db:
        service = PricingService(provider=_StaticQuoteProvider(123.45), ttl_seconds=0)
        original_flush = db.flush

        def _raise_locked_flush() -> None:
            raise OperationalError(
                "INSERT INTO quote_cache (symbol, price, fetched_at) VALUES (?, ?, ?)",
                {},
                RuntimeError("database is locked"),
            )

        monkeypatch.setattr(db, "flush", _raise_locked_flush)
        quote = service.get_quote(db, "AAPL")
        monkeypatch.setattr(db, "flush", original_flush)

        assert quote is None

        # Session remains usable after rollback.
        cached = db.scalar(select(QuoteCache).where(QuoteCache.symbol == "AAPL"))
        assert cached is None
