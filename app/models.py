from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SqlEnum,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def utc_now() -> datetime:
    """Return an aware UTC timestamp."""
    return datetime.now(timezone.utc)


class AssetType(str, Enum):
    MARKET = "market"
    MANUAL = "manual"


class TransactionType(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    MANUAL_VALUE_UPDATE = "MANUAL_VALUE_UPDATE"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )


class Portfolio(Base):
    __tablename__ = "portfolios"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    groups: Mapped[list[Group]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan"
    )
    assets: Mapped[list[Asset]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan"
    )
    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan"
    )
    baskets: Mapped[list[Basket]] = relationship(
        back_populates="portfolio", cascade="all, delete-orphan"
    )


class Group(Base):
    __tablename__ = "groups"
    __table_args__ = (
        UniqueConstraint("portfolio_id", "name", name="uq_group_portfolio_name"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)

    portfolio: Mapped[Portfolio] = relationship(back_populates="groups")
    assets: Mapped[list[Asset]] = relationship(back_populates="group")


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    asset_type: Mapped[AssetType] = mapped_column(SqlEnum(AssetType), nullable=False)
    group_id: Mapped[int] = mapped_column(
        ForeignKey("groups.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )
    is_archived: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    portfolio: Mapped[Portfolio] = relationship(back_populates="assets")
    group: Mapped[Group] = relationship(back_populates="assets")
    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="asset", cascade="all, delete-orphan"
    )
    basket_links: Mapped[list[BasketAsset]] = relationship(
        back_populates="asset", cascade="all, delete-orphan"
    )


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    type: Mapped[TransactionType] = mapped_column(
        SqlEnum(TransactionType), nullable=False
    )
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    quantity: Mapped[float | None] = mapped_column(Float, nullable=True)
    price: Mapped[float | None] = mapped_column(Float, nullable=True)
    fees: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)
    manual_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    invested_override: Mapped[float | None] = mapped_column(Float, nullable=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    portfolio: Mapped[Portfolio] = relationship(back_populates="transactions")
    asset: Mapped[Asset] = relationship(back_populates="transactions")


class QuoteCache(Base):
    __tablename__ = "quote_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(
        String(32), unique=True, nullable=False, index=True
    )
    price: Mapped[float] = mapped_column(Float, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now
    )


class Basket(Base):
    __tablename__ = "baskets"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(
        ForeignKey("portfolios.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False
    )

    portfolio: Mapped[Portfolio] = relationship(back_populates="baskets")
    assets: Mapped[list[BasketAsset]] = relationship(
        back_populates="basket", cascade="all, delete-orphan"
    )


class BasketAsset(Base):
    __tablename__ = "basket_assets"
    __table_args__ = (
        UniqueConstraint("basket_id", "asset_id", name="uq_basket_asset"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    basket_id: Mapped[int] = mapped_column(
        ForeignKey("baskets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    asset_id: Mapped[int] = mapped_column(
        ForeignKey("assets.id", ondelete="CASCADE"), nullable=False, index=True
    )
    weight: Mapped[float | None] = mapped_column(Float, nullable=True)

    basket: Mapped[Basket] = relationship(back_populates="assets")
    asset: Mapped[Asset] = relationship(back_populates="basket_links")
