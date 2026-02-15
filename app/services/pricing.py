from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import QuoteCache


@dataclass
class QuoteResult:
    symbol: str
    price: float
    fetched_at: datetime
    stale: bool = False
    warning: str | None = None


@dataclass
class HistoricalPoint:
    date: date
    close: float


class QuoteProvider(Protocol):
    """Provider interface for live and historical quotes."""

    def get_latest_quote(self, symbol: str) -> QuoteResult:
        ...

    def get_historical_daily(self, symbol: str, start: date, end: date) -> list[HistoricalPoint]:
        ...


class YFinanceProvider:
    """Best-effort provider backed by the free yfinance library."""

    def __init__(self) -> None:
        try:
            import yfinance as yf
        except Exception as exc:  # pragma: no cover - exercised in runtime, not tests
            raise RuntimeError("yfinance is not available") from exc
        self._yf = yf

    def get_latest_quote(self, symbol: str) -> QuoteResult:
        ticker = self._yf.Ticker(symbol)
        hist = ticker.history(period="5d", interval="1d", auto_adjust=False)
        if hist.empty:
            raise RuntimeError(f"No quote data found for {symbol}")

        close_series = hist["Close"].dropna()
        if close_series.empty:
            raise RuntimeError(f"No close data found for {symbol}")

        price = float(close_series.iloc[-1])
        return QuoteResult(symbol=symbol.upper(), price=price, fetched_at=datetime.now(timezone.utc))

    def get_historical_daily(self, symbol: str, start: date, end: date) -> list[HistoricalPoint]:
        ticker = self._yf.Ticker(symbol)
        # yfinance end date is exclusive, so shift by one day.
        hist = ticker.history(
            start=start.isoformat(),
            end=(end + timedelta(days=1)).isoformat(),
            interval="1d",
            auto_adjust=False,
        )
        if hist.empty:
            return []

        points: list[HistoricalPoint] = []
        close_series = hist["Close"].dropna()
        for idx, close in close_series.items():
            point_date = idx.date() if hasattr(idx, "date") else idx
            points.append(HistoricalPoint(date=point_date, close=float(close)))
        return points


class PricingService:
    """Handles quote lookups with SQLite-backed cache and provider fallback."""

    def __init__(self, provider: QuoteProvider, ttl_seconds: int = 60) -> None:
        self.provider = provider
        self.ttl_seconds = ttl_seconds

    def get_quote(self, db: Session, symbol: str) -> QuoteResult | None:
        clean_symbol = symbol.strip().upper()
        now = datetime.now(timezone.utc)

        cached = db.scalar(select(QuoteCache).where(QuoteCache.symbol == clean_symbol))
        if cached:
            age_seconds = (now - self._as_utc(cached.fetched_at)).total_seconds()
            if age_seconds <= self.ttl_seconds:
                return QuoteResult(
                    symbol=clean_symbol,
                    price=float(cached.price),
                    fetched_at=self._as_utc(cached.fetched_at),
                )

        try:
            fresh = self.provider.get_latest_quote(clean_symbol)
            if cached is None:
                cached = QuoteCache(symbol=clean_symbol, price=fresh.price, fetched_at=self._as_utc(fresh.fetched_at))
                db.add(cached)
            else:
                cached.price = fresh.price
                cached.fetched_at = self._as_utc(fresh.fetched_at)
            db.flush()
            return QuoteResult(symbol=clean_symbol, price=fresh.price, fetched_at=self._as_utc(fresh.fetched_at))
        except Exception as exc:
            if cached is not None:
                return QuoteResult(
                    symbol=clean_symbol,
                    price=float(cached.price),
                    fetched_at=self._as_utc(cached.fetched_at),
                    stale=True,
                    warning=f"Using cached quote due to provider issue: {exc}",
                )
            return None

    def get_historical_daily(self, symbol: str, start: date, end: date) -> list[HistoricalPoint]:
        try:
            return self.provider.get_historical_daily(symbol.strip().upper(), start, end)
        except Exception:
            return []

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
