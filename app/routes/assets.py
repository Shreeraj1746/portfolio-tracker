from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import Asset, AssetType, Transaction, TransactionType
from app.services.portfolio import (
    InvalidTransaction,
    build_asset_history,
    compute_asset_position,
    ensure_default_portfolio,
    validate_asset_transactions,
)
from app.web import flash, form_with_csrf, get_user_from_session, render_template

router = APIRouter(prefix="/assets", tags=["assets"])


def _parse_datetime_local(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_float(value: str, field_name: str, allow_zero: bool = True) -> float:
    try:
        number = float(value)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be a number") from exc

    if not allow_zero and number <= 0:
        raise ValueError(f"{field_name} must be greater than zero")
    if allow_zero and number < 0:
        raise ValueError(f"{field_name} cannot be negative")
    return number


def _load_asset(db: Session, asset_id: int) -> Asset | None:
    return db.scalar(select(Asset).where(Asset.id == asset_id).options(selectinload(Asset.group)))


@router.get("/{asset_id}")
def asset_detail(asset_id: int, request: Request, db: Session = Depends(get_db)):
    """Render asset detail with transaction history and chart."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    asset = _load_asset(db, asset_id)
    if not asset:
        flash(request, "Asset not found", "error")
        return RedirectResponse(url="/", status_code=303)

    transactions = list(
        db.scalars(
            select(Transaction)
            .where(Transaction.asset_id == asset.id)
            .order_by(Transaction.timestamp.asc(), Transaction.id.asc())
        )
    )

    row = compute_asset_position(db, request.app.state.pricing_service, asset, transactions)
    history = build_asset_history(request.app.state.pricing_service, asset)
    chart_labels = [point.date.isoformat() for point in history]
    chart_values = [point.value for point in history]

    db.commit()

    return render_template(
        request,
        "asset_detail.html",
        {
            "page_title": f"Asset: {asset.symbol}",
            "user": user,
            "asset": asset,
            "summary": row,
            "transactions": sorted(transactions, key=lambda tx: (tx.timestamp, tx.id), reverse=True),
            "chart_labels_json": json.dumps(chart_labels),
            "chart_values_json": json.dumps(chart_values),
            "can_chart": bool(chart_labels),
            "default_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M"),
            "tx_types": [t.value for t in TransactionType],
        },
    )


@router.post("/{asset_id}/transactions")
async def add_transaction(asset_id: int, request: Request, db: Session = Depends(get_db)):
    """Create a transaction and validate full history deterministically."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    asset = _load_asset(db, asset_id)
    if not asset:
        flash(request, "Asset not found", "error")
        return RedirectResponse(url="/", status_code=303)

    form = await form_with_csrf(request)
    tx_type = str(form.get("type", "")).strip().upper()

    try:
        tx_enum = TransactionType(tx_type)
    except ValueError:
        flash(request, "Invalid transaction type", "error")
        return RedirectResponse(url=f"/assets/{asset.id}", status_code=303)

    try:
        timestamp = _parse_datetime_local(str(form.get("timestamp", "")))
        note = str(form.get("note", "")).strip() or None
        fees = _parse_float(str(form.get("fees", "0") or "0"), "Fees", allow_zero=True)

        quantity = None
        price = None
        manual_value = None

        if tx_enum in {TransactionType.BUY, TransactionType.SELL}:
            quantity = _parse_float(str(form.get("quantity", "")), "Quantity", allow_zero=False)
            price = _parse_float(str(form.get("price", "")), "Price", allow_zero=False)
        elif tx_enum == TransactionType.MANUAL_VALUE_UPDATE:
            manual_value = _parse_float(str(form.get("manual_value", "")), "Manual value", allow_zero=True)

        candidate = SimpleNamespace(
            id=10**9,
            type=tx_enum,
            timestamp=timestamp,
            quantity=quantity,
            price=price,
            fees=fees,
            manual_value=manual_value,
        )

        existing = list(
            db.scalars(
                select(Transaction)
                .where(Transaction.asset_id == asset.id)
                .order_by(Transaction.timestamp.asc(), Transaction.id.asc())
            )
        )
        validate_asset_transactions(asset, [*existing, candidate])
    except (InvalidTransaction, ValueError) as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(url=f"/assets/{asset.id}", status_code=303)

    portfolio = ensure_default_portfolio(db)
    tx = Transaction(
        portfolio_id=portfolio.id,
        asset_id=asset.id,
        type=tx_enum,
        timestamp=timestamp,
        quantity=quantity,
        price=price,
        fees=fees,
        manual_value=manual_value,
        note=note,
    )
    db.add(tx)
    db.commit()
    flash(request, "Transaction added", "success")
    return RedirectResponse(url=f"/assets/{asset.id}", status_code=303)


@router.get("/{asset_id}/transactions/{tx_id}/edit")
def edit_transaction_page(asset_id: int, tx_id: int, request: Request, db: Session = Depends(get_db)):
    """Render edit page for one transaction."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    asset = _load_asset(db, asset_id)
    tx = db.scalar(select(Transaction).where(Transaction.id == tx_id, Transaction.asset_id == asset_id))
    if not asset or not tx:
        flash(request, "Transaction not found", "error")
        return RedirectResponse(url=f"/assets/{asset_id}", status_code=303)

    return render_template(
        request,
        "transaction_edit.html",
        {
            "page_title": "Edit Transaction",
            "asset": asset,
            "transaction": tx,
            "tx_types": [t.value for t in TransactionType],
        },
    )


@router.post("/{asset_id}/transactions/{tx_id}/edit")
async def edit_transaction(asset_id: int, tx_id: int, request: Request, db: Session = Depends(get_db)):
    """Update one transaction and revalidate sequence."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    asset = _load_asset(db, asset_id)
    tx = db.scalar(select(Transaction).where(Transaction.id == tx_id, Transaction.asset_id == asset_id))
    if not asset or not tx:
        flash(request, "Transaction not found", "error")
        return RedirectResponse(url=f"/assets/{asset_id}", status_code=303)

    form = await form_with_csrf(request)

    try:
        tx_enum = TransactionType(str(form.get("type", "")).strip().upper())
        timestamp = _parse_datetime_local(str(form.get("timestamp", "")))
        fees = _parse_float(str(form.get("fees", "0") or "0"), "Fees", allow_zero=True)
        note = str(form.get("note", "")).strip() or None

        quantity = None
        price = None
        manual_value = None

        if tx_enum in {TransactionType.BUY, TransactionType.SELL}:
            quantity = _parse_float(str(form.get("quantity", "")), "Quantity", allow_zero=False)
            price = _parse_float(str(form.get("price", "")), "Price", allow_zero=False)
        elif tx_enum == TransactionType.MANUAL_VALUE_UPDATE:
            manual_value = _parse_float(str(form.get("manual_value", "")), "Manual value", allow_zero=True)

        candidate = SimpleNamespace(
            id=tx.id,
            type=tx_enum,
            timestamp=timestamp,
            quantity=quantity,
            price=price,
            fees=fees,
            manual_value=manual_value,
        )

        existing = list(
            db.scalars(
                select(Transaction)
                .where(Transaction.asset_id == asset.id)
                .order_by(Transaction.timestamp.asc(), Transaction.id.asc())
            )
        )

        merged: list[object] = []
        for row in existing:
            merged.append(candidate if row.id == tx.id else row)

        validate_asset_transactions(asset, merged)
    except (InvalidTransaction, ValueError) as exc:
        flash(request, str(exc), "error")
        return RedirectResponse(url=f"/assets/{asset.id}/transactions/{tx.id}/edit", status_code=303)

    tx.type = tx_enum
    tx.timestamp = timestamp
    tx.quantity = quantity
    tx.price = price
    tx.manual_value = manual_value
    tx.fees = fees
    tx.note = note
    db.commit()
    flash(request, "Transaction updated", "success")
    return RedirectResponse(url=f"/assets/{asset.id}", status_code=303)


@router.post("/{asset_id}/transactions/{tx_id}/delete")
async def delete_transaction(asset_id: int, tx_id: int, request: Request, db: Session = Depends(get_db)):
    """Delete one transaction when remaining history is still valid."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    asset = _load_asset(db, asset_id)
    tx = db.scalar(select(Transaction).where(Transaction.id == tx_id, Transaction.asset_id == asset_id))
    if not asset or not tx:
        flash(request, "Transaction not found", "error")
        return RedirectResponse(url=f"/assets/{asset_id}", status_code=303)

    existing = list(
        db.scalars(
            select(Transaction)
            .where(Transaction.asset_id == asset.id)
            .order_by(Transaction.timestamp.asc(), Transaction.id.asc())
        )
    )
    remaining = [row for row in existing if row.id != tx.id]

    try:
        validate_asset_transactions(asset, remaining)
    except InvalidTransaction as exc:
        flash(request, f"Cannot delete transaction: {exc}", "error")
        return RedirectResponse(url=f"/assets/{asset.id}", status_code=303)

    db.delete(tx)
    db.commit()
    flash(request, "Transaction deleted", "success")
    return RedirectResponse(url=f"/assets/{asset.id}", status_code=303)
