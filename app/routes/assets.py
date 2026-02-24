from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace

from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, selectinload

from app.db import get_db
from app.models import Asset, AssetType, Group, Transaction, TransactionType
from app.services.portfolio import (
    InvalidTransaction,
    build_asset_history,
    compute_asset_position,
    ensure_default_portfolio,
    get_portfolio_groups,
    validate_asset_transactions,
)
from app.web import flash, form_with_csrf, get_user_from_session, render_template

router = APIRouter(prefix="/assets", tags=["assets"])


def _commit_quote_cache_updates(db: Session) -> None:
    """Best-effort commit for quote cache writes on read-only endpoints."""
    try:
        db.commit()
    except OperationalError:
        db.rollback()


def _parse_datetime_local(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


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
        if not allow_zero and number <= 0:
            raise ValueError(f"{field_name} must be greater than zero")
        if allow_zero and number < 0:
            raise ValueError(f"{field_name} cannot be negative")
    return number


def _load_asset(db: Session, asset_id: int) -> Asset | None:
    return db.scalar(
        select(Asset)
        .where(Asset.id == asset_id)
        .options(selectinload(Asset.group))
    )


def _tx_count(db: Session, asset_id: int) -> int:
    return len(list(db.scalars(select(Transaction.id).where(Transaction.asset_id == asset_id))))


def _allowed_tx_types(asset_type: AssetType) -> list[TransactionType]:
    if asset_type == AssetType.MARKET:
        return [TransactionType.BUY, TransactionType.SELL]
    return [TransactionType.BUY, TransactionType.SELL, TransactionType.MANUAL_VALUE_UPDATE]


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

    summary = compute_asset_position(
        db=db,
        pricing_service=request.app.state.pricing_service,
        asset=asset,
        transactions=transactions,
    )
    history = build_asset_history(request.app.state.pricing_service, asset)

    chart_labels = [point.date.isoformat() for point in history]
    chart_values = [point.value for point in history]

    tx_types = _allowed_tx_types(asset.asset_type)
    tx_count = len(transactions)

    _commit_quote_cache_updates(db)

    return render_template(
        request,
        "asset_detail.html",
        {
            "page_title": f"Asset: {asset.symbol}",
            "user": user,
            "asset": asset,
            "summary": summary,
            "transactions": sorted(
                transactions, key=lambda tx: (tx.timestamp, tx.id), reverse=True
            ),
            "chart_labels_json": json.dumps(chart_labels),
            "chart_values_json": json.dumps(chart_values),
            "can_chart": bool(chart_labels),
            "default_timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M"),
            "tx_types": [t.value for t in tx_types],
            "tx_count": tx_count,
            "can_delete_asset": tx_count == 0,
        },
    )


@router.get("/{asset_id}/edit")
def asset_edit_page(asset_id: int, request: Request, db: Session = Depends(get_db)):
    """Render asset edit page."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    asset = _load_asset(db, asset_id)
    if not asset:
        flash(request, "Asset not found", "error")
        return RedirectResponse(url="/", status_code=303)

    tx_count = _tx_count(db, asset.id)
    portfolio = ensure_default_portfolio(db)

    return render_template(
        request,
        "asset_edit.html",
        {
            "page_title": f"Edit Asset: {asset.symbol}",
            "user": user,
            "asset": asset,
            "groups": get_portfolio_groups(db, portfolio.id),
            "asset_types": [AssetType.MARKET.value, AssetType.MANUAL.value],
            "has_transactions": tx_count > 0,
            "tx_count": tx_count,
        },
    )


@router.post("/{asset_id}/edit")
async def asset_edit_submit(asset_id: int, request: Request, db: Session = Depends(get_db)):
    """Update one asset with transaction-aware edit constraints."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    asset = _load_asset(db, asset_id)
    if not asset:
        flash(request, "Asset not found", "error")
        return RedirectResponse(url="/", status_code=303)

    form = await form_with_csrf(request)
    portfolio = ensure_default_portfolio(db)

    name = str(form.get("name", "")).strip()
    symbol = str(form.get("symbol", asset.symbol)).strip().upper() or asset.symbol
    asset_type_raw = str(form.get("asset_type", asset.asset_type.value)).strip().lower()
    group_id_raw = str(form.get("group_id", "")).strip()
    is_archived = str(form.get("is_archived", "")).strip().lower() in {"on", "1", "true", "yes"}

    if not name:
        flash(request, "Name is required", "error")
        return RedirectResponse(url=f"/assets/{asset.id}/edit", status_code=303)

    try:
        group_id = int(group_id_raw)
    except ValueError:
        flash(request, "Group is required", "error")
        return RedirectResponse(url=f"/assets/{asset.id}/edit", status_code=303)

    group = db.scalar(
        select(Group).where(Group.id == group_id, Group.portfolio_id == portfolio.id)
    )
    if not group:
        flash(request, "Invalid group", "error")
        return RedirectResponse(url=f"/assets/{asset.id}/edit", status_code=303)

    if asset_type_raw not in {AssetType.MARKET.value, AssetType.MANUAL.value}:
        flash(request, "Invalid asset type", "error")
        return RedirectResponse(url=f"/assets/{asset.id}/edit", status_code=303)

    tx_count = _tx_count(db, asset.id)

    if tx_count > 0:
        if symbol != asset.symbol or asset_type_raw != asset.asset_type.value:
            flash(
                request,
                "Symbol and type cannot be changed after transactions exist",
                "error",
            )
            return RedirectResponse(url=f"/assets/{asset.id}/edit", status_code=303)
    else:
        duplicate = db.scalar(
            select(Asset).where(
                Asset.id != asset.id,
                Asset.portfolio_id == portfolio.id,
                Asset.symbol == symbol,
                Asset.is_archived.is_(False),
            )
        )
        if duplicate:
            flash(request, f"Active asset with symbol '{symbol}' already exists", "error")
            return RedirectResponse(url=f"/assets/{asset.id}/edit", status_code=303)
        asset.symbol = symbol
        asset.asset_type = AssetType(asset_type_raw)

    asset.name = name
    asset.group_id = group.id
    asset.is_archived = is_archived

    db.commit()
    flash(request, "Asset updated", "success")
    return RedirectResponse(url=f"/assets/{asset.id}", status_code=303)


@router.post("/{asset_id}/delete")
async def delete_asset(asset_id: int, request: Request, db: Session = Depends(get_db)):
    """Delete an asset only when transaction history is empty."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    _ = await form_with_csrf(request)

    asset = _load_asset(db, asset_id)
    if not asset:
        flash(request, "Asset not found", "error")
        return RedirectResponse(url="/", status_code=303)

    if _tx_count(db, asset.id) > 0:
        flash(request, "Cannot delete asset with transactions. Archive it instead.", "error")
        return RedirectResponse(url=f"/assets/{asset.id}/edit", status_code=303)

    db.delete(asset)
    db.commit()
    flash(request, "Asset deleted", "success")
    return RedirectResponse(url="/", status_code=303)


@router.post("/{asset_id}/archive")
async def archive_asset(asset_id: int, request: Request, db: Session = Depends(get_db)):
    """Archive one asset (soft delete)."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    _ = await form_with_csrf(request)

    asset = _load_asset(db, asset_id)
    if not asset:
        flash(request, "Asset not found", "error")
        return RedirectResponse(url="/", status_code=303)

    asset.is_archived = True
    db.commit()
    flash(request, "Asset archived", "success")
    return RedirectResponse(url="/", status_code=303)


@router.post("/{asset_id}/transactions")
async def add_transaction(
    asset_id: int, request: Request, db: Session = Depends(get_db)
):
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

    if tx_enum not in _allowed_tx_types(asset.asset_type):
        flash(request, "Transaction type not allowed for this asset type", "error")
        return RedirectResponse(url=f"/assets/{asset.id}", status_code=303)

    try:
        timestamp = _parse_datetime_local(str(form.get("timestamp", "")))
        note = str(form.get("note", "")).strip() or None
        fees = _parse_float(str(form.get("fees", "0") or "0"), "Fees", allow_zero=True)

        quantity = None
        price = None
        manual_value = None
        invested_override = None

        if tx_enum in {TransactionType.BUY, TransactionType.SELL}:
            quantity = _parse_float(
                str(form.get("quantity", "")), "Quantity", allow_zero=False
            )
            if tx_enum == TransactionType.BUY:
                price = _parse_float(
                    str(form.get("price", "")),
                    "Price",
                    allow_zero=True,
                    allow_negative=True,
                )
            else:
                price = _parse_float(
                    str(form.get("price", "")),
                    "Price",
                    allow_zero=False,
                )
        elif tx_enum == TransactionType.MANUAL_VALUE_UPDATE:
            manual_value = _parse_float(
                str(form.get("manual_value", "")), "Manual value", allow_zero=True
            )
            raw_invested_override = str(
                form.get("manual_invested_override", "")
            ).strip()
            if raw_invested_override:
                invested_override = _parse_float(
                    raw_invested_override,
                    "Manual invested override",
                    allow_zero=True,
                )

        candidate = SimpleNamespace(
            id=10**9,
            type=tx_enum,
            timestamp=timestamp,
            quantity=quantity,
            price=price,
            fees=fees,
            manual_value=manual_value,
            invested_override=invested_override,
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
        invested_override=invested_override,
        note=note,
    )
    db.add(tx)
    db.commit()
    flash(request, "Transaction added", "success")
    return RedirectResponse(url=f"/assets/{asset.id}", status_code=303)


@router.get("/{asset_id}/transactions/{tx_id}/edit")
def edit_transaction_page(
    asset_id: int, tx_id: int, request: Request, db: Session = Depends(get_db)
):
    """Render edit page for one transaction."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    asset = _load_asset(db, asset_id)
    tx = db.scalar(
        select(Transaction).where(
            Transaction.id == tx_id, Transaction.asset_id == asset_id
        )
    )
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
            "tx_types": [t.value for t in _allowed_tx_types(asset.asset_type)],
        },
    )


@router.post("/{asset_id}/transactions/{tx_id}/edit")
async def edit_transaction(
    asset_id: int, tx_id: int, request: Request, db: Session = Depends(get_db)
):
    """Update one transaction and revalidate sequence."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    asset = _load_asset(db, asset_id)
    tx = db.scalar(
        select(Transaction).where(
            Transaction.id == tx_id, Transaction.asset_id == asset_id
        )
    )
    if not asset or not tx:
        flash(request, "Transaction not found", "error")
        return RedirectResponse(url=f"/assets/{asset_id}", status_code=303)

    form = await form_with_csrf(request)

    try:
        tx_enum = TransactionType(str(form.get("type", "")).strip().upper())
        if tx_enum not in _allowed_tx_types(asset.asset_type):
            raise ValueError("Transaction type not allowed for this asset type")

        timestamp = _parse_datetime_local(str(form.get("timestamp", "")))
        fees = _parse_float(str(form.get("fees", "0") or "0"), "Fees", allow_zero=True)
        note = str(form.get("note", "")).strip() or None

        quantity = None
        price = None
        manual_value = None
        invested_override = None

        if tx_enum in {TransactionType.BUY, TransactionType.SELL}:
            quantity = _parse_float(
                str(form.get("quantity", "")), "Quantity", allow_zero=False
            )
            if tx_enum == TransactionType.BUY:
                price = _parse_float(
                    str(form.get("price", "")),
                    "Price",
                    allow_zero=True,
                    allow_negative=True,
                )
            else:
                price = _parse_float(
                    str(form.get("price", "")),
                    "Price",
                    allow_zero=False,
                )
        elif tx_enum == TransactionType.MANUAL_VALUE_UPDATE:
            manual_value = _parse_float(
                str(form.get("manual_value", "")), "Manual value", allow_zero=True
            )
            raw_invested_override = str(
                form.get("manual_invested_override", "")
            ).strip()
            if raw_invested_override:
                invested_override = _parse_float(
                    raw_invested_override,
                    "Manual invested override",
                    allow_zero=True,
                )

        candidate = SimpleNamespace(
            id=tx.id,
            type=tx_enum,
            timestamp=timestamp,
            quantity=quantity,
            price=price,
            fees=fees,
            manual_value=manual_value,
            invested_override=invested_override,
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
        return RedirectResponse(
            url=f"/assets/{asset.id}/transactions/{tx.id}/edit", status_code=303
        )

    tx.type = tx_enum
    tx.timestamp = timestamp
    tx.quantity = quantity
    tx.price = price
    tx.manual_value = manual_value
    tx.invested_override = invested_override
    tx.fees = fees
    tx.note = note
    db.commit()
    flash(request, "Transaction updated", "success")
    return RedirectResponse(url=f"/assets/{asset.id}", status_code=303)


@router.post("/{asset_id}/transactions/{tx_id}/delete")
async def delete_transaction(
    asset_id: int, tx_id: int, request: Request, db: Session = Depends(get_db)
):
    """Delete one transaction when remaining history is still valid."""
    user = get_user_from_session(request, db)
    if not user:
        return RedirectResponse(url="/login", status_code=303)

    asset = _load_asset(db, asset_id)
    tx = db.scalar(
        select(Transaction).where(
            Transaction.id == tx_id, Transaction.asset_id == asset_id
        )
    )
    if not asset or not tx:
        flash(request, "Transaction not found", "error")
        return RedirectResponse(url=f"/assets/{asset_id}", status_code=303)

    _ = await form_with_csrf(request)

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
