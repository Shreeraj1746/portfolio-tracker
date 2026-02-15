from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import Asset, Basket, BasketAsset
from app.services.portfolio import (
    build_basket_normalized_series,
    ensure_default_portfolio,
    get_portfolio_assets,
    get_portfolio_baskets,
)
from app.web import flash, form_with_csrf, get_user_from_session, render_template

router = APIRouter(prefix="/baskets", tags=["baskets"])


@router.get("")
def baskets_index(request: Request, db: Session = Depends(get_db)):
    """Render basket list and creation form."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    portfolio = ensure_default_portfolio(db)
    baskets = get_portfolio_baskets(db, portfolio.id)
    assets = [asset for asset in get_portfolio_assets(db, portfolio.id) if asset.asset_type.value == "market"]

    return render_template(
        request,
        "baskets.html",
        {
            "page_title": "Baskets",
            "user": user,
            "portfolio": portfolio,
            "baskets": baskets,
            "assets": assets,
        },
    )


@router.post("/create")
async def create_basket(request: Request, db: Session = Depends(get_db)):
    """Create basket with optional selected assets and weights."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    form = await form_with_csrf(request)
    portfolio = ensure_default_portfolio(db)

    name = str(form.get("name", "")).strip()
    selected_ids = [int(value) for value in form.getlist("asset_ids")]

    if not name:
        flash(request, "Basket name is required", "error")
        return RedirectResponse(url="/baskets", status_code=303)

    basket = Basket(portfolio_id=portfolio.id, name=name)
    db.add(basket)
    db.flush()

    for asset_id in selected_ids:
        asset = db.scalar(select(Asset).where(Asset.id == asset_id, Asset.portfolio_id == portfolio.id))
        if not asset:
            continue

        weight_raw = str(form.get(f"weight_{asset_id}", "")).strip()
        weight = None
        if weight_raw:
            try:
                weight = float(weight_raw)
                if weight < 0:
                    raise ValueError
            except ValueError:
                db.rollback()
                flash(request, "Weights must be positive numbers", "error")
                return RedirectResponse(url="/baskets", status_code=303)

        db.add(BasketAsset(basket_id=basket.id, asset_id=asset.id, weight=weight))

    db.commit()
    flash(request, "Basket created", "success")
    return RedirectResponse(url=f"/baskets/{basket.id}", status_code=303)


@router.get("/{basket_id}")
def basket_detail(basket_id: int, request: Request, db: Session = Depends(get_db)):
    """Render basket detail and normalized performance chart."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    basket = db.scalar(
        select(Basket)
        .where(Basket.id == basket_id)
        .options(selectinload(Basket.assets).selectinload(BasketAsset.asset))
    )
    if not basket:
        flash(request, "Basket not found", "error")
        return RedirectResponse(url="/baskets", status_code=303)

    series = build_basket_normalized_series(request.app.state.pricing_service, basket.assets)
    labels = [point.date.isoformat() for point in series]
    values = [point.value for point in series]

    return render_template(
        request,
        "basket_detail.html",
        {
            "page_title": f"Basket: {basket.name}",
            "user": user,
            "basket": basket,
            "series_labels_json": json.dumps(labels),
            "series_values_json": json.dumps(values),
            "has_series": bool(labels),
        },
    )
