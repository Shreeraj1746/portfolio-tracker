from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Asset, AssetType, Basket, BasketAsset, Group, Portfolio, Transaction, TransactionType
from app.services.pricing import PricingService


@dataclass
class MarketPosition:
    quantity: float
    avg_cost: float


@dataclass
class ManualPosition:
    current_value: float
    invested_total: float
    unrealized_pnl: float | None
    as_of: datetime | None


@dataclass
class PositionRow:
    asset_id: int
    symbol: str
    name: str
    group_name: str
    asset_type: AssetType
    quantity: float | None
    avg_cost: float | None
    current_price: float | None
    current_value: float
    unrealized_pnl: float | None
    allocation_pct: float
    as_of: datetime | None
    quote_stale: bool


@dataclass
class DashboardSnapshot:
    positions: list[PositionRow]
    group_totals: dict[str, dict[str, float]]
    total_value: float
    total_unrealized_pnl: float


@dataclass
class BasketSeriesPoint:
    date: date
    value: float


class InvalidTransaction(Exception):
    """Domain error raised for invalid transaction sequences."""


def _tx_type(value: Transaction | object) -> str:
    tx_type = getattr(value, "type")
    if isinstance(tx_type, TransactionType):
        return tx_type.value
    return str(tx_type)


def _tx_timestamp(value: Transaction | object) -> datetime:
    return getattr(value, "timestamp")


def _tx_id(value: Transaction | object) -> int:
    tx_id = getattr(value, "id", 0)
    return int(tx_id or 0)


def _tx_quantity(value: Transaction | object) -> float:
    return float(getattr(value, "quantity") or 0.0)


def _tx_price(value: Transaction | object) -> float:
    return float(getattr(value, "price") or 0.0)


def _tx_fees(value: Transaction | object) -> float:
    return float(getattr(value, "fees") or 0.0)


def _tx_manual_value(value: Transaction | object) -> float:
    return float(getattr(value, "manual_value") or 0.0)


def sort_transactions(transactions: Iterable[Transaction | object]) -> list[Transaction | object]:
    """Sort transactions deterministically by timestamp then ID."""
    return sorted(transactions, key=lambda tx: (_tx_timestamp(tx), _tx_id(tx)))


def compute_market_position(transactions: Iterable[Transaction | object]) -> MarketPosition:
    """Compute weighted average cost basis from BUY/SELL transactions."""
    qty = 0.0
    avg_cost = 0.0

    for tx in sort_transactions(transactions):
        tx_type = _tx_type(tx)
        if tx_type == TransactionType.MANUAL_VALUE_UPDATE.value:
            continue

        quantity = _tx_quantity(tx)
        price = _tx_price(tx)
        fees = _tx_fees(tx)

        if tx_type == TransactionType.BUY.value:
            if quantity <= 0:
                raise InvalidTransaction("BUY quantity must be positive")
            new_cost_total = (qty * avg_cost) + (quantity * price) + fees
            qty += quantity
            avg_cost = new_cost_total / qty if qty > 0 else 0.0
        elif tx_type == TransactionType.SELL.value:
            if quantity <= 0:
                raise InvalidTransaction("SELL quantity must be positive")
            if quantity - qty > 1e-9:
                raise InvalidTransaction("Cannot sell more than currently held quantity")
            qty -= quantity
            if qty <= 1e-9:
                qty = 0.0
                avg_cost = 0.0
        else:
            raise InvalidTransaction(f"Unsupported transaction type: {tx_type}")

    return MarketPosition(quantity=qty, avg_cost=avg_cost)


def compute_manual_position(transactions: Iterable[Transaction | object]) -> ManualPosition:
    """Compute manual-asset value using latest MANUAL_VALUE_UPDATE plus optional invested BUY cost."""
    invested_total = 0.0
    latest_value: float | None = None
    latest_value_at: datetime | None = None

    for tx in sort_transactions(transactions):
        tx_type = _tx_type(tx)
        if tx_type == TransactionType.BUY.value:
            invested_total += (_tx_quantity(tx) * _tx_price(tx)) + _tx_fees(tx)
        elif tx_type == TransactionType.MANUAL_VALUE_UPDATE.value:
            latest_value = _tx_manual_value(tx)
            latest_value_at = _tx_timestamp(tx)

    current_value = latest_value if latest_value is not None else 0.0
    unrealized = current_value - invested_total if invested_total > 0 else None
    return ManualPosition(
        current_value=current_value,
        invested_total=invested_total,
        unrealized_pnl=unrealized,
        as_of=latest_value_at,
    )


def compute_allocation_percentages(values_by_key: dict[str, float]) -> dict[str, float]:
    """Return allocation percentages for each key, summing to ~100."""
    total = sum(values_by_key.values())
    if total <= 0:
        return {key: 0.0 for key in values_by_key}
    return {key: (value / total) * 100.0 for key, value in values_by_key.items()}


def ensure_default_portfolio(db: Session) -> Portfolio:
    """Ensure one default portfolio exists for the MVP."""
    portfolio = db.scalar(select(Portfolio).order_by(Portfolio.id))
    if portfolio:
        return portfolio
    portfolio = Portfolio(name="Primary")
    db.add(portfolio)
    db.flush()
    return portfolio


def compute_asset_position(
    db: Session,
    pricing_service: PricingService,
    asset: Asset,
    transactions: list[Transaction],
) -> PositionRow:
    """Compute one asset row for the dashboard table."""
    group_name = asset.group.name if asset.group else "Ungrouped"

    if asset.asset_type == AssetType.MARKET:
        market = compute_market_position(transactions)
        quote = pricing_service.get_quote(db, asset.symbol)
        current_price = quote.price if quote else None
        current_value = market.quantity * current_price if current_price is not None else 0.0
        unrealized = (current_price - market.avg_cost) * market.quantity if current_price is not None else None
        return PositionRow(
            asset_id=asset.id,
            symbol=asset.symbol,
            name=asset.name,
            group_name=group_name,
            asset_type=asset.asset_type,
            quantity=market.quantity,
            avg_cost=market.avg_cost,
            current_price=current_price,
            current_value=current_value,
            unrealized_pnl=unrealized,
            allocation_pct=0.0,
            as_of=quote.fetched_at if quote else None,
            quote_stale=quote.stale if quote else True,
        )

    manual = compute_manual_position(transactions)
    return PositionRow(
        asset_id=asset.id,
        symbol=asset.symbol,
        name=asset.name,
        group_name=group_name,
        asset_type=asset.asset_type,
        quantity=1.0,
        avg_cost=manual.invested_total if manual.invested_total > 0 else None,
        current_price=manual.current_value,
        current_value=manual.current_value,
        unrealized_pnl=manual.unrealized_pnl,
        allocation_pct=0.0,
        as_of=manual.as_of,
        quote_stale=False,
    )


def build_dashboard_snapshot(db: Session, pricing_service: PricingService, portfolio_id: int) -> DashboardSnapshot:
    """Build full dashboard data for one portfolio."""
    assets = list(
        db.scalars(
            select(Asset)
            .where(Asset.portfolio_id == portfolio_id, Asset.is_archived.is_(False))
            .options(selectinload(Asset.group))
        )
    )
    assets.sort(key=lambda asset: ((asset.group.name.lower() if asset.group else ""), asset.symbol.lower()))

    tx_rows = list(
        db.scalars(
            select(Transaction)
            .where(Transaction.portfolio_id == portfolio_id)
            .order_by(Transaction.timestamp.asc(), Transaction.id.asc())
        )
    )
    tx_by_asset: dict[int, list[Transaction]] = defaultdict(list)
    for tx in tx_rows:
        tx_by_asset[tx.asset_id].append(tx)

    positions: list[PositionRow] = []
    for asset in assets:
        position = compute_asset_position(db, pricing_service, asset, tx_by_asset.get(asset.id, []))
        positions.append(position)

    totals_by_group: dict[str, dict[str, float]] = defaultdict(lambda: {"value": 0.0, "pnl": 0.0})
    total_value = 0.0
    total_unrealized = 0.0

    for position in positions:
        totals_by_group[position.group_name]["value"] += position.current_value
        pnl = position.unrealized_pnl if position.unrealized_pnl is not None else 0.0
        totals_by_group[position.group_name]["pnl"] += pnl
        total_value += position.current_value
        total_unrealized += pnl

    allocation_map = compute_allocation_percentages({str(row.asset_id): row.current_value for row in positions})
    for row in positions:
        row.allocation_pct = allocation_map.get(str(row.asset_id), 0.0)

    return DashboardSnapshot(
        positions=positions,
        group_totals=dict(totals_by_group),
        total_value=total_value,
        total_unrealized_pnl=total_unrealized,
    )


def validate_asset_transactions(asset: Asset, transactions: Iterable[Transaction | object]) -> None:
    """Validate full transaction history for an asset."""
    tx_list = list(transactions)
    if asset.asset_type == AssetType.MARKET:
        compute_market_position(tx_list)
        for tx in tx_list:
            if _tx_type(tx) == TransactionType.MANUAL_VALUE_UPDATE.value:
                raise InvalidTransaction("Market assets do not support MANUAL_VALUE_UPDATE transactions")
    else:
        for tx in tx_list:
            tx_type = _tx_type(tx)
            if tx_type == TransactionType.SELL.value:
                raise InvalidTransaction("Manual assets do not support SELL transactions in MVP")
        compute_manual_position(tx_list)


def build_asset_history(
    pricing_service: PricingService,
    asset: Asset,
    days: int = 120,
) -> list[BasketSeriesPoint]:
    """Build per-asset daily close history for charting."""
    if asset.asset_type != AssetType.MARKET:
        return []

    end = date.today()
    start = end - timedelta(days=days)
    points = pricing_service.get_historical_daily(asset.symbol, start, end)
    return [BasketSeriesPoint(date=point.date, value=point.close) for point in points]


def build_basket_normalized_series(
    pricing_service: PricingService,
    basket_links: list[BasketAsset],
    days: int = 120,
) -> list[BasketSeriesPoint]:
    """Build basket series normalized to 100 at the start date."""
    market_links = [link for link in basket_links if link.asset.asset_type == AssetType.MARKET]
    if not market_links:
        return []

    weights: list[float] = []
    explicit_weights = [link.weight for link in market_links if link.weight is not None]
    if explicit_weights and sum(explicit_weights) > 0:
        for link in market_links:
            weights.append(float(link.weight or 0.0))
    else:
        equal = 1.0 / len(market_links)
        weights = [equal for _ in market_links]

    weight_sum = sum(weights)
    if weight_sum <= 0:
        return []
    normalized_weights = [weight / weight_sum for weight in weights]

    end = date.today()
    start = end - timedelta(days=days)

    all_dates: set[date] = set()
    history_by_symbol: list[dict[date, float]] = []
    base_by_symbol: list[float] = []

    for link in market_links:
        points = pricing_service.get_historical_daily(link.asset.symbol, start, end)
        if not points:
            history_by_symbol.append({})
            base_by_symbol.append(0.0)
            continue

        series = {point.date: point.close for point in points}
        history_by_symbol.append(series)
        all_dates.update(series.keys())
        first_date = sorted(series.keys())[0]
        base_by_symbol.append(series[first_date])

    if not all_dates:
        return []

    ordered_dates = sorted(all_dates)
    last_seen: list[float | None] = [None for _ in history_by_symbol]
    output: list[BasketSeriesPoint] = []

    for day in ordered_dates:
        composite = 0.0
        for idx, series in enumerate(history_by_symbol):
            if day in series:
                last_seen[idx] = series[day]
            current = last_seen[idx]
            base = base_by_symbol[idx]
            if current is None or base <= 0:
                continue
            normalized = (current / base) * 100.0
            composite += normalized_weights[idx] * normalized
        output.append(BasketSeriesPoint(date=day, value=composite))

    return output


def get_portfolio_groups(db: Session, portfolio_id: int) -> list[Group]:
    """Return all groups sorted by name."""
    return list(
        db.scalars(
            select(Group)
            .where(Group.portfolio_id == portfolio_id)
            .order_by(Group.name.asc())
        )
    )


def get_portfolio_assets(db: Session, portfolio_id: int) -> list[Asset]:
    """Return active assets sorted by symbol."""
    return list(
        db.scalars(
            select(Asset)
            .where(Asset.portfolio_id == portfolio_id, Asset.is_archived.is_(False))
            .options(selectinload(Asset.group))
            .order_by(Asset.symbol.asc())
        )
    )


def get_portfolio_baskets(db: Session, portfolio_id: int) -> list[Basket]:
    """Return baskets sorted by name."""
    return list(
        db.scalars(
            select(Basket)
            .where(Basket.portfolio_id == portfolio_id)
            .order_by(Basket.name.asc())
        )
    )
