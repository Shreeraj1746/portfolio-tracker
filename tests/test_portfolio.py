from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.models import TransactionType
from app.services.portfolio import (
    InvalidTransaction,
    compute_allocation_percentages,
    compute_manual_position,
    compute_market_position,
    sort_transactions,
)


def make_tx(
    tx_id: int,
    tx_type: TransactionType,
    quantity: float = 0.0,
    price: float = 0.0,
    fees: float = 0.0,
    manual_value: float | None = None,
    invested_override: float | None = None,
):
    return SimpleNamespace(
        id=tx_id,
        type=tx_type,
        timestamp=datetime(2025, 1, tx_id, tzinfo=timezone.utc),
        quantity=quantity,
        price=price,
        fees=fees,
        manual_value=manual_value,
        invested_override=invested_override,
    )


def test_weighted_average_cost_basis_with_fees() -> None:
    txs = [
        make_tx(1, TransactionType.BUY, quantity=10, price=100, fees=5),
        make_tx(2, TransactionType.BUY, quantity=5, price=120, fees=2),
        make_tx(3, TransactionType.SELL, quantity=3, price=130),
    ]

    state = compute_market_position(txs)
    assert state.quantity == pytest.approx(12)

    # ((10*100 + 5) + (5*120 + 2)) / 15 = 107.1333...
    assert state.avg_cost == pytest.approx(107.1333333333, rel=1e-6)


def test_transaction_edit_recompute_changes_result_deterministically() -> None:
    original = [
        make_tx(1, TransactionType.BUY, quantity=10, price=100, fees=0),
        make_tx(2, TransactionType.BUY, quantity=10, price=200, fees=0),
        make_tx(3, TransactionType.SELL, quantity=5, price=150, fees=0),
    ]
    edited = [
        make_tx(1, TransactionType.BUY, quantity=20, price=100, fees=0),
        make_tx(2, TransactionType.BUY, quantity=10, price=200, fees=0),
        make_tx(3, TransactionType.SELL, quantity=5, price=150, fees=0),
    ]

    before = compute_market_position(original)
    after = compute_market_position(edited)

    assert before.quantity == pytest.approx(15)
    assert after.quantity == pytest.approx(25)
    assert before.avg_cost == pytest.approx(150.0)
    assert after.avg_cost == pytest.approx((20 * 100 + 10 * 200) / 30)


def test_sell_more_than_owned_is_rejected() -> None:
    txs = [
        make_tx(1, TransactionType.BUY, quantity=2, price=100),
        make_tx(2, TransactionType.SELL, quantity=3, price=120),
    ]
    with pytest.raises(InvalidTransaction):
        compute_market_position(txs)


def test_allocations_sum_to_100_percent() -> None:
    allocation = compute_allocation_percentages(
        {"asset1": 150.0, "asset2": 50.0, "asset3": 300.0}
    )
    assert sum(allocation.values()) == pytest.approx(100.0)


def test_sort_transactions_handles_naive_and_aware_datetimes() -> None:
    txs = [
        SimpleNamespace(id=1, timestamp=datetime(2026, 2, 15, 13, 15, 0)),
        SimpleNamespace(
            id=2, timestamp=datetime(2026, 2, 15, 13, 14, 0, tzinfo=timezone.utc)
        ),
    ]

    ordered = sort_transactions(txs)
    assert [tx.id for tx in ordered] == [2, 1]


def test_market_position_allows_negative_buy_price_for_adjusted_basis() -> None:
    txs = [
        make_tx(1, TransactionType.BUY, quantity=0.5, price=-1000, fees=0),
        make_tx(2, TransactionType.BUY, quantity=0.5, price=500, fees=0),
    ]

    state = compute_market_position(txs)
    assert state.quantity == pytest.approx(1.0)
    assert state.avg_cost == pytest.approx(-250.0)


def test_manual_position_supports_sell_and_reduces_invested_cost_basis() -> None:
    txs = [
        make_tx(1, TransactionType.BUY, quantity=100_000, price=1.0),
        make_tx(2, TransactionType.MANUAL_VALUE_UPDATE, manual_value=100_000.0),
        make_tx(3, TransactionType.SELL, quantity=20_000, price=20_000),
        make_tx(4, TransactionType.MANUAL_VALUE_UPDATE, manual_value=80_000.0),
    ]

    state = compute_manual_position(txs)
    assert state.invested_total == pytest.approx(80_000.0)
    assert state.current_value == pytest.approx(80_000.0)
    assert state.unrealized_pnl == pytest.approx(0.0)


def test_manual_value_update_can_override_invested_basis() -> None:
    txs = [
        make_tx(1, TransactionType.BUY, quantity=1, price=166_054.0),
        make_tx(
            2,
            TransactionType.MANUAL_VALUE_UPDATE,
            manual_value=162_811.0,
            invested_override=162_811.0,
        ),
    ]

    state = compute_manual_position(txs)
    assert state.invested_total == pytest.approx(162_811.0)
    assert state.current_value == pytest.approx(162_811.0)
    assert state.unrealized_pnl == pytest.approx(0.0)
