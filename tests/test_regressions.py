from __future__ import annotations

import re
from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models import Asset, AssetType, Basket, BasketAsset, Group, Transaction, TransactionType
from app.services.portfolio import (
    allocation_by_asset,
    allocation_by_group,
    build_basket_member_composition,
    build_dashboard_snapshot,
    compute_overlay_pnl_series,
    compute_portfolio_series,
    compute_basket_series,
    ensure_default_portfolio,
)
from app.services.pricing import PricingService


def _make_market_asset(portfolio_id: int, group_id: int, symbol: str, name: str) -> Asset:
    return Asset(
        portfolio_id=portfolio_id,
        symbol=symbol,
        name=name,
        asset_type=AssetType.MARKET,
        group_id=group_id,
    )


def _make_manual_asset(portfolio_id: int, group_id: int, symbol: str, name: str) -> Asset:
    return Asset(
        portfolio_id=portfolio_id,
        symbol=symbol,
        name=name,
        asset_type=AssetType.MANUAL,
        group_id=group_id,
    )


def _seed_group(db, portfolio_id: int, name: str) -> Group:
    group = Group(portfolio_id=portfolio_id, name=name)
    db.add(group)
    db.flush()
    return group


def test_basket_crud(authed_client, csrf_token_for, db_session_factory):
    with db_session_factory() as db:
        portfolio = ensure_default_portfolio(db)
        group = _seed_group(db, portfolio.id, "stonks")
        asset_a = _make_market_asset(portfolio.id, group.id, "CRWD", "CrowdStrike")
        asset_b = _make_market_asset(portfolio.id, group.id, "PANW", "Palo Alto")
        db.add_all([asset_a, asset_b])
        db.commit()
        db.refresh(asset_a)
        db.refresh(asset_b)

    token = csrf_token_for("/baskets")
    response = authed_client.post(
        "/baskets/create",
        data={
            "csrf_token": token,
            "name": "growth basket",
            "asset_ids": [str(asset_a.id)],
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    with db_session_factory() as db:
        basket = db.scalar(select(Basket).where(Basket.name == "growth basket"))
        assert basket is not None
        basket_id = basket.id
        links = list(db.scalars(select(BasketAsset).where(BasketAsset.basket_id == basket_id)))
        assert len(links) == 1
        assert links[0].asset_id == asset_a.id
        assert links[0].weight is None

    token = csrf_token_for(f"/baskets/{basket_id}/edit")
    response = authed_client.post(
        f"/baskets/{basket_id}/edit",
        data={
            "csrf_token": token,
            "name": "unequal basket",
            "asset_ids": [str(asset_a.id), str(asset_b.id)],
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    with db_session_factory() as db:
        basket = db.get(Basket, basket_id)
        assert basket is not None
        assert basket.name == "unequal basket"
        links = list(
            db.scalars(
                select(BasketAsset)
                .where(BasketAsset.basket_id == basket_id)
                .order_by(BasketAsset.asset_id)
            )
        )
        assert len(links) == 2
        assert links[0].weight is None
        assert links[1].weight is None

    token = csrf_token_for(f"/baskets/{basket_id}/edit")
    response = authed_client.post(
        f"/baskets/{basket_id}/edit",
        data={
            "csrf_token": token,
            "name": "unequal basket",
            "asset_ids": [str(asset_b.id)],
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    with db_session_factory() as db:
        links = list(db.scalars(select(BasketAsset).where(BasketAsset.basket_id == basket_id)))
        assert len(links) == 1
        assert links[0].asset_id == asset_b.id

    token = csrf_token_for(f"/baskets/{basket_id}")
    response = authed_client.post(
        f"/baskets/{basket_id}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    with db_session_factory() as db:
        assert db.get(Basket, basket_id) is None
        remaining_links = list(db.scalars(select(BasketAsset).where(BasketAsset.basket_id == basket_id)))
        assert remaining_links == []
        assert db.scalar(select(Asset).where(Asset.id == asset_a.id)) is not None
        assert db.scalar(select(Asset).where(Asset.id == asset_b.id)) is not None


def test_basket_inputs_show_live_share_defaults_and_no_weight_fields(
    authed_client,
    csrf_token_for,
    db_session_factory,
):
    with db_session_factory() as db:
        portfolio = ensure_default_portfolio(db)
        group = _seed_group(db, portfolio.id, "weights")
        asset = _make_market_asset(portfolio.id, group.id, "WGHT", "Weight Asset")
        db.add(asset)
        db.flush()
        db.add(
            Transaction(
                portfolio_id=portfolio.id,
                asset_id=asset.id,
                type=TransactionType.BUY,
                timestamp=datetime(2026, 2, 15, 13, 0, tzinfo=timezone.utc),
                quantity=12.5,
                price=100,
                fees=0,
            )
        )
        db.commit()
        db.refresh(asset)
        asset_id = asset.id

    response = authed_client.get("/baskets")
    assert response.status_code == 200
    assert f'name="weight_{asset_id}"' not in response.text
    assert "12.500000 shares" in response.text

    token = csrf_token_for("/baskets")
    response = authed_client.post(
        "/baskets/create",
        data={
            "csrf_token": token,
            "name": "share check",
            "asset_ids": [str(asset_id)],
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    with db_session_factory() as db:
        basket = db.scalar(select(Basket).where(Basket.name == "share check"))
        assert basket is not None
        basket_id = basket.id

    response = authed_client.get(f"/baskets/{basket_id}/edit")
    assert response.status_code == 200
    assert f'name="weight_{asset_id}"' not in response.text
    assert "12.500000 shares" in response.text


def test_basket_composition_auto_updates_when_share_counts_change(
    db_session_factory,
    app,
    mock_provider,
):
    mock_provider.set_latest("AUTOA", 200.0)
    mock_provider.set_latest("AUTOB", 50.0)

    with db_session_factory() as db:
        portfolio = ensure_default_portfolio(db)
        group = _seed_group(db, portfolio.id, "auto")
        asset_a = _make_market_asset(portfolio.id, group.id, "AUTOA", "Auto A")
        asset_b = _make_market_asset(portfolio.id, group.id, "AUTOB", "Auto B")
        db.add_all([asset_a, asset_b])
        db.flush()

        db.add_all(
            [
                Transaction(
                    portfolio_id=portfolio.id,
                    asset_id=asset_a.id,
                    type=TransactionType.BUY,
                    timestamp=datetime(2026, 2, 15, 13, 0, tzinfo=timezone.utc),
                    quantity=2.0,
                    price=100.0,
                    fees=0.0,
                ),
                Transaction(
                    portfolio_id=portfolio.id,
                    asset_id=asset_b.id,
                    type=TransactionType.BUY,
                    timestamp=datetime(2026, 2, 15, 13, 0, tzinfo=timezone.utc),
                    quantity=8.0,
                    price=100.0,
                    fees=0.0,
                ),
            ]
        )

        basket = Basket(portfolio_id=portfolio.id, name="auto basket")
        db.add(basket)
        db.flush()
        db.add_all(
            [
                BasketAsset(basket_id=basket.id, asset_id=asset_a.id, weight=None),
                BasketAsset(basket_id=basket.id, asset_id=asset_b.id, weight=None),
            ]
        )
        db.commit()

        loaded = db.scalar(
            select(Basket)
            .where(Basket.id == basket.id)
            .options(
                selectinload(Basket.assets)
                .selectinload(BasketAsset.asset)
                .selectinload(Asset.transactions)
            )
        )
        assert loaded is not None
        before = build_basket_member_composition(
            db,
            app.state.pricing_service,
            loaded.assets,
        )
        before_map = {row.symbol: row.allocation_pct for row in before}
        before_values = {row.symbol: row.current_value for row in before}
        assert before_values["AUTOA"] == pytest.approx(400.0)
        assert before_values["AUTOB"] == pytest.approx(400.0)
        assert before_map["AUTOA"] == pytest.approx(50.0)
        assert before_map["AUTOB"] == pytest.approx(50.0)
        assert sum(before_map.values()) == pytest.approx(100.0)

        db.add(
            Transaction(
                portfolio_id=portfolio.id,
                asset_id=asset_a.id,
                type=TransactionType.BUY,
                timestamp=datetime(2026, 2, 16, 13, 0, tzinfo=timezone.utc),
                quantity=8.0,
                price=100.0,
                fees=0.0,
            )
        )
        db.commit()

        loaded_after = db.scalar(
            select(Basket)
            .where(Basket.id == basket.id)
            .options(
                selectinload(Basket.assets)
                .selectinload(BasketAsset.asset)
                .selectinload(Asset.transactions)
            )
        )
        assert loaded_after is not None
        after = build_basket_member_composition(
            db,
            app.state.pricing_service,
            loaded_after.assets,
        )
        after_map = {row.symbol: row.allocation_pct for row in after}
        after_values = {row.symbol: row.current_value for row in after}
        assert after_values["AUTOA"] == pytest.approx(2000.0)
        assert after_values["AUTOB"] == pytest.approx(400.0)
        assert after_map["AUTOA"] == pytest.approx((2000.0 / 2400.0) * 100.0)
        assert after_map["AUTOB"] == pytest.approx((400.0 / 2400.0) * 100.0)
        assert sum(after_map.values()) == pytest.approx(100.0)


def test_asset_create_with_initial_holdings_and_manual_values(
    authed_client,
    csrf_token_for,
    db_session_factory,
    app,
    mock_provider,
):
    mock_provider.set_latest("MKTINIT", 120.0)
    mock_provider.set_latest("MKTWL", 50.0)

    with db_session_factory() as db:
        portfolio = ensure_default_portfolio(db)
        group = _seed_group(db, portfolio.id, "core")
        group_id = group.id
        db.commit()

    token = csrf_token_for("/")
    authed_client.post(
        "/assets",
        data={
            "csrf_token": token,
            "symbol": "MKTINIT",
            "name": "Market Initial",
            "asset_type": "market",
            "group_id": str(group_id),
            "initial_quantity": "10",
            "initial_buy_price": "100",
            "initial_fees": "5",
        },
        follow_redirects=False,
    )

    token = csrf_token_for("/")
    authed_client.post(
        "/assets",
        data={
            "csrf_token": token,
            "symbol": "MKTWL",
            "name": "Watchlist",
            "asset_type": "market",
            "group_id": str(group_id),
            "initial_quantity": "",
            "initial_buy_price": "",
            "initial_fees": "",
        },
        follow_redirects=False,
    )

    token = csrf_token_for("/")
    authed_client.post(
        "/assets",
        data={
            "csrf_token": token,
            "symbol": "MANVAL",
            "name": "Manual Value",
            "asset_type": "manual",
            "group_id": str(group_id),
            "initial_value": "5000",
            "initial_invested": "",
        },
        follow_redirects=False,
    )

    token = csrf_token_for("/")
    authed_client.post(
        "/assets",
        data={
            "csrf_token": token,
            "symbol": "MANINV",
            "name": "Manual Invested",
            "asset_type": "manual",
            "group_id": str(group_id),
            "initial_value": "7000",
            "initial_invested": "6500",
        },
        follow_redirects=False,
    )

    with db_session_factory() as db:
        portfolio = ensure_default_portfolio(db)
        market = db.scalar(select(Asset).where(Asset.symbol == "MKTINIT"))
        watchlist = db.scalar(select(Asset).where(Asset.symbol == "MKTWL"))
        manual_value = db.scalar(select(Asset).where(Asset.symbol == "MANVAL"))
        manual_invested = db.scalar(select(Asset).where(Asset.symbol == "MANINV"))

        assert market is not None
        assert watchlist is not None
        assert manual_value is not None
        assert manual_invested is not None

        market_txs = list(db.scalars(select(Transaction).where(Transaction.asset_id == market.id)))
        watchlist_txs = list(db.scalars(select(Transaction).where(Transaction.asset_id == watchlist.id)))
        manual_value_txs = list(db.scalars(select(Transaction).where(Transaction.asset_id == manual_value.id)))
        manual_invested_txs = list(db.scalars(select(Transaction).where(Transaction.asset_id == manual_invested.id)))

        assert len(market_txs) == 1
        assert market_txs[0].type == TransactionType.BUY
        assert watchlist_txs == []
        assert len(manual_value_txs) == 1
        assert manual_value_txs[0].type == TransactionType.MANUAL_VALUE_UPDATE
        assert len(manual_invested_txs) == 2
        assert {tx.type for tx in manual_invested_txs} == {
            TransactionType.BUY,
            TransactionType.MANUAL_VALUE_UPDATE,
        }

        snapshot = build_dashboard_snapshot(db, app.state.pricing_service, portfolio.id)
        rows = {row.symbol: row for row in snapshot.positions}

        assert rows["MKTINIT"].quantity == pytest.approx(10)
        assert rows["MKTINIT"].avg_cost == pytest.approx(100.5)
        assert rows["MKTINIT"].current_value == pytest.approx(1200.0)

        assert rows["MKTWL"].quantity == pytest.approx(0.0)
        assert rows["MKTWL"].current_value == pytest.approx(0.0)

        assert rows["MANVAL"].current_value == pytest.approx(5000.0)
        assert rows["MANVAL"].unrealized_pnl is None

        assert rows["MANINV"].current_value == pytest.approx(7000.0)
        assert rows["MANINV"].unrealized_pnl == pytest.approx(500.0)


def test_add_transaction_accepts_mixed_naive_and_aware_timestamps(
    authed_client,
    csrf_token_for,
    db_session_factory,
):
    with db_session_factory() as db:
        portfolio = ensure_default_portfolio(db)
        group = _seed_group(db, portfolio.id, "timing")
        asset = _make_market_asset(portfolio.id, group.id, "PANW", "Palo Alto")
        db.add(asset)
        db.flush()

        db.add(
            Transaction(
                portfolio_id=portfolio.id,
                asset_id=asset.id,
                type=TransactionType.BUY,
                # Intentionally naive to mirror SQLite/legacy data behavior.
                timestamp=datetime(2026, 2, 15, 13, 14, 51),
                quantity=10,
                price=166,
                fees=0,
            )
        )
        db.commit()
        asset_id = asset.id

    token = csrf_token_for(f"/assets/{asset_id}")
    response = authed_client.post(
        f"/assets/{asset_id}/transactions",
        data={
            "csrf_token": token,
            "type": "BUY",
            "timestamp": "2026-02-15T13:15",
            "quantity": "20",
            "price": "167",
            "fees": "0",
            "note": "follow up buy",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)
    assert response.headers["location"] == f"/assets/{asset_id}"

    with db_session_factory() as db:
        txs = list(
            db.scalars(
                select(Transaction)
                .where(Transaction.asset_id == asset_id)
                .order_by(Transaction.timestamp.asc(), Transaction.id.asc())
            )
        )
        assert len(txs) == 2


def test_manual_asset_transactions_support_sell_and_invested_override(
    authed_client,
    csrf_token_for,
    db_session_factory,
    app,
):
    with db_session_factory() as db:
        portfolio = ensure_default_portfolio(db)
        group = _seed_group(db, portfolio.id, "cash")
        asset = _make_manual_asset(portfolio.id, group.id, "USD", "US Dollar")
        db.add(asset)
        db.flush()
        db.add(
            Transaction(
                portfolio_id=portfolio.id,
                asset_id=asset.id,
                type=TransactionType.BUY,
                timestamp=datetime(2026, 2, 15, 13, 0, tzinfo=timezone.utc),
                quantity=100000.0,
                price=1.0,
                fees=0.0,
                note="Initial cash basis",
            )
        )
        db.add(
            Transaction(
                portfolio_id=portfolio.id,
                asset_id=asset.id,
                type=TransactionType.MANUAL_VALUE_UPDATE,
                timestamp=datetime(2026, 2, 15, 13, 1, tzinfo=timezone.utc),
                manual_value=100000.0,
                note="Initial value",
            )
        )
        db.commit()
        asset_id = asset.id

    token = csrf_token_for(f"/assets/{asset_id}")
    response = authed_client.post(
        f"/assets/{asset_id}/transactions",
        data={
            "csrf_token": token,
            "type": "SELL",
            "timestamp": "2026-02-16T13:00",
            "quantity": "20000",
            "price": "20000",
            "fees": "0",
            "note": "Sold cash position",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    token = csrf_token_for(f"/assets/{asset_id}")
    response = authed_client.post(
        f"/assets/{asset_id}/transactions",
        data={
            "csrf_token": token,
            "type": "MANUAL_VALUE_UPDATE",
            "timestamp": "2026-02-16T13:01",
            "manual_value": "80000",
            "manual_invested_override": "80000",
            "fees": "0",
            "note": "Sync invested to current value",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    with db_session_factory() as db:
        txs = list(
            db.scalars(
                select(Transaction)
                .where(Transaction.asset_id == asset_id)
                .order_by(Transaction.timestamp.asc(), Transaction.id.asc())
            )
        )
        assert [tx.type for tx in txs] == [
            TransactionType.BUY,
            TransactionType.MANUAL_VALUE_UPDATE,
            TransactionType.SELL,
            TransactionType.MANUAL_VALUE_UPDATE,
        ]
        assert txs[-1].invested_override == pytest.approx(80000.0)

        portfolio = ensure_default_portfolio(db)
        snapshot = build_dashboard_snapshot(db, app.state.pricing_service, portfolio.id)
        rows = {row.symbol: row for row in snapshot.positions}
        assert rows["USD"].avg_cost == pytest.approx(80000.0)
        assert rows["USD"].current_value == pytest.approx(80000.0)
        assert rows["USD"].unrealized_pnl == pytest.approx(0.0)


def test_asset_edit_delete_rules(
    authed_client,
    csrf_token_for,
    db_session_factory,
):
    with db_session_factory() as db:
        portfolio = ensure_default_portfolio(db)
        group_a = _seed_group(db, portfolio.id, "group-a")
        group_b = _seed_group(db, portfolio.id, "group-b")

        asset_with_tx = _make_market_asset(portfolio.id, group_a.id, "EDITMKT", "Editable")
        asset_no_tx = _make_market_asset(portfolio.id, group_a.id, "EMPTY", "No Tx")
        db.add_all([asset_with_tx, asset_no_tx])
        db.flush()

        db.add(
            Transaction(
                portfolio_id=portfolio.id,
                asset_id=asset_with_tx.id,
                type=TransactionType.BUY,
                timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
                quantity=2,
                price=100,
                fees=0,
            )
        )

        db.commit()
        asset_with_tx_id = asset_with_tx.id
        asset_no_tx_id = asset_no_tx.id
        group_b_id = group_b.id

    token = csrf_token_for(f"/assets/{asset_with_tx_id}/edit")
    response = authed_client.post(
        f"/assets/{asset_with_tx_id}/edit",
        data={
            "csrf_token": token,
            "name": "Renamed Asset",
            "symbol": "EDITMKT",
            "asset_type": "market",
            "group_id": str(group_b_id),
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    with db_session_factory() as db:
        asset = db.get(Asset, asset_with_tx_id)
        assert asset is not None
        assert asset.name == "Renamed Asset"
        assert asset.group_id == group_b_id

    token = csrf_token_for(f"/assets/{asset_with_tx_id}/edit")
    authed_client.post(
        f"/assets/{asset_with_tx_id}/edit",
        data={
            "csrf_token": token,
            "name": "Renamed Asset",
            "symbol": "CHANGED",
            "asset_type": "market",
            "group_id": str(group_b_id),
        },
        follow_redirects=False,
    )

    with db_session_factory() as db:
        asset = db.get(Asset, asset_with_tx_id)
        assert asset is not None
        assert asset.symbol == "EDITMKT"

    token = csrf_token_for(f"/assets/{asset_with_tx_id}/edit")
    authed_client.post(
        f"/assets/{asset_with_tx_id}/edit",
        data={
            "csrf_token": token,
            "name": "Renamed Asset",
            "symbol": "EDITMKT",
            "asset_type": "manual",
            "group_id": str(group_b_id),
        },
        follow_redirects=False,
    )

    with db_session_factory() as db:
        asset = db.get(Asset, asset_with_tx_id)
        assert asset is not None
        assert asset.asset_type == AssetType.MARKET

    token = csrf_token_for(f"/assets/{asset_with_tx_id}")
    authed_client.post(
        f"/assets/{asset_with_tx_id}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )

    with db_session_factory() as db:
        assert db.get(Asset, asset_with_tx_id) is not None

    token = csrf_token_for(f"/assets/{asset_with_tx_id}")
    authed_client.post(
        f"/assets/{asset_with_tx_id}/archive",
        data={"csrf_token": token},
        follow_redirects=False,
    )

    with db_session_factory() as db:
        asset = db.get(Asset, asset_with_tx_id)
        assert asset is not None
        assert asset.is_archived is True

    token = csrf_token_for(f"/assets/{asset_no_tx_id}")
    authed_client.post(
        f"/assets/{asset_no_tx_id}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )

    with db_session_factory() as db:
        assert db.get(Asset, asset_no_tx_id) is None


def test_crypto_symbol_alias_uses_usd_quote(db_session_factory, app, mock_provider):
    mock_provider.set_latest("BTC-USD", 69500.0)

    with db_session_factory() as db:
        portfolio = ensure_default_portfolio(db)
        group = _seed_group(db, portfolio.id, "crypto")
        btc = _make_market_asset(portfolio.id, group.id, "BTC", "Bitcoin")
        db.add(btc)
        db.flush()
        db.add(
            Transaction(
                portfolio_id=portfolio.id,
                asset_id=btc.id,
                type=TransactionType.BUY,
                timestamp=datetime(2026, 2, 15, 13, 0, tzinfo=timezone.utc),
                quantity=0.25,
                price=70000.0,
                fees=0.0,
            )
        )
        db.commit()

        snapshot = build_dashboard_snapshot(db, app.state.pricing_service, portfolio.id)
        row = next((entry for entry in snapshot.positions if entry.symbol == "BTC"), None)

    assert row is not None
    assert row.current_price == pytest.approx(69500.0)
    assert row.current_value == pytest.approx(17375.0)


def test_btc_position_with_negative_cost_basis_pnl_is_computed_correctly(
    authed_client,
    csrf_token_for,
    db_session_factory,
    app,
    mock_provider,
):
    quantity = 0.157426328
    current_price = 68846.0
    target_unrealized_pnl = 22509.29
    buy_price = current_price - (target_unrealized_pnl / quantity)

    mock_provider.set_latest("BTC-USD", current_price)

    with db_session_factory() as db:
        portfolio = ensure_default_portfolio(db)
        crypto = _seed_group(db, portfolio.id, "crypto")
        group_id = crypto.id
        db.commit()

    page = authed_client.get("/")
    assert page.status_code == 200
    initial_buy_match = re.search(
        r'<input[^>]*name="initial_buy_price"[^>]*>',
        page.text,
    )
    assert initial_buy_match is not None
    assert 'min="0"' not in initial_buy_match.group(0)

    token = csrf_token_for("/")
    response = authed_client.post(
        "/assets",
        data={
            "csrf_token": token,
            "symbol": "BTC",
            "name": "Bitcoin",
            "asset_type": "market",
            "group_id": str(group_id),
            "initial_quantity": str(quantity),
            "initial_buy_price": f"{buy_price:.12f}",
            "initial_fees": "0",
        },
        follow_redirects=False,
    )
    assert response.status_code in (302, 303)

    with db_session_factory() as db:
        portfolio = ensure_default_portfolio(db)
        snapshot = build_dashboard_snapshot(db, app.state.pricing_service, portfolio.id)
        row = next((entry for entry in snapshot.positions if entry.symbol == "BTC"), None)

    assert row is not None
    assert row.quantity == pytest.approx(quantity)
    assert row.current_price == pytest.approx(current_price)
    assert row.current_value == pytest.approx(quantity * current_price, abs=0.01)
    assert row.unrealized_pnl is not None
    assert row.unrealized_pnl == pytest.approx(target_unrealized_pnl, abs=0.01)


def test_allocation_by_group_with_market_and_manual(db_session_factory, app, mock_provider):
    mock_provider.set_latest("ALOC1", 150.0)

    with db_session_factory() as db:
        portfolio = ensure_default_portfolio(db)
        group_market = _seed_group(db, portfolio.id, "stonks")
        group_manual = _seed_group(db, portfolio.id, "real estate")

        market_asset = _make_market_asset(portfolio.id, group_market.id, "ALOC1", "Alloc Market")
        manual_asset = _make_manual_asset(portfolio.id, group_manual.id, "HOME", "House")
        db.add_all([market_asset, manual_asset])
        db.flush()

        db.add(
            Transaction(
                portfolio_id=portfolio.id,
                asset_id=market_asset.id,
                type=TransactionType.BUY,
                timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
                quantity=2,
                price=100,
                fees=0,
            )
        )
        db.add(
            Transaction(
                portfolio_id=portfolio.id,
                asset_id=manual_asset.id,
                type=TransactionType.MANUAL_VALUE_UPDATE,
                timestamp=datetime(2025, 1, 2, tzinfo=timezone.utc),
                manual_value=700,
            )
        )
        db.commit()

        snapshot = build_dashboard_snapshot(db, app.state.pricing_service, portfolio.id)
        allocation = allocation_by_group(snapshot.positions)

    assert set(allocation.labels) == {"stonks", "real estate"}

    values = dict(zip(allocation.labels, allocation.values, strict=True))
    percentages = dict(zip(allocation.labels, allocation.percentages, strict=True))

    assert values["stonks"] == pytest.approx(300.0)
    assert values["real estate"] == pytest.approx(700.0)
    assert sum(allocation.percentages) == pytest.approx(100.0)
    assert percentages["stonks"] == pytest.approx(30.0)
    assert percentages["real estate"] == pytest.approx(70.0)


def test_basket_series_normalization_and_intersection(mock_provider):
    pricing = PricingService(provider=mock_provider, ttl_seconds=60)

    d1 = date(2025, 1, 1)
    d2 = date(2025, 1, 2)
    d3 = date(2025, 1, 3)

    mock_provider.set_history("CRWD", [(d1, 100.0), (d2, 110.0), (d3, 120.0)])
    mock_provider.set_history("PANW", [(d2, 200.0), (d3, 220.0)])

    tx_crwd = SimpleNamespace(
        id=1,
        type=TransactionType.BUY,
        timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
        quantity=2.0,
        price=100.0,
        fees=0.0,
        manual_value=None,
    )
    tx_panw = SimpleNamespace(
        id=2,
        type=TransactionType.BUY,
        timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
        quantity=8.0,
        price=200.0,
        fees=0.0,
        manual_value=None,
    )
    link_crwd = SimpleNamespace(
        asset_id=1,
        asset=SimpleNamespace(
            symbol="CRWD",
            asset_type=AssetType.MARKET,
            is_archived=False,
            transactions=[tx_crwd],
        ),
    )
    link_panw = SimpleNamespace(
        asset_id=2,
        asset=SimpleNamespace(
            symbol="PANW",
            asset_type=AssetType.MARKET,
            is_archived=False,
            transactions=[tx_panw],
        ),
    )

    result = compute_basket_series(pricing, [link_crwd, link_panw], start=d1, end=d3)

    assert result.error_message is None
    assert result.missing_symbols == []
    assert [point.date for point in result.points] == [d2, d3]
    assert result.points[0].value == pytest.approx(100.0)

    expected_day3 = (0.2 * (120.0 / 110.0) * 100.0) + (0.8 * (220.0 / 200.0) * 100.0)
    assert result.points[1].value == pytest.approx(expected_day3)


def test_basket_series_reports_missing_members(mock_provider):
    pricing = PricingService(provider=mock_provider, ttl_seconds=60)

    d1 = date(2025, 1, 1)
    d2 = date(2025, 1, 2)

    mock_provider.set_history("CRWD", [(d1, 100.0), (d2, 105.0)])
    mock_provider.set_history("MISSING", [])

    tx_crwd = SimpleNamespace(
        id=1,
        type=TransactionType.BUY,
        timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
        quantity=1.0,
        price=100.0,
        fees=0.0,
        manual_value=None,
    )
    tx_missing = SimpleNamespace(
        id=2,
        type=TransactionType.BUY,
        timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
        quantity=1.0,
        price=100.0,
        fees=0.0,
        manual_value=None,
    )
    link_crwd = SimpleNamespace(
        asset_id=1,
        asset=SimpleNamespace(
            symbol="CRWD",
            asset_type=AssetType.MARKET,
            is_archived=False,
            transactions=[tx_crwd],
        ),
    )
    link_missing = SimpleNamespace(
        asset_id=2,
        asset=SimpleNamespace(
            symbol="MISSING",
            asset_type=AssetType.MARKET,
            is_archived=False,
            transactions=[tx_missing],
        ),
    )

    result = compute_basket_series(pricing, [link_crwd, link_missing], start=d1, end=d2)

    assert result.points == []
    assert "MISSING" in result.missing_symbols
    assert result.error_message is not None
    assert "Missing historical data" in result.error_message


def test_dashboard_snapshot_includes_basket_as_first_class_row(
    authed_client,
    db_session_factory,
    app,
    mock_provider,
):
    mock_provider.set_latest("CRWD", 400.0)
    mock_provider.set_latest("PANW", 200.0)
    mock_provider.set_latest("MSFT", 300.0)

    with db_session_factory() as db:
        portfolio = ensure_default_portfolio(db)
        group = _seed_group(db, portfolio.id, "stonks")

        crwd = _make_market_asset(portfolio.id, group.id, "CRWD", "CrowdStrike")
        panw = _make_market_asset(portfolio.id, group.id, "PANW", "Palo Alto Networks")
        msft = _make_market_asset(portfolio.id, group.id, "MSFT", "Microsoft")
        db.add_all([crwd, panw, msft])
        db.flush()

        db.add_all(
            [
                Transaction(
                    portfolio_id=portfolio.id,
                    asset_id=crwd.id,
                    type=TransactionType.BUY,
                    timestamp=datetime(2026, 2, 1, tzinfo=timezone.utc),
                    quantity=10,
                    price=350.0,
                    fees=0,
                ),
                Transaction(
                    portfolio_id=portfolio.id,
                    asset_id=panw.id,
                    type=TransactionType.BUY,
                    timestamp=datetime(2026, 2, 1, tzinfo=timezone.utc),
                    quantity=5,
                    price=180.0,
                    fees=0,
                ),
                Transaction(
                    portfolio_id=portfolio.id,
                    asset_id=msft.id,
                    type=TransactionType.BUY,
                    timestamp=datetime(2026, 2, 1, tzinfo=timezone.utc),
                    quantity=1,
                    price=280.0,
                    fees=0,
                ),
            ]
        )

        basket = Basket(portfolio_id=portfolio.id, name="cybersecurity")
        db.add(basket)
        db.flush()
        db.add_all(
            [
                BasketAsset(basket_id=basket.id, asset_id=crwd.id, weight=None),
                BasketAsset(basket_id=basket.id, asset_id=panw.id, weight=None),
            ]
        )
        basket_id = basket.id
        db.commit()

        snapshot = build_dashboard_snapshot(db, app.state.pricing_service, portfolio.id)
        basket_row = next(
            (row for row in snapshot.positions if row.row_kind == "basket" and row.name == "cybersecurity"),
            None,
        )

        group_allocation = allocation_by_group(snapshot.positions)
        group_values = dict(
            zip(group_allocation.labels, group_allocation.values, strict=True)
        )
        asset_allocation = allocation_by_asset(
            snapshot.positions,
            snapshot.basket_member_asset_ids,
        )
        asset_values = dict(
            zip(asset_allocation.labels, asset_allocation.values, strict=True)
        )

    assert basket_row is not None
    assert basket_row.symbol == f"BASKET:{basket_id}"
    assert basket_row.detail_path == f"/baskets/{basket_id}"
    assert basket_row.current_value == pytest.approx(5000.0)
    assert basket_row.unrealized_pnl == pytest.approx(600.0)
    assert basket_row.counts_in_totals is False
    assert basket_row.counts_in_allocation is False
    assert basket_row.valuation_scope == "derived"
    assert basket_row.basket_member_ids == [crwd.id, panw.id]
    assert "baskets" not in group_values
    assert group_values["stonks"] == pytest.approx(5300.0)
    assert snapshot.canonical_total_value == pytest.approx(5300.0)
    assert snapshot.canonical_total_unrealized_pnl == pytest.approx(620.0)
    assert snapshot.derived_total_value == pytest.approx(5000.0)
    assert snapshot.derived_total_unrealized_pnl == pytest.approx(600.0)
    assert snapshot.total_value == pytest.approx(5300.0)
    assert snapshot.total_unrealized_pnl == pytest.approx(620.0)
    assert snapshot.group_totals["stonks"]["value"] == pytest.approx(5300.0)

    assert asset_values["cybersecurity"] == pytest.approx(5000.0)
    assert asset_values["MSFT"] == pytest.approx(300.0)
    assert "CRWD" not in asset_values
    assert "PANW" not in asset_values

    response = authed_client.get("/")
    assert response.status_code == 200
    assert f"/baskets/{basket_id}" in response.text
    assert "Allocation By Group" in response.text
    assert "Allocation By Asset" in response.text
    assert "allocationByGroupChart" in response.text
    assert "allocationByAssetChart" in response.text
    assert "Derived Baskets (excluded from canonical totals)" in response.text
    assert 'data-counts-in-totals="0"' in response.text


def test_compute_portfolio_series_uses_forward_fill_and_manual_step(
    db_session_factory,
    app,
    mock_provider,
):
    d1 = date(2025, 1, 1)
    d2 = date(2025, 1, 2)
    d3 = date(2025, 1, 3)
    d4 = date(2025, 1, 4)
    d5 = date(2025, 1, 5)

    mock_provider.set_history("MKT", [(d1, 100.0), (d3, 110.0), (d5, 120.0)])

    with db_session_factory() as db:
        portfolio = ensure_default_portfolio(db)
        g1 = _seed_group(db, portfolio.id, "stonks")
        g2 = _seed_group(db, portfolio.id, "manual")
        market = _make_market_asset(portfolio.id, g1.id, "MKT", "Market")
        manual = _make_manual_asset(portfolio.id, g2.id, "HOUSE", "House")
        db.add_all([market, manual])
        db.flush()

        db.add(
            Transaction(
                portfolio_id=portfolio.id,
                asset_id=market.id,
                type=TransactionType.BUY,
                timestamp=datetime(2025, 1, 1, 9, 0, tzinfo=timezone.utc),
                quantity=2.0,
                price=90.0,
                fees=0.0,
            )
        )
        db.add_all(
            [
                Transaction(
                    portfolio_id=portfolio.id,
                    asset_id=manual.id,
                    type=TransactionType.MANUAL_VALUE_UPDATE,
                    timestamp=datetime(2025, 1, 2, 9, 0, tzinfo=timezone.utc),
                    manual_value=50.0,
                ),
                Transaction(
                    portfolio_id=portfolio.id,
                    asset_id=manual.id,
                    type=TransactionType.MANUAL_VALUE_UPDATE,
                    timestamp=datetime(2025, 1, 4, 9, 0, tzinfo=timezone.utc),
                    manual_value=80.0,
                ),
            ]
        )
        db.commit()

        result = compute_portfolio_series(
            db=db,
            pricing_service=app.state.pricing_service,
            portfolio_id=portfolio.id,
            start=d1,
            end=d5,
        )

    assert result.error_message is None
    assert result.missing_symbols == []
    assert [point.date for point in result.points] == [d1, d2, d3, d4, d5]
    assert [point.total_value_usd for point in result.points] == pytest.approx(
        [200.0, 250.0, 270.0, 300.0, 320.0]
    )


def test_compute_portfolio_series_excludes_basket_overlays(
    db_session_factory,
    app,
    mock_provider,
):
    d1 = date(2025, 1, 1)
    d2 = date(2025, 1, 2)
    mock_provider.set_history("CYBR", [(d1, 100.0), (d2, 110.0)])

    with db_session_factory() as db:
        portfolio = ensure_default_portfolio(db)
        group = _seed_group(db, portfolio.id, "stonks")
        asset = _make_market_asset(portfolio.id, group.id, "CYBR", "Cyber")
        db.add(asset)
        db.flush()
        db.add(
            Transaction(
                portfolio_id=portfolio.id,
                asset_id=asset.id,
                type=TransactionType.BUY,
                timestamp=datetime(2025, 1, 1, 9, 0, tzinfo=timezone.utc),
                quantity=3.0,
                price=90.0,
                fees=0.0,
            )
        )
        basket = Basket(portfolio_id=portfolio.id, name="overlay")
        db.add(basket)
        db.flush()
        db.add(BasketAsset(basket_id=basket.id, asset_id=asset.id, weight=None))
        db.commit()

        result = compute_portfolio_series(
            db=db,
            pricing_service=app.state.pricing_service,
            portfolio_id=portfolio.id,
            start=d1,
            end=d2,
        )

    assert result.error_message is None
    assert [point.total_value_usd for point in result.points] == pytest.approx([300.0, 330.0])


def test_dashboard_includes_portfolio_chart_payload(
    authed_client,
    db_session_factory,
    mock_provider,
):
    # Use a simple latest quote path to ensure dashboard has a non-empty series.
    mock_provider.set_latest("CHART", 50.0)
    mock_provider.set_history("CHART", [(date.today(), 50.0)])

    with db_session_factory() as db:
        portfolio = ensure_default_portfolio(db)
        group = _seed_group(db, portfolio.id, "charting")
        asset = _make_market_asset(portfolio.id, group.id, "CHART", "Chart Asset")
        db.add(asset)
        db.flush()
        db.add(
            Transaction(
                portfolio_id=portfolio.id,
                asset_id=asset.id,
                type=TransactionType.BUY,
                timestamp=datetime.now(timezone.utc),
                quantity=1.0,
                price=40.0,
                fees=0.0,
            )
        )
        db.commit()

    response = authed_client.get("/")
    assert response.status_code == 200
    assert "Portfolio Value (USD)" in response.text
    assert "portfolioSeriesLabels" in response.text
    assert "portfolioSeriesValues" in response.text


def test_overlapping_baskets_do_not_change_canonical_totals(
    db_session_factory,
    app,
    mock_provider,
):
    mock_provider.set_latest("OVA", 100.0)
    mock_provider.set_latest("OVB", 50.0)

    with db_session_factory() as db:
        portfolio = ensure_default_portfolio(db)
        group = _seed_group(db, portfolio.id, "stonks")
        a = _make_market_asset(portfolio.id, group.id, "OVA", "Overlap A")
        b = _make_market_asset(portfolio.id, group.id, "OVB", "Overlap B")
        db.add_all([a, b])
        db.flush()

        db.add_all(
            [
                Transaction(
                    portfolio_id=portfolio.id,
                    asset_id=a.id,
                    type=TransactionType.BUY,
                    timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    quantity=2.0,
                    price=90.0,
                    fees=0.0,
                ),
                Transaction(
                    portfolio_id=portfolio.id,
                    asset_id=b.id,
                    type=TransactionType.BUY,
                    timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
                    quantity=4.0,
                    price=40.0,
                    fees=0.0,
                ),
            ]
        )

        basket_one = Basket(portfolio_id=portfolio.id, name="basket one")
        basket_two = Basket(portfolio_id=portfolio.id, name="basket two")
        db.add_all([basket_one, basket_two])
        db.flush()
        db.add_all(
            [
                BasketAsset(basket_id=basket_one.id, asset_id=a.id, weight=None),
                BasketAsset(basket_id=basket_two.id, asset_id=a.id, weight=None),
                BasketAsset(basket_id=basket_two.id, asset_id=b.id, weight=None),
            ]
        )
        db.commit()

        snapshot = build_dashboard_snapshot(db, app.state.pricing_service, portfolio.id)
        derived_rows = [row for row in snapshot.positions if row.row_kind == "basket"]

    assert snapshot.canonical_total_value == pytest.approx(400.0)
    assert snapshot.canonical_total_unrealized_pnl == pytest.approx(60.0)
    assert len(derived_rows) == 2
    derived_values = {row.name: row.current_value for row in derived_rows}
    assert derived_values["basket one"] == pytest.approx(200.0)
    assert derived_values["basket two"] == pytest.approx(400.0)


def test_dashboard_allows_setting_chart_time_range(
    authed_client,
    db_session_factory,
    mock_provider,
):
    d1 = date(2025, 1, 1)
    d2 = date(2025, 1, 2)
    d3 = date(2025, 1, 3)
    d4 = date(2025, 1, 4)
    d5 = date(2025, 1, 5)
    mock_provider.set_history("RANGE", [(d1, 100.0), (d2, 101.0), (d3, 102.0), (d4, 103.0), (d5, 104.0)])
    mock_provider.set_latest("RANGE", 104.0)

    with db_session_factory() as db:
        portfolio = ensure_default_portfolio(db)
        group = _seed_group(db, portfolio.id, "range")
        asset = _make_market_asset(portfolio.id, group.id, "RANGE", "Range Asset")
        db.add(asset)
        db.flush()
        db.add(
            Transaction(
                portfolio_id=portfolio.id,
                asset_id=asset.id,
                type=TransactionType.BUY,
                timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
                quantity=1.0,
                price=90.0,
                fees=0.0,
            )
        )
        db.commit()

    response = authed_client.get("/?chart_start=2025-01-02&chart_end=2025-01-04")
    assert response.status_code == 200
    assert 'name="chart_start" value="2025-01-02"' in response.text
    assert 'name="chart_end" value="2025-01-04"' in response.text
    assert 'portfolioSeriesLabels: ["2025-01-02", "2025-01-03", "2025-01-04"]' in response.text


def test_overlay_pnl_series_excludes_basket_members_and_combines_basket(
    db_session_factory,
    app,
    mock_provider,
):
    d1 = date(2025, 1, 1)
    d2 = date(2025, 1, 2)
    mock_provider.set_history("PNA", [(d1, 90.0), (d2, 110.0)])
    mock_provider.set_history("PNB", [(d1, 55.0), (d2, 45.0)])
    mock_provider.set_history("PNC", [(d1, 30.0), (d2, 10.0)])

    with db_session_factory() as db:
        portfolio = ensure_default_portfolio(db)
        group = _seed_group(db, portfolio.id, "pnl")
        asset_a = _make_market_asset(portfolio.id, group.id, "PNA", "PnL A")
        asset_b = _make_market_asset(portfolio.id, group.id, "PNB", "PnL B")
        asset_c = _make_market_asset(portfolio.id, group.id, "PNC", "PnL C")
        db.add_all([asset_a, asset_b, asset_c])
        db.flush()

        db.add_all(
            [
                Transaction(
                    portfolio_id=portfolio.id,
                    asset_id=asset_a.id,
                    type=TransactionType.BUY,
                    timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
                    quantity=1.0,
                    price=100.0,
                    fees=0.0,
                ),
                Transaction(
                    portfolio_id=portfolio.id,
                    asset_id=asset_b.id,
                    type=TransactionType.BUY,
                    timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
                    quantity=2.0,
                    price=50.0,
                    fees=0.0,
                ),
                Transaction(
                    portfolio_id=portfolio.id,
                    asset_id=asset_c.id,
                    type=TransactionType.BUY,
                    timestamp=datetime(2025, 1, 1, tzinfo=timezone.utc),
                    quantity=1.0,
                    price=20.0,
                    fees=0.0,
                ),
            ]
        )

        basket = Basket(portfolio_id=portfolio.id, name="PnL Basket")
        db.add(basket)
        db.flush()
        db.add_all(
            [
                BasketAsset(basket_id=basket.id, asset_id=asset_a.id, weight=None),
                BasketAsset(basket_id=basket.id, asset_id=asset_b.id, weight=None),
            ]
        )
        db.commit()

        result = compute_overlay_pnl_series(
            db=db,
            pricing_service=app.state.pricing_service,
            portfolio_id=portfolio.id,
            start=d1,
            end=d2,
        )

    assert result.error_message is None
    assert result.dates == [d1, d2]
    assert "PNA" not in result.series_by_label
    assert "PNB" not in result.series_by_label
    assert result.series_by_label["PnL Basket"] == pytest.approx([0.0, 0.0])
    assert result.series_by_label["PNC"] == pytest.approx([10.0, -10.0])


def test_dashboard_pnl_chart_selectors_include_baskets_and_exclude_members(
    authed_client,
    db_session_factory,
    mock_provider,
):
    d1 = date.today()
    mock_provider.set_history("SEL1", [(d1, 100.0)])
    mock_provider.set_history("SEL2", [(d1, 200.0)])
    mock_provider.set_history("SEL3", [(d1, 300.0)])
    mock_provider.set_latest("SEL1", 100.0)
    mock_provider.set_latest("SEL2", 200.0)
    mock_provider.set_latest("SEL3", 300.0)

    with db_session_factory() as db:
        portfolio = ensure_default_portfolio(db)
        group = _seed_group(db, portfolio.id, "selectors")
        a = _make_market_asset(portfolio.id, group.id, "SEL1", "Selector 1")
        b = _make_market_asset(portfolio.id, group.id, "SEL2", "Selector 2")
        c = _make_market_asset(portfolio.id, group.id, "SEL3", "Selector 3")
        db.add_all([a, b, c])
        db.flush()

        now = datetime.now(timezone.utc)
        db.add_all(
            [
                Transaction(
                    portfolio_id=portfolio.id,
                    asset_id=a.id,
                    type=TransactionType.BUY,
                    timestamp=now,
                    quantity=1.0,
                    price=90.0,
                    fees=0.0,
                ),
                Transaction(
                    portfolio_id=portfolio.id,
                    asset_id=b.id,
                    type=TransactionType.BUY,
                    timestamp=now,
                    quantity=1.0,
                    price=190.0,
                    fees=0.0,
                ),
                Transaction(
                    portfolio_id=portfolio.id,
                    asset_id=c.id,
                    type=TransactionType.BUY,
                    timestamp=now,
                    quantity=1.0,
                    price=290.0,
                    fees=0.0,
                ),
            ]
        )

        basket = Basket(portfolio_id=portfolio.id, name="Selector Basket")
        db.add(basket)
        db.flush()
        db.add_all(
            [
                BasketAsset(basket_id=basket.id, asset_id=a.id, weight=None),
                BasketAsset(basket_id=basket.id, asset_id=b.id, weight=None),
            ]
        )
        db.commit()

    response = authed_client.get("/")
    assert response.status_code == 200
    assert 'id="pnlChart"' in response.text
    assert 'value="Selector Basket"' in response.text
    assert 'value="SEL3"' in response.text
    assert 'value="SEL1"' not in response.text
    assert 'value="SEL2"' not in response.text
