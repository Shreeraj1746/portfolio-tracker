from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import math
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
    valuation_scope: str = "canonical"
    counts_in_totals: bool = True
    counts_in_allocation: bool = True
    display_badge: str | None = None
    basket_member_ids: list[int] | None = None
    in_basket_member: bool = False


@dataclass
class DashboardSnapshot:
    positions: list[PositionRow]
    canonical_positions: list[PositionRow]
    derived_positions: list[PositionRow]
    canonical_group_totals: dict[str, dict[str, float]]
    canonical_total_value: float
    canonical_total_unrealized_pnl: float
    derived_total_value: float
    derived_total_unrealized_pnl: float
    basket_member_asset_ids: set[int]
    # Backward-compatible aliases used by older route/template code paths.
    group_totals: dict[str, dict[str, float]]
    total_value: float
    total_unrealized_pnl: float


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


@dataclass
class PortfolioSeriesPoint:
    date: date
    total_value_usd: float


@dataclass
class PortfolioSeriesResult:
    points: list[PortfolioSeriesPoint]
    missing_symbols: list[str]
    error_message: str | None


@dataclass
class OverlayPnlSeriesResult:
    dates: list[date]
    series_by_label: dict[str, list[float]]
    missing_symbols: list[str]
    error_message: str | None


@dataclass
class BasketMemberComposition:
    asset_id: int
    symbol: str
    name: str
    quantity: float
    current_price: float | None
    current_value: float
    allocation_pct: float


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


def _tx_invested_override(value: Transaction | object) -> float | None:
    raw = getattr(value, "invested_override", None)
    if raw is None:
        return None
    return float(raw)


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
            if not math.isfinite(price):
                raise InvalidTransaction("BUY price must be a finite number")
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
    """Compute manual-asset value with optional invested override snapshots."""
    held_quantity = 0.0
    avg_cost = 0.0
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
            cost_total = invested_total + (quantity * price) + fees
            held_quantity += quantity
            invested_total = cost_total
            avg_cost = invested_total / held_quantity if held_quantity > 0 else 0.0
        elif tx_type == TransactionType.SELL.value:
            quantity_to_sell = _tx_quantity(tx)
            price = _tx_price(tx)
            fees = _tx_fees(tx)
            if quantity_to_sell <= 0:
                raise InvalidTransaction("Manual SELL quantity must be positive")
            if price < 0:
                raise InvalidTransaction("Manual SELL price cannot be negative")
            if fees < 0:
                raise InvalidTransaction("Fees cannot be negative")
            if quantity_to_sell - held_quantity > 1e-9:
                raise InvalidTransaction(
                    "Cannot sell more than currently held manual quantity"
                )

            invested_total -= avg_cost * quantity_to_sell
            held_quantity -= quantity_to_sell
            if held_quantity <= 1e-9:
                held_quantity = 0.0
                avg_cost = 0.0
                invested_total = 0.0
            else:
                avg_cost = invested_total / held_quantity
        elif tx_type == TransactionType.MANUAL_VALUE_UPDATE.value:
            value = _tx_manual_value(tx)
            if value < 0:
                raise InvalidTransaction("Manual value cannot be negative")
            latest_value = value
            latest_value_at = _tx_timestamp(tx)

            invested_override = _tx_invested_override(tx)
            if invested_override is not None:
                if invested_override < 0:
                    raise InvalidTransaction(
                        "Manual invested override cannot be negative"
                    )
                invested_total = invested_override
                # Invested override is treated as a direct basis reset.
                held_quantity = invested_override
                avg_cost = 1.0 if held_quantity > 0 else 0.0
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
    """Build chart-ready allocation slices by group using canonical rows only."""
    values_by_group: dict[str, float] = defaultdict(float)
    for row in positions:
        if not row.counts_in_allocation:
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
            valuation_scope="canonical",
            counts_in_totals=True,
            counts_in_allocation=True,
            basket_member_ids=[],
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
        valuation_scope="canonical",
        counts_in_totals=True,
        counts_in_allocation=True,
        basket_member_ids=[],
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
    derived_pnl = 0.0
    has_member_pnl = False
    member_ids: list[int] = []

    if market_links:
        for link in market_links:
            member = asset_positions[link.asset_id]
            current_value += member.current_value
            member_ids.append(link.asset_id)
            if member.unrealized_pnl is not None:
                derived_pnl += member.unrealized_pnl
                has_member_pnl = True
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
        unrealized_pnl=derived_pnl if has_member_pnl else None,
        allocation_pct=0.0,
        as_of=as_of,
        quote_stale=quote_stale,
        row_kind="basket",
        detail_path=f"/baskets/{basket.id}",
        valuation_scope="derived",
        counts_in_totals=False,
        counts_in_allocation=False,
        display_badge="Derived (excluded from totals)",
        basket_member_ids=sorted(member_ids),
    )


def build_dashboard_snapshot(
    db: Session,
    pricing_service: PricingService,
    portfolio_id: int,
) -> DashboardSnapshot:
    """Build dashboard data with canonical accounting and derived basket overlays."""
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
            row.valuation_scope != "canonical",
            row.group_name.lower(),
            row.symbol.lower(),
            row.asset_id,
        )
    )

    canonical_positions = [row for row in positions if row.counts_in_totals]
    derived_positions = [row for row in positions if not row.counts_in_totals]

    canonical_group_totals: dict[str, dict[str, float]] = defaultdict(
        lambda: {"value": 0.0, "pnl": 0.0}
    )
    canonical_total_value = 0.0
    canonical_total_unrealized = 0.0

    for row in canonical_positions:
        canonical_group_totals[row.group_name]["value"] += row.current_value
        pnl = row.unrealized_pnl if row.unrealized_pnl is not None else 0.0
        canonical_group_totals[row.group_name]["pnl"] += pnl
        canonical_total_value += row.current_value
        canonical_total_unrealized += pnl

    derived_total_value = 0.0
    derived_total_unrealized = 0.0
    for row in derived_positions:
        derived_total_value += row.current_value
        if row.unrealized_pnl is not None:
            derived_total_unrealized += row.unrealized_pnl

    allocation_map = compute_allocation_percentages(
        {
            str(row.asset_id): row.current_value
            for row in canonical_positions
            if row.counts_in_allocation
        }
    )
    for row in positions:
        if row.counts_in_allocation:
            row.allocation_pct = allocation_map.get(str(row.asset_id), 0.0)
        else:
            row.allocation_pct = 0.0

    return DashboardSnapshot(
        positions=positions,
        canonical_positions=canonical_positions,
        derived_positions=derived_positions,
        canonical_group_totals=dict(canonical_group_totals),
        canonical_total_value=canonical_total_value,
        canonical_total_unrealized_pnl=canonical_total_unrealized,
        derived_total_value=derived_total_value,
        derived_total_unrealized_pnl=derived_total_unrealized,
        basket_member_asset_ids=basket_member_asset_ids,
        group_totals=dict(canonical_group_totals),
        total_value=canonical_total_value,
        total_unrealized_pnl=canonical_total_unrealized,
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


def _basket_member_quantity(link: BasketAsset | object) -> float:
    asset = getattr(link, "asset", None)
    if asset is None:
        return 0.0
    if getattr(asset, "asset_type", None) != AssetType.MARKET:
        return 0.0
    if getattr(asset, "is_archived", False):
        return 0.0

    txs = list(getattr(asset, "transactions", []) or [])
    if not txs:
        return 0.0
    try:
        position = compute_market_position(txs)
    except InvalidTransaction:
        return 0.0
    return max(position.quantity, 0.0)


def build_basket_member_composition(
    db: Session,
    pricing_service: PricingService,
    basket_links: list[BasketAsset],
) -> list[BasketMemberComposition]:
    """Compute current basket composition using live held value (shares x latest price)."""
    market_links = [
        link
        for link in basket_links
        if link.asset is not None
        and link.asset.asset_type == AssetType.MARKET
        and not link.asset.is_archived
    ]
    if not market_links:
        return []

    rows: list[BasketMemberComposition] = []
    total_value = 0.0

    for link in sorted(market_links, key=lambda entry: entry.asset.symbol.lower()):
        qty = _basket_member_quantity(link)
        quote = pricing_service.get_quote(db, link.asset.symbol)
        current_price = quote.price if quote else None
        current_value = qty * current_price if current_price is not None else 0.0
        total_value += current_value
        rows.append(
            BasketMemberComposition(
                asset_id=link.asset_id,
                symbol=link.asset.symbol,
                name=link.asset.name,
                quantity=qty,
                current_price=current_price,
                current_value=current_value,
                allocation_pct=0.0,
            )
        )

    if total_value > 0:
        for row in rows:
            row.allocation_pct = (row.current_value / total_value) * 100.0

    return rows


def _normalized_basket_weights(
    market_links: list[BasketAsset],
    quantities_by_asset_id: dict[int, float],
) -> list[float]:
    if not market_links:
        return []

    raw_weights = [max(float(quantities_by_asset_id.get(link.asset_id, 0.0)), 0.0) for link in market_links]

    total_weight = sum(raw_weights)
    if total_weight <= 0:
        raise ValueError("Basket members must have positive total shares")

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

    quantities_by_asset_id = {
        link.asset_id: _basket_member_quantity(link) for link in market_links
    }
    positive_links = [
        link for link in market_links if quantities_by_asset_id.get(link.asset_id, 0.0) > 0
    ]
    if not positive_links:
        return BasketSeriesResult(
            points=[],
            missing_symbols=[],
            error_message="Basket members have zero shares. Add holdings to included assets.",
        )

    normalized_weights = _normalized_basket_weights(
        positive_links,
        quantities_by_asset_id,
    )

    series_by_asset_id: dict[int, dict[date, float]] = {}
    missing_symbols: list[str] = []

    for link in positive_links:
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
    for link in positive_links:
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
        for idx, link in enumerate(positive_links):
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


def _day_range(start: date, end: date) -> list[date]:
    if start > end:
        return []
    day_count = (end - start).days + 1
    return [start + timedelta(days=idx) for idx in range(day_count)]


def _market_quantity_by_day(
    transactions: list[Transaction],
    days: list[date],
) -> dict[date, float]:
    """Replay BUY/SELL history and return end-of-day quantity for each day."""
    ordered = sort_transactions(transactions)
    out: dict[date, float] = {}

    qty = 0.0
    tx_index = 0
    total = len(ordered)

    for day in days:
        day_end = datetime.combine(day, datetime.max.time(), tzinfo=timezone.utc)
        while tx_index < total and _tx_timestamp(ordered[tx_index]) <= day_end:
            tx = ordered[tx_index]
            tx_type = _tx_type(tx)
            if tx_type == TransactionType.BUY.value:
                qty += max(_tx_quantity(tx), 0.0)
            elif tx_type == TransactionType.SELL.value:
                qty -= max(_tx_quantity(tx), 0.0)
                if qty < 0:
                    qty = 0.0
            tx_index += 1
        out[day] = qty
    return out


def _market_state_by_day(
    transactions: list[Transaction],
    days: list[date],
) -> dict[date, tuple[float, float]]:
    """Replay BUY/SELL history and return end-of-day (quantity, avg_cost)."""
    ordered = sort_transactions(transactions)
    out: dict[date, tuple[float, float]] = {}

    qty = 0.0
    avg_cost = 0.0
    tx_index = 0
    total = len(ordered)

    for day in days:
        day_end = datetime.combine(day, datetime.max.time(), tzinfo=timezone.utc)
        while tx_index < total and _tx_timestamp(ordered[tx_index]) <= day_end:
            tx = ordered[tx_index]
            tx_type = _tx_type(tx)
            quantity = max(_tx_quantity(tx), 0.0)
            price = _tx_price(tx)
            fees = max(_tx_fees(tx), 0.0)

            if tx_type == TransactionType.BUY.value and quantity > 0:
                new_cost_total = (qty * avg_cost) + (quantity * price) + fees
                qty += quantity
                avg_cost = new_cost_total / qty if qty > 0 else 0.0
            elif tx_type == TransactionType.SELL.value and quantity > 0:
                qty -= quantity
                if qty <= 1e-9:
                    qty = 0.0
                    avg_cost = 0.0
            tx_index += 1
        out[day] = (qty, avg_cost)
    return out


def _manual_value_by_day(
    transactions: list[Transaction],
    days: list[date],
) -> dict[date, float]:
    """Build daily step-function values from MANUAL_VALUE_UPDATE transactions."""
    updates = [
        tx for tx in sort_transactions(transactions) if _tx_type(tx) == TransactionType.MANUAL_VALUE_UPDATE.value
    ]
    out: dict[date, float] = {}
    latest_value = 0.0
    idx = 0
    total = len(updates)

    for day in days:
        day_end = datetime.combine(day, datetime.max.time(), tzinfo=timezone.utc)
        while idx < total and _tx_timestamp(updates[idx]) <= day_end:
            latest_value = max(_tx_manual_value(updates[idx]), 0.0)
            idx += 1
        out[day] = latest_value
    return out


def _manual_invested_by_day(
    transactions: list[Transaction],
    days: list[date],
) -> dict[date, float]:
    """Return running invested amount by day for manual assets."""
    ordered = sort_transactions(transactions)
    out: dict[date, float] = {}
    invested = 0.0
    tx_index = 0
    total = len(ordered)

    for day in days:
        day_end = datetime.combine(day, datetime.max.time(), tzinfo=timezone.utc)
        while tx_index < total and _tx_timestamp(ordered[tx_index]) <= day_end:
            tx = ordered[tx_index]
            if _tx_type(tx) == TransactionType.BUY.value:
                quantity = max(_tx_quantity(tx), 0.0)
                price = _tx_price(tx)
                fees = max(_tx_fees(tx), 0.0)
                invested += (quantity * price) + fees
            tx_index += 1
        out[day] = invested
    return out


def compute_portfolio_series(
    db: Session,
    pricing_service: PricingService,
    portfolio_id: int,
    start: date,
    end: date,
) -> PortfolioSeriesResult:
    """Compute canonical portfolio daily USD value series for the dashboard."""
    days = _day_range(start, end)
    if not days:
        return PortfolioSeriesResult(
            points=[],
            missing_symbols=[],
            error_message="Invalid date range for portfolio chart.",
        )

    assets = list(
        db.scalars(
            select(Asset).where(
                Asset.portfolio_id == portfolio_id,
                Asset.is_archived.is_(False),
            )
        )
    )
    if not assets:
        return PortfolioSeriesResult(
            points=[],
            missing_symbols=[],
            error_message="No active assets to chart yet.",
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

    daily_totals = {day: 0.0 for day in days}
    missing_symbols: list[str] = []

    for asset in assets:
        transactions = tx_by_asset.get(asset.id, [])
        if asset.asset_type == AssetType.MARKET:
            qty_by_day = _market_quantity_by_day(transactions, days)
            history = pricing_service.get_historical_daily(asset.symbol, start, end)
            close_by_day = {
                point.date: float(point.close)
                for point in history
                if start <= point.date <= end and float(point.close) > 0
            }

            if not close_by_day:
                missing_symbols.append(asset.symbol)
                continue

            rolling_close: float | None = None
            for day in days:
                close = close_by_day.get(day)
                if close is not None and close > 0:
                    rolling_close = close
                price = rolling_close if rolling_close is not None else 0.0
                daily_totals[day] += qty_by_day[day] * price
        else:
            value_by_day = _manual_value_by_day(transactions, days)
            for day in days:
                daily_totals[day] += value_by_day[day]

    points = [
        PortfolioSeriesPoint(date=day, total_value_usd=daily_totals[day])
        for day in days
    ]
    if all(point.total_value_usd <= 0 for point in points):
        return PortfolioSeriesResult(
            points=[],
            missing_symbols=sorted(set(missing_symbols)),
            error_message="No holdings data available for the selected range.",
        )

    return PortfolioSeriesResult(
        points=points,
        missing_symbols=sorted(set(missing_symbols)),
        error_message=None,
    )


def compute_overlay_pnl_series(
    db: Session,
    pricing_service: PricingService,
    portfolio_id: int,
    start: date,
    end: date,
) -> OverlayPnlSeriesResult:
    """Compute chart-ready unrealized PnL series keyed by asset/basket overlay labels."""
    days = _day_range(start, end)
    if not days:
        return OverlayPnlSeriesResult(
            dates=[],
            series_by_label={},
            missing_symbols=[],
            error_message="Invalid date range for PnL chart.",
        )

    assets = list(
        db.scalars(
            select(Asset)
            .where(Asset.portfolio_id == portfolio_id, Asset.is_archived.is_(False))
            .order_by(Asset.symbol.asc())
        )
    )
    if not assets:
        return OverlayPnlSeriesResult(
            dates=[],
            series_by_label={},
            missing_symbols=[],
            error_message="No active assets to chart yet.",
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

    pnl_by_asset_id: dict[int, list[float]] = {}
    missing_symbols: list[str] = []

    for asset in assets:
        txs = tx_by_asset.get(asset.id, [])
        if asset.asset_type == AssetType.MARKET:
            state_by_day = _market_state_by_day(txs, days)
            history = pricing_service.get_historical_daily(asset.symbol, start, end)
            close_by_day = {
                point.date: float(point.close)
                for point in history
                if start <= point.date <= end and float(point.close) > 0
            }
            if not close_by_day:
                missing_symbols.append(asset.symbol)
                continue

            rolling_close: float | None = None
            series: list[float] = []
            for day in days:
                close = close_by_day.get(day)
                if close is not None and close > 0:
                    rolling_close = close
                current_price = rolling_close if rolling_close is not None else 0.0
                qty, avg_cost = state_by_day[day]
                series.append((current_price - avg_cost) * qty if qty > 0 else 0.0)
            pnl_by_asset_id[asset.id] = series
        else:
            manual_values = _manual_value_by_day(txs, days)
            invested_by_day = _manual_invested_by_day(txs, days)
            series = []
            for day in days:
                invested = invested_by_day[day]
                if invested > 0:
                    series.append(manual_values[day] - invested)
                else:
                    series.append(0.0)
            pnl_by_asset_id[asset.id] = series

    baskets = list(
        db.scalars(
            select(Basket)
            .where(Basket.portfolio_id == portfolio_id)
            .options(selectinload(Basket.assets).selectinload(BasketAsset.asset))
            .order_by(Basket.name.asc())
        )
    )

    basket_member_asset_ids: set[int] = set()
    for basket in baskets:
        for link in basket.assets:
            if (
                link.asset is not None
                and link.asset.asset_type == AssetType.MARKET
                and not link.asset.is_archived
            ):
                basket_member_asset_ids.add(link.asset_id)

    overlay_series: dict[str, list[float]] = {}
    for basket in baskets:
        member_ids = [
            link.asset_id
            for link in basket.assets
            if (
                link.asset is not None
                and link.asset.asset_type == AssetType.MARKET
                and not link.asset.is_archived
                and link.asset_id in pnl_by_asset_id
            )
        ]
        if not member_ids:
            continue
        merged = [0.0 for _ in days]
        for asset_id in member_ids:
            for idx, value in enumerate(pnl_by_asset_id[asset_id]):
                merged[idx] += value
        overlay_series[basket.name] = merged

    for asset in assets:
        if asset.id in basket_member_asset_ids:
            continue
        series = pnl_by_asset_id.get(asset.id)
        if series is None:
            continue
        overlay_series[asset.symbol] = series

    if not overlay_series:
        return OverlayPnlSeriesResult(
            dates=[],
            series_by_label={},
            missing_symbols=sorted(set(missing_symbols)),
            error_message="No PnL data available for the selected range.",
        )

    return OverlayPnlSeriesResult(
        dates=days,
        series_by_label=overlay_series,
        missing_symbols=sorted(set(missing_symbols)),
        error_message=None,
    )


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
