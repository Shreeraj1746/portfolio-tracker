from __future__ import annotations

import json
from datetime import date, timedelta

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import Asset, AssetType, Basket, BasketAsset
from app.services.portfolio import (
    build_basket_member_composition,
    compute_basket_series,
    compute_market_position,
    ensure_default_portfolio,
)
from app.web import flash, form_with_csrf, get_user_from_session, render_template

router = APIRouter(prefix="/baskets", tags=["baskets"])


def _market_assets(db: Session, portfolio_id: int) -> list[Asset]:
    return list(
        db.scalars(
            select(Asset)
            .where(
                Asset.portfolio_id == portfolio_id,
                Asset.asset_type == AssetType.MARKET,
                Asset.is_archived.is_(False),
            )
            .options(selectinload(Asset.transactions))
            .order_by(Asset.symbol.asc())
        )
    )


def _load_basket(db: Session, basket_id: int, portfolio_id: int) -> Basket | None:
    return db.scalar(
        select(Basket)
        .where(Basket.id == basket_id, Basket.portfolio_id == portfolio_id)
        .options(
            selectinload(Basket.assets)
            .selectinload(BasketAsset.asset)
            .selectinload(Asset.transactions)
        )
    )


def _parse_selected_ids(raw_values: list[str]) -> list[int]:
    output: list[int] = []
    for value in raw_values:
        value = value.strip()
        if not value:
            continue
        output.append(int(value))
    return output


def _asset_quantity(asset: Asset) -> float:
    try:
        return compute_market_position(list(asset.transactions)).quantity
    except Exception:
        return 0.0


@router.get("")
def baskets_index(request: Request, db: Session = Depends(get_db)):
    """Render basket list and creation form."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    portfolio = ensure_default_portfolio(db)
    baskets = list(
        db.scalars(
            select(Basket)
            .where(Basket.portfolio_id == portfolio.id)
            .options(selectinload(Basket.assets))
            .order_by(Basket.name.asc())
        )
    )
    assets = _market_assets(db, portfolio.id)
    asset_quantities = {asset.id: _asset_quantity(asset) for asset in assets}

    return render_template(
        request,
        "baskets.html",
        {
            "page_title": "Baskets",
            "user": user,
            "portfolio": portfolio,
            "baskets": baskets,
            "assets": assets,
            "asset_quantities": asset_quantities,
        },
    )


@router.post("/create")
async def create_basket(request: Request, db: Session = Depends(get_db)):
    """Create basket with selected assets; composition uses live share counts."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    form = await form_with_csrf(request)
    portfolio = ensure_default_portfolio(db)

    name = str(form.get("name", "")).strip()
    if not name:
        flash(request, "Basket name is required", "error")
        return RedirectResponse(url="/baskets", status_code=303)

    try:
        selected_ids = _parse_selected_ids(list(form.getlist("asset_ids")))
    except ValueError:
        flash(request, "Invalid asset selection", "error")
        return RedirectResponse(url="/baskets", status_code=303)

    basket = Basket(portfolio_id=portfolio.id, name=name)
    db.add(basket)
    db.flush()

    for asset_id in selected_ids:
        asset = db.scalar(
            select(Asset).where(
                Asset.id == asset_id,
                Asset.portfolio_id == portfolio.id,
                Asset.asset_type == AssetType.MARKET,
                Asset.is_archived.is_(False),
            )
        )
        if not asset:
            continue
        db.add(BasketAsset(basket_id=basket.id, asset_id=asset.id, weight=None))

    db.commit()
    flash(request, "Basket created", "success")
    return RedirectResponse(url=f"/baskets/{basket.id}", status_code=303)


@router.get("/{basket_id}")
def basket_detail(
    basket_id: int,
    request: Request,
    start: str | None = Query(default=None),
    end: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    """Render basket detail and normalized performance chart."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    portfolio = ensure_default_portfolio(db)
    basket = _load_basket(db, basket_id, portfolio.id)
    if not basket:
        flash(request, "Basket not found", "error")
        return RedirectResponse(url="/baskets", status_code=303)

    today = date.today()
    default_start = today - timedelta(days=120)

    try:
        start_date = date.fromisoformat(start) if start else default_start
        end_date = date.fromisoformat(end) if end else today
    except ValueError:
        flash(request, "Invalid date range", "error")
        return RedirectResponse(url=f"/baskets/{basket.id}", status_code=303)

    if start_date > end_date:
        flash(request, "Start date cannot be after end date", "error")
        return RedirectResponse(url=f"/baskets/{basket.id}", status_code=303)

    series_result = compute_basket_series(
        pricing_service=request.app.state.pricing_service,
        basket_links=basket.assets,
        start=start_date,
        end=end_date,
    )

    labels = [point.date.isoformat() for point in series_result.points]
    values = [point.value for point in series_result.points]
    member_composition = build_basket_member_composition(
        db=db,
        pricing_service=request.app.state.pricing_service,
        basket_links=basket.assets,
    )
    db.commit()

    return render_template(
        request,
        "basket_detail.html",
        {
            "page_title": f"Basket: {basket.name}",
            "user": user,
            "basket": basket,
            "series_labels_json": json.dumps(labels),
            "series_values_json": json.dumps(values),
            "member_composition": member_composition,
            "has_series": bool(labels),
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "missing_symbols": series_result.missing_symbols,
            "series_error": series_result.error_message,
        },
    )


@router.get("/{basket_id}/edit")
def edit_basket_page(basket_id: int, request: Request, db: Session = Depends(get_db)):
    """Render basket edit page for rename/member edits."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    portfolio = ensure_default_portfolio(db)
    basket = _load_basket(db, basket_id, portfolio.id)
    if not basket:
        flash(request, "Basket not found", "error")
        return RedirectResponse(url="/baskets", status_code=303)

    assets = _market_assets(db, portfolio.id)
    selected_ids = {link.asset_id for link in basket.assets}
    asset_quantities = {asset.id: _asset_quantity(asset) for asset in assets}

    return render_template(
        request,
        "basket_edit.html",
        {
            "page_title": f"Edit Basket: {basket.name}",
            "user": user,
            "basket": basket,
            "assets": assets,
            "selected_ids": selected_ids,
            "asset_quantities": asset_quantities,
        },
    )


@router.post("/{basket_id}/edit")
async def edit_basket_submit(basket_id: int, request: Request, db: Session = Depends(get_db)):
    """Update basket name and members."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    form = await form_with_csrf(request)
    portfolio = ensure_default_portfolio(db)
    basket = _load_basket(db, basket_id, portfolio.id)
    if not basket:
        flash(request, "Basket not found", "error")
        return RedirectResponse(url="/baskets", status_code=303)

    name = str(form.get("name", "")).strip()
    if not name:
        flash(request, "Basket name is required", "error")
        return RedirectResponse(url=f"/baskets/{basket.id}/edit", status_code=303)

    try:
        selected_ids = _parse_selected_ids(list(form.getlist("asset_ids")))
    except ValueError:
        flash(request, "Invalid asset selection", "error")
        return RedirectResponse(url=f"/baskets/{basket.id}/edit", status_code=303)

    basket.name = name

    # Replace all links for simpler and deterministic updates.
    for link in list(basket.assets):
        db.delete(link)
    db.flush()

    for asset_id in selected_ids:
        asset = db.scalar(
            select(Asset).where(
                Asset.id == asset_id,
                Asset.portfolio_id == portfolio.id,
                Asset.asset_type == AssetType.MARKET,
                Asset.is_archived.is_(False),
            )
        )
        if not asset:
            continue
        db.add(BasketAsset(basket_id=basket.id, asset_id=asset.id, weight=None))

    db.commit()
    flash(request, "Basket updated", "success")
    return RedirectResponse(url=f"/baskets/{basket.id}", status_code=303)


@router.post("/{basket_id}/delete")
async def delete_basket(basket_id: int, request: Request, db: Session = Depends(get_db)):
    """Delete basket and cascade remove basket members."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    _ = await form_with_csrf(request)

    portfolio = ensure_default_portfolio(db)
    basket = _load_basket(db, basket_id, portfolio.id)
    if not basket:
        flash(request, "Basket not found", "error")
        return RedirectResponse(url="/baskets", status_code=303)

    db.delete(basket)
    db.commit()
    flash(request, "Basket deleted", "success")
    return RedirectResponse(url="/baskets", status_code=303)
