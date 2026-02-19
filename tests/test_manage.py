from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select

import manage
from app.models import Asset, AssetType, Group
from app.services.portfolio import ensure_default_portfolio


def test_purge_archived_assets_respects_archive_flag_and_age_filter(
    db_session_factory,
    monkeypatch,
):
    now = datetime.now(timezone.utc)
    with db_session_factory() as db:
        portfolio = ensure_default_portfolio(db)
        group = Group(portfolio_id=portfolio.id, name="maintenance")
        db.add(group)
        db.flush()

        db.add_all(
            [
                Asset(
                    portfolio_id=portfolio.id,
                    symbol="ARCHOLD",
                    name="Archived Old Asset",
                    asset_type=AssetType.MANUAL,
                    group_id=group.id,
                    is_archived=True,
                    created_at=now - timedelta(days=45),
                ),
                Asset(
                    portfolio_id=portfolio.id,
                    symbol="ARCHNEW",
                    name="Archived New Asset",
                    asset_type=AssetType.MANUAL,
                    group_id=group.id,
                    is_archived=True,
                    created_at=now - timedelta(days=2),
                ),
                Asset(
                    portfolio_id=portfolio.id,
                    symbol="LIVE",
                    name="Live Asset",
                    asset_type=AssetType.MANUAL,
                    group_id=group.id,
                    is_archived=False,
                    created_at=now - timedelta(days=90),
                ),
            ]
        )
        db.commit()

    monkeypatch.setattr(manage, "SessionLocal", db_session_factory)
    deleted = manage.purge_archived_assets(older_than_days=30)
    assert deleted == 1

    with db_session_factory() as db:
        assert db.scalar(select(Asset).where(Asset.symbol == "ARCHOLD")) is None
        assert db.scalar(select(Asset).where(Asset.symbol == "ARCHNEW")) is not None
        assert db.scalar(select(Asset).where(Asset.symbol == "LIVE")) is not None

    deleted = manage.purge_archived_assets()
    assert deleted == 1

    with db_session_factory() as db:
        assert db.scalar(select(Asset).where(Asset.symbol == "ARCHNEW")) is None
