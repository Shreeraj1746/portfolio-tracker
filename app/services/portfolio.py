from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import (
    Asset,
    AssetType,
    Basket,
    BasketAsset,
    Group,
    Portfolio,
    Transaction,
    TransactionType,
)
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
    row_kind: str
    detail_path: str
    in_basket_member: bool = False


@dataclass
class DashboardSnapshot:
    positions: list[PositionRow]
    group_totals: dict[str, dict[str, float]]
    total_value: float
    total_unrealized_pnl: float
    basket_member_asset_ids: set[int]


@dataclass
class AllocationChartData:
    labels: list[str]
    values: list[float]
    percentages: list[float]


@dataclass
class BasketSeriesPoint:
    date: date
    value: float


@dataclass
class BasketSeriesResult:
    points: list[BasketSeriesPoint]
    missing_symbols: list[str]
    error_message: str | None


class InvalidTransaction(Exception):
    """Domain error raised for invalid transaction sequences."""


def _tx_type(value: Transaction | object) -> str:
    tx_type = getattr(value, "type")
    if isinstance(tx_type, TransactionType):
        return tx_type.value
    return str(tx_type)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _tx_timestamp(value: Transaction | object) -> datetime:
    return _as_utc(getattr(value, "timestamp"))


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


def sort_transactions(
    transactions: Iterable[Transaction | object],
) -> list[Transaction | object]:
    """Sort transactions deterministically by timestamp then ID."""
    return sorted(transactions, key=lambda tx: (_tx_timestamp(tx), _tx_id(tx)))


def compute_market_position(
    transactions: Iterable[Transaction | object],
) -> MarketPosition:
    """Compute weighted-average quantity and cost basis from BUY/SELL transactions."""
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
            if price <= 0:
                raise InvalidTransaction("BUY price must be positive")
            if fees < 0:
                raise InvalidTransaction("Fees cannot be negative")
            new_cost_total = (qty * avg_cost) + (quantity * price) + fees
            qty += quantity
            avg_cost = new_cost_total / qty if qty > 0 else 0.0
        elif tx_type == TransactionType.SELL.value:
            if quantity <= 0:
                raise InvalidTransaction("SELL quantity must be positive")
            if price <= 0:
                raise InvalidTransaction("SELL price must be positive")
            if quantity - qty > 1e-9:
                raise InvalidTransaction("Cannot sell more than currently held quantity")
            qty -= quantity
            if qty <= 1e-9:
                qty = 0.0
                avg_cost = 0.0
        else:
            raise InvalidTransaction(f"Unsupported transaction type: {tx_type}")

    return MarketPosition(quantity=qty, avg_cost=avg_cost)


def compute_manual_position(
    transactions: Iterable[Transaction | object],
) -> ManualPosition:
    """Compute manual-asset value using manual updates plus optional invested BUY cost."""
    invested_total = 0.0
    latest_value: float | None = None
    latest_value_at: datetime | None = None

    for tx in sort_transactions(transactions):
        tx_type = _tx_type(tx)

        if tx_type == TransactionType.BUY.value:
            quantity = _tx_quantity(tx)
            price = _tx_price(tx)
            fees = _tx_fees(tx)
            if quantity <= 0:
                raise InvalidTransaction("Manual BUY quantity must be positive")
            if price < 0:
                raise InvalidTransaction("Manual BUY price cannot be negative")
            if fees < 0:
                raise InvalidTransaction("Fees cannot be negative")
            invested_total += (quantity * price) + fees
        elif tx_type == TransactionType.SELL.value:
            raise InvalidTransaction("Manual assets do not support SELL transactions")
        elif tx_type == TransactionType.MANUAL_VALUE_UPDATE.value:
            value = _tx_manual_value(tx)
            if value < 0:
                raise InvalidTransaction("Manual value cannot be negative")
            latest_value = value
            latest_value_at = _tx_timestamp(tx)
        else:
            raise InvalidTransaction(f"Unsupported transaction type: {tx_type}")

    current_value = latest_value if latest_value is not None else 0.0
    unrealized = current_value - invested_total if invested_total > 0 else None
    return ManualPosition(
        current_value=current_value,
        invested_total=invested_total,
        unrealized_pnl=unrealized,
        as_of=latest_value_at,
    )


def compute_allocation_percentages(values_by_key: dict[str, float]) -> dict[str, float]:
    """Return allocation percentages for each key, summing to approximately 100."""
    total = sum(values_by_key.values())
    if total <= 0:
        return {key: 0.0 for key in values_by_key}
    return {key: (value / total) * 100.0 for key, value in values_by_key.items()}


def allocation_by_group(positions: list[PositionRow]) -> AllocationChartData:
    """Build chart-ready allocation slices by group, excluding basket rows."""
    values_by_group: dict[str, float] = defaultdict(float)
    for row in positions:
        if row.row_kind == "basket":
            continue
        if row.current_value > 0:
            values_by_group[row.group_name] += row.current_value

    if not values_by_group:
        return AllocationChartData(labels=[], values=[], percentages=[])

    labels = sorted(values_by_group.keys(), key=str.lower)
    percentages = compute_allocation_percentages(values_by_group)
    return AllocationChartData(
        labels=labels,
        values=[values_by_group[label] for label in labels],
        percentages=[percentages[label] for label in labels],
    )


def allocation_by_asset(
    positions: list[PositionRow],
    basket_member_asset_ids: set[int] | None = None,
) -> AllocationChartData:
    """Build chart-ready allocation slices by asset, replacing basket members with basket rows."""
    basket_members = basket_member_asset_ids or set()
    values_by_label: dict[str, float] = {}

    for row in positions:
        if row.current_value <= 0:
            continue

        if row.row_kind == "basket":
            label = row.name
        else:
            if row.asset_id in basket_members:
                continue
            label = row.symbol

        values_by_label[label] = values_by_label.get(label, 0.0) + row.current_value

    if not values_by_label:
        return AllocationChartData(labels=[], values=[], percentages=[])

    labels = sorted(values_by_label.keys(), key=str.lower)
    percentages = compute_allocation_percentages(values_by_label)
    return AllocationChartData(
        labels=labels,
        values=[values_by_label[label] for label in labels],
        percentages=[percentages[label] for label in labels],
    )


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
    """Compute one dashboard row for a single asset."""
    group_name = asset.group.name if asset.group else "Ungrouped"

    if asset.asset_type == AssetType.MARKET:
        market = compute_market_position(transactions)
        quote = pricing_service.get_quote(db, asset.symbol)
        current_price = quote.price if quote else None
        current_value = market.quantity * current_price if current_price is not None else 0.0
        unrealized = (
            (current_price - market.avg_cost) * market.quantity
            if current_price is not None
            else None
        )
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
            quote_stale=quote.stale if quote else False,
            row_kind="asset",
            detail_path=f"/assets/{asset.id}",
        )

    manual = compute_manual_position(transactions)
    return PositionRow(
        asset_id=asset.id,
        symbol=asset.symbol,
        name=asset.name,
        group_name=group_name,
        asset_type=asset.asset_type,
        quantity=None,
        avg_cost=manual.invested_total if manual.invested_total > 0 else None,
        current_price=None,
        current_value=manual.current_value,
        unrealized_pnl=manual.unrealized_pnl,
        allocation_pct=0.0,
        as_of=manual.as_of,
        quote_stale=False,
        row_kind="asset",
        detail_path=f"/assets/{asset.id}",
    )


def _compute_basket_position(
    basket: Basket,
    asset_positions: dict[int, PositionRow],
) -> PositionRow:
    """Build a synthetic dashboard row for one basket."""
    market_links = [
        link
        for link in basket.assets
        if link.asset is not None
        and link.asset.asset_type == AssetType.MARKET
        and not link.asset.is_archived
        and link.asset_id in asset_positions
    ]

    current_value = 0.0
    as_of: datetime | None = None
    quote_stale = False

    if market_links:
        try:
            normalized_weights = _normalized_basket_weights(market_links)
        except ValueError:
            normalized_weights = []

        for idx, link in enumerate(market_links):
            if idx >= len(normalized_weights):
                break
            member = asset_positions[link.asset_id]
            current_value += normalized_weights[idx] * member.current_value
            if member.as_of and (as_of is None or member.as_of > as_of):
                as_of = member.as_of
            quote_stale = quote_stale or member.quote_stale

    return PositionRow(
        asset_id=-basket.id,
        symbol=f"BASKET:{basket.id}",
        name=basket.name,
        group_name="baskets",
        asset_type=AssetType.MANUAL,
        quantity=None,
        avg_cost=None,
        current_price=None,
        current_value=current_value,
        unrealized_pnl=None,
        allocation_pct=0.0,
        as_of=as_of,
        quote_stale=quote_stale,
        row_kind="basket",
        detail_path=f"/baskets/{basket.id}",
    )


def build_dashboard_snapshot(
    db: Session,
    pricing_service: PricingService,
    portfolio_id: int,
) -> DashboardSnapshot:
    """Build full dashboard data for one portfolio."""
    assets = list(
        db.scalars(
            select(Asset)
            .where(Asset.portfolio_id == portfolio_id, Asset.is_archived.is_(False))
            .options(selectinload(Asset.group))
        )
    )
    assets.sort(
        key=lambda asset: (
            (asset.group.name.lower() if asset.group else ""),
            asset.symbol.lower(),
            asset.id,
        )
    )

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

    seen_asset_ids: set[int] = set()
    positions: list[PositionRow] = []
    for asset in assets:
        if asset.id in seen_asset_ids:
            continue
        seen_asset_ids.add(asset.id)
        position = compute_asset_position(
            db=db,
            pricing_service=pricing_service,
            asset=asset,
            transactions=tx_by_asset.get(asset.id, []),
        )
        positions.append(position)

    asset_positions = {row.asset_id: row for row in positions}
    basket_member_asset_ids: set[int] = set()
    baskets = list(
        db.scalars(
            select(Basket)
            .where(Basket.portfolio_id == portfolio_id)
            .options(selectinload(Basket.assets).selectinload(BasketAsset.asset))
        )
    )
    for basket in baskets:
        for link in basket.assets:
            if (
                link.asset is not None
                and link.asset.asset_type == AssetType.MARKET
                and not link.asset.is_archived
                and link.asset_id in asset_positions
            ):
                basket_member_asset_ids.add(link.asset_id)
        positions.append(_compute_basket_position(basket, asset_positions))

    for asset_id in basket_member_asset_ids:
        row = asset_positions.get(asset_id)
        if row:
            row.in_basket_member = True

    positions.sort(
        key=lambda row: (
            row.group_name.lower(),
            row.symbol.lower(),
            row.asset_id,
        )
    )

    totals_by_group: dict[str, dict[str, float]] = defaultdict(lambda: {"value": 0.0, "pnl": 0.0})
    total_value = 0.0
    total_unrealized = 0.0

    for position in positions:
        totals_by_group[position.group_name]["value"] += position.current_value
        pnl = position.unrealized_pnl if position.unrealized_pnl is not None else 0.0
        totals_by_group[position.group_name]["pnl"] += pnl
        total_value += position.current_value
        total_unrealized += pnl

    allocation_map = compute_allocation_percentages(
        {str(row.asset_id): row.current_value for row in positions}
    )
    for row in positions:
        row.allocation_pct = allocation_map.get(str(row.asset_id), 0.0)

    return DashboardSnapshot(
        positions=positions,
        group_totals=dict(totals_by_group),
        total_value=total_value,
        total_unrealized_pnl=total_unrealized,
        basket_member_asset_ids=basket_member_asset_ids,
    )


def validate_asset_transactions(
    asset: Asset,
    transactions: Iterable[Transaction | object],
) -> None:
    """Validate full transaction history for an asset."""
    tx_list = list(transactions)
    if asset.asset_type == AssetType.MARKET:
        for tx in tx_list:
            if _tx_type(tx) == TransactionType.MANUAL_VALUE_UPDATE.value:
                raise InvalidTransaction("Market assets do not support MANUAL_VALUE_UPDATE transactions")
        compute_market_position(tx_list)
    else:
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


def _normalized_basket_weights(market_links: list[BasketAsset]) -> list[float]:
    if not market_links:
        return []

    if all(link.weight is None for link in market_links):
        raw_weights = [1.0 for _ in market_links]
    else:
        raw_weights = [float(link.weight) if link.weight is not None else 1.0 for link in market_links]

    if any(weight <= 0 for weight in raw_weights):
        raise ValueError("All basket weights must be positive")

    total_weight = sum(raw_weights)
    if total_weight <= 0:
        raise ValueError("Basket weights must sum to a positive number")

    return [weight / total_weight for weight in raw_weights]


def compute_basket_series(
    pricing_service: PricingService,
    basket_links: list[BasketAsset],
    start: date,
    end: date,
) -> BasketSeriesResult:
    """Compute normalized basket series with strict date intersection across members."""
    market_links = [
        link
        for link in basket_links
        if link.asset.asset_type == AssetType.MARKET and not link.asset.is_archived
    ]
    if not market_links:
        return BasketSeriesResult(
            points=[],
            missing_symbols=[],
            error_message="Basket has no active market assets.",
        )

    try:
        normalized_weights = _normalized_basket_weights(market_links)
    except ValueError as exc:
        return BasketSeriesResult(points=[], missing_symbols=[], error_message=str(exc))

    series_by_asset_id: dict[int, dict[date, float]] = {}
    missing_symbols: list[str] = []

    for link in market_links:
        points = pricing_service.get_historical_daily(link.asset.symbol, start, end)
        series = {
            point.date: float(point.close)
            for point in points
            if start <= point.date <= end and float(point.close) > 0
        }
        if not series:
            missing_symbols.append(link.asset.symbol)
            continue
        series_by_asset_id[link.asset_id] = series

    if missing_symbols:
        symbols = ", ".join(sorted(set(missing_symbols)))
        return BasketSeriesResult(
            points=[],
            missing_symbols=sorted(set(missing_symbols)),
            error_message=f"Missing historical data for: {symbols}",
        )

    if not series_by_asset_id:
        return BasketSeriesResult(points=[], missing_symbols=[], error_message="No historical data available.")

    common_dates: set[date] | None = None
    for series in series_by_asset_id.values():
        date_set = set(series.keys())
        common_dates = date_set if common_dates is None else (common_dates & date_set)

    if not common_dates:
        return BasketSeriesResult(
            points=[],
            missing_symbols=[],
            error_message="No overlapping historical dates across basket members.",
        )

    ordered_dates = sorted(common_dates)
    start_date = ordered_dates[0]

    baseline_by_asset_id: dict[int, float] = {}
    for link in market_links:
        baseline = series_by_asset_id[link.asset_id][start_date]
        if baseline <= 0:
            return BasketSeriesResult(
                points=[],
                missing_symbols=[link.asset.symbol],
                error_message=f"Invalid baseline price for {link.asset.symbol}.",
            )
        baseline_by_asset_id[link.asset_id] = baseline

    points: list[BasketSeriesPoint] = []
    for day in ordered_dates:
        composite = 0.0
        for idx, link in enumerate(market_links):
            close = series_by_asset_id[link.asset_id][day]
            baseline = baseline_by_asset_id[link.asset_id]
            member_index = 100.0 * (close / baseline)
            composite += normalized_weights[idx] * member_index
        points.append(BasketSeriesPoint(date=day, value=composite))

    return BasketSeriesResult(points=points, missing_symbols=[], error_message=None)


def build_basket_normalized_series(
    pricing_service: PricingService,
    basket_links: list[BasketAsset],
    days: int = 120,
) -> list[BasketSeriesPoint]:
    """Backward-compatible wrapper returning only basket points for the default range."""
    end = date.today()
    start = end - timedelta(days=days)
    return compute_basket_series(pricing_service, basket_links, start, end).points


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
