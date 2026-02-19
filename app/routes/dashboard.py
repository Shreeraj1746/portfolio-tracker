from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Asset, AssetType, Group, Transaction, TransactionType
from app.services.portfolio import (
    allocation_by_asset,
    allocation_by_group,
    build_dashboard_snapshot,
    compute_overlay_pnl_series,
    compute_portfolio_series,
    ensure_default_portfolio,
    get_portfolio_assets,
    get_portfolio_groups,
)
from app.web import flash, form_with_csrf, get_user_from_session, render_template

router = APIRouter()


def _parse_float(
    value: str,
    field_name: str,
    allow_zero: bool = True,
    allow_negative: bool = False,
) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a number") from exc

    if allow_negative:
        if not allow_zero and number == 0:
            raise ValueError(f"{field_name} must be non-zero")
    else:
        if allow_zero and number < 0:
            raise ValueError(f"{field_name} cannot be negative")
        if not allow_zero and number <= 0:
            raise ValueError(f"{field_name} must be greater than zero")
    return number


@router.get("/")
def dashboard_page(request: Request, db: Session = Depends(get_db)):
    """Render dashboard with position table and allocation chart."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    portfolio = ensure_default_portfolio(db)
    snapshot = build_dashboard_snapshot(
        db=db,
        pricing_service=request.app.state.pricing_service,
        portfolio_id=portfolio.id,
    )

    today = date.today()
    default_end = today
    default_start = default_end - timedelta(days=179)
    chart_start = default_start
    chart_end = default_end
    range_error: str | None = None

    raw_start = request.query_params.get("chart_start")
    raw_end = request.query_params.get("chart_end")
    if raw_start:
        try:
            chart_start = date.fromisoformat(raw_start)
        except ValueError:
            range_error = "Invalid chart start date."
    if raw_end:
        try:
            chart_end = date.fromisoformat(raw_end)
        except ValueError:
            range_error = "Invalid chart end date."
    if range_error is None and chart_start > chart_end:
        range_error = "Chart start date cannot be after chart end date."

    group_allocation = allocation_by_group(snapshot.positions)
    asset_allocation = allocation_by_asset(
        snapshot.positions,
        snapshot.basket_member_asset_ids,
    )

    if range_error is None:
        portfolio_series = compute_portfolio_series(
            db=db,
            pricing_service=request.app.state.pricing_service,
            portfolio_id=portfolio.id,
            start=chart_start,
            end=chart_end,
        )
        pnl_overlay = compute_overlay_pnl_series(
            db=db,
            pricing_service=request.app.state.pricing_service,
            portfolio_id=portfolio.id,
            start=chart_start,
            end=chart_end,
        )
    else:
        portfolio_series = None
        pnl_overlay = None

    db.commit()

    canonical_groups_for_table: dict[str, list] = {}
    for row in snapshot.canonical_positions:
        canonical_groups_for_table.setdefault(row.group_name, []).append(row)

    market_symbols = sorted(
        {
            row.symbol
            for row in snapshot.positions
            if row.asset_type == AssetType.MARKET and row.row_kind == "asset"
        }
    )

    return render_template(
        request,
        "dashboard.html",
        {
            "page_title": "Dashboard",
            "user": user,
            "portfolio": portfolio,
            "groups": get_portfolio_groups(db, portfolio.id),
            "assets": get_portfolio_assets(db, portfolio.id),
            "canonical_grouped_positions": canonical_groups_for_table,
            "derived_positions": snapshot.derived_positions,
            "canonical_group_totals": snapshot.canonical_group_totals,
            "canonical_total_value": snapshot.canonical_total_value,
            "canonical_total_unrealized_pnl": snapshot.canonical_total_unrealized_pnl,
            "derived_total_value": snapshot.derived_total_value,
            "derived_total_unrealized_pnl": snapshot.derived_total_unrealized_pnl,
            "group_allocation_labels": group_allocation.labels,
            "group_allocation_values": group_allocation.values,
            "group_allocation_percentages": group_allocation.percentages,
            "has_group_allocation": bool(group_allocation.values),
            "asset_allocation_labels": asset_allocation.labels,
            "asset_allocation_values": asset_allocation.values,
            "asset_allocation_percentages": asset_allocation.percentages,
            "has_asset_allocation": bool(asset_allocation.values),
            "chart_start": chart_start.isoformat(),
            "chart_end": chart_end.isoformat(),
            "chart_range_error": range_error,
            "portfolio_series_labels": (
                [point.date.isoformat() for point in portfolio_series.points]
                if portfolio_series
                else []
            ),
            "portfolio_series_values": (
                [point.total_value_usd for point in portfolio_series.points]
                if portfolio_series
                else []
            ),
            "has_portfolio_series": bool(portfolio_series and portfolio_series.points),
            "portfolio_series_error": (
                range_error if range_error else (portfolio_series.error_message if portfolio_series else None)
            ),
            "portfolio_series_missing_symbols": (
                portfolio_series.missing_symbols if portfolio_series else []
            ),
            "pnl_series_labels": (
                [day.isoformat() for day in pnl_overlay.dates] if pnl_overlay else []
            ),
            "pnl_series_by_asset": (
                pnl_overlay.series_by_label if pnl_overlay else {}
            ),
            "pnl_selector_labels": (
                sorted(pnl_overlay.series_by_label.keys(), key=str.lower) if pnl_overlay else []
            ),
            "has_pnl_series": bool(pnl_overlay and pnl_overlay.series_by_label),
            "pnl_series_error": (
                range_error if range_error else (pnl_overlay.error_message if pnl_overlay else None)
            ),
            "pnl_series_missing_symbols": (
                pnl_overlay.missing_symbols if pnl_overlay else []
            ),
            "market_symbols": market_symbols,
        },
    )


@router.post("/groups")
async def create_group(request: Request, db: Session = Depends(get_db)):
    """Create a new group for the default portfolio."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    form = await form_with_csrf(request)
    name = str(form.get("name", "")).strip()
    if not name:
        flash(request, "Group name is required", "error")
        return RedirectResponse(url="/", status_code=303)

    portfolio = ensure_default_portfolio(db)
    exists = db.scalar(
        select(Group).where(Group.portfolio_id == portfolio.id, Group.name == name)
    )
    if exists:
        flash(request, "Group already exists", "error")
        return RedirectResponse(url="/", status_code=303)

    group = Group(portfolio_id=portfolio.id, name=name)
    db.add(group)
    db.commit()
    flash(request, f"Group '{name}' created", "success")
    return RedirectResponse(url="/", status_code=303)


@router.post("/assets")
async def create_asset(request: Request, db: Session = Depends(get_db)):
    """Create one asset and optional initial transactions from dashboard form."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    form = await form_with_csrf(request)

    symbol = str(form.get("symbol", "")).strip().upper()
    name = str(form.get("name", "")).strip()
    asset_type_raw = str(form.get("asset_type", "market")).strip().lower()
    group_id_raw = str(form.get("group_id", "")).strip()

    if not symbol or not name or not group_id_raw:
        flash(request, "Symbol, name, and group are required", "error")
        return RedirectResponse(url="/", status_code=303)

    if asset_type_raw not in {AssetType.MARKET.value, AssetType.MANUAL.value}:
        flash(request, "Invalid asset type", "error")
        return RedirectResponse(url="/", status_code=303)

    try:
        group_id = int(group_id_raw)
    except ValueError:
        flash(request, "Invalid group", "error")
        return RedirectResponse(url="/", status_code=303)

    portfolio = ensure_default_portfolio(db)
    group = db.scalar(
        select(Group).where(Group.id == group_id, Group.portfolio_id == portfolio.id)
    )
    if not group:
        flash(request, "Group not found", "error")
        return RedirectResponse(url="/", status_code=303)

    existing_symbol = db.scalar(
        select(Asset).where(
            Asset.portfolio_id == portfolio.id,
            Asset.symbol == symbol,
            Asset.is_archived.is_(False),
        )
    )
    if existing_symbol:
        flash(request, f"Active asset with symbol '{symbol}' already exists", "error")
        return RedirectResponse(url="/", status_code=303)

    asset_type = AssetType(asset_type_raw)
    created_at = datetime.now(timezone.utc)

    asset = Asset(
        portfolio_id=portfolio.id,
        symbol=symbol,
        name=name,
        asset_type=asset_type,
        group_id=group.id,
    )
    db.add(asset)
    db.flush()

    try:
        if asset_type == AssetType.MARKET:
            qty_raw = str(form.get("initial_quantity", "")).strip()
            buy_price_raw = str(form.get("initial_buy_price", "")).strip()
            fees_raw = str(form.get("initial_fees", "0")).strip() or "0"

            initial_quantity = _parse_float(qty_raw, "Initial shares", allow_zero=False) if qty_raw else 0.0
            initial_fees = _parse_float(fees_raw, "Initial fees", allow_zero=True)

            if initial_quantity > 0:
                if not buy_price_raw:
                    raise ValueError("Initial buy price is required when shares are provided")
                initial_buy_price = _parse_float(
                    buy_price_raw,
                    "Initial buy price",
                    allow_zero=True,
                    allow_negative=True,
                )
                db.add(
                    Transaction(
                        portfolio_id=portfolio.id,
                        asset_id=asset.id,
                        type=TransactionType.BUY,
                        timestamp=created_at,
                        quantity=initial_quantity,
                        price=initial_buy_price,
                        fees=initial_fees,
                        note="Initial position",
                    )
                )
        else:
            value_raw = str(form.get("initial_value", "")).strip()
            invested_raw = str(form.get("initial_invested", "")).strip()

            if not value_raw:
                raise ValueError("Initial manual value is required for manual assets")

            initial_value = _parse_float(value_raw, "Initial manual value", allow_zero=True)
            db.add(
                Transaction(
                    portfolio_id=portfolio.id,
                    asset_id=asset.id,
                    type=TransactionType.MANUAL_VALUE_UPDATE,
                    timestamp=created_at,
                    manual_value=initial_value,
                    note="Initial manual value",
                )
            )

            if invested_raw:
                initial_invested = _parse_float(invested_raw, "Initial invested", allow_zero=False)
                db.add(
                    Transaction(
                        portfolio_id=portfolio.id,
                        asset_id=asset.id,
                        type=TransactionType.BUY,
                        timestamp=created_at,
                        quantity=initial_invested,
                        price=1.0,
                        fees=0.0,
                        note="Initial invested amount at unit cost 1",
                    )
                )
    except ValueError as exc:
        db.rollback()
        flash(request, str(exc), "error")
        return RedirectResponse(url="/", status_code=303)

    db.commit()
    flash(request, f"Asset '{symbol}' created", "success")
    return RedirectResponse(url=f"/assets/{asset.id}", status_code=303)


@router.get("/api/quotes")
def api_quotes(
    request: Request,
    symbols: str = Query(default="", description="Comma-separated symbols"),
    db: Session = Depends(get_db),
):
    """Return latest quotes for active market assets used by dashboard polling."""
    user = get_user_from_session(request, db)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    portfolio = ensure_default_portfolio(db)
    allowed_symbols = {
        symbol
        for symbol in db.scalars(
            select(Asset.symbol).where(
                Asset.portfolio_id == portfolio.id,
                Asset.asset_type == AssetType.MARKET,
                Asset.is_archived.is_(False),
            )
        )
    }

    requested_symbols = {
        symbol.strip().upper() for symbol in symbols.split(",") if symbol.strip()
    }

    data: dict[str, dict[str, object]] = {}
    for symbol in sorted(requested_symbols & allowed_symbols):
        quote = request.app.state.pricing_service.get_quote(db, symbol)
        if quote is None:
            data[symbol] = {"price": None, "as_of": None, "stale": True}
        else:
            data[symbol] = {
                "price": quote.price,
                "as_of": quote.fetched_at.isoformat(),
                "stale": quote.stale,
                "warning": quote.warning,
            }

    db.commit()
    return {"quotes": data}
