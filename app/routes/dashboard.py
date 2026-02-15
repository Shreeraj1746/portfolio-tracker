from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Asset, AssetType, Group
from app.services.portfolio import (
    build_dashboard_snapshot,
    ensure_default_portfolio,
    get_portfolio_assets,
    get_portfolio_groups,
)
from app.web import flash, form_with_csrf, get_user_from_session, render_template

router = APIRouter()

@router.get("/")
def dashboard_page(request: Request, db: Session = Depends(get_db)):
    """Render dashboard with position table and allocation chart."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    portfolio = ensure_default_portfolio(db)
    snapshot = build_dashboard_snapshot(db, request.app.state.pricing_service, portfolio.id)
    db.commit()

    groups_for_table: dict[str, list] = {}
    for row in snapshot.positions:
        groups_for_table.setdefault(row.group_name, []).append(row)

    allocation_labels = list(snapshot.group_totals.keys())
    allocation_values = [values["value"] for values in snapshot.group_totals.values()]
    market_symbols = [row.symbol for row in snapshot.positions if row.asset_type.value == "market"]

    return render_template(
        request,
        "dashboard.html",
        {
            "page_title": "Dashboard",
            "user": user,
            "portfolio": portfolio,
            "groups": get_portfolio_groups(db, portfolio.id),
            "assets": get_portfolio_assets(db, portfolio.id),
            "grouped_positions": groups_for_table,
            "group_totals": snapshot.group_totals,
            "total_value": snapshot.total_value,
            "total_unrealized_pnl": snapshot.total_unrealized_pnl,
            "allocation_labels": allocation_labels,
            "allocation_values": allocation_values,
            "market_symbols": sorted(set(market_symbols)),
            "default_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M"),
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
    exists = db.scalar(select(Group).where(Group.portfolio_id == portfolio.id, Group.name == name))
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
    """Create one asset under the default portfolio."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    form = await form_with_csrf(request)
    symbol = str(form.get("symbol", "")).strip().upper()
    name = str(form.get("name", "")).strip()
    asset_type = str(form.get("asset_type", "market")).strip().lower()
    group_id_raw = str(form.get("group_id", "")).strip()

    if not symbol or not name or not group_id_raw:
        flash(request, "Symbol, name, and group are required", "error")
        return RedirectResponse(url="/", status_code=303)

    if asset_type not in {AssetType.MARKET.value, AssetType.MANUAL.value}:
        flash(request, "Invalid asset type", "error")
        return RedirectResponse(url="/", status_code=303)

    try:
        group_id = int(group_id_raw)
    except ValueError:
        flash(request, "Invalid group", "error")
        return RedirectResponse(url="/", status_code=303)

    portfolio = ensure_default_portfolio(db)
    group = db.scalar(select(Group).where(Group.id == group_id, Group.portfolio_id == portfolio.id))
    if not group:
        flash(request, "Group not found", "error")
        return RedirectResponse(url="/", status_code=303)

    asset = Asset(
        portfolio_id=portfolio.id,
        symbol=symbol,
        name=name,
        asset_type=AssetType(asset_type),
        group_id=group.id,
    )
    db.add(asset)
    db.commit()
    flash(request, f"Asset '{symbol}' created", "success")
    return RedirectResponse(url="/", status_code=303)


@router.get("/api/quotes")
def api_quotes(
    request: Request,
    symbols: str = Query(default="", description="Comma-separated symbols"),
    db: Session = Depends(get_db),
):
    """Return latest quotes for UI polling every 60 seconds."""
    user = get_user_from_session(request, db)
    if not user:
        return JSONResponse(status_code=401, content={"error": "Unauthorized"})

    pricing_service = request.app.state.pricing_service
    unique_symbols = [symbol.strip().upper() for symbol in symbols.split(",") if symbol.strip()]

    data: dict[str, dict[str, object]] = {}
    for symbol in sorted(set(unique_symbols)):
        quote = pricing_service.get_quote(db, symbol)
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
