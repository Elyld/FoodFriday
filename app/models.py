"""SQLAlchemy models for FoodFriday."""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class Restaurant(Base):
    """A place to eat."""

    __tablename__ = "restaurants"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    cuisine: Mapped[str | None] = mapped_column(String(120), nullable=True)
    price_tier: Mapped[int | None] = mapped_column(Integer, nullable=True)  # 1-3
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    address: Mapped[str | None] = mapped_column(String(255), nullable=True)
    favorite: Mapped[bool | None] = mapped_column(Boolean, nullable=True, default=False)
    include_in_picks: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    visits: Mapped[list["Visit"]] = relationship(
        "Visit", back_populates="restaurant", cascade="all, delete-orphan", lazy="selectin"
    )
    deals: Mapped[list["Deal"]] = relationship(
        "Deal", back_populates="restaurant", cascade="all, delete-orphan", lazy="selectin"
    )


class Visit(Base):
    """One logged visit to a restaurant."""

    __tablename__ = "visits"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    restaurant_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=False
    )
    visited_at: Mapped[date] = mapped_column(Date, nullable=False)
    total: Mapped[float | None] = mapped_column(Float, nullable=True)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)  # manual|import
    external_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # e.g. "gmail:<message-id>" — dedup key for imports
    items: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # newline-separated item names from receipts (for deal item-matching)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    restaurant: Mapped["Restaurant"] = relationship("Restaurant", back_populates="visits")


class Deal(Base):
    """A promotion/discount at a restaurant. Active deals boost pick weight."""

    __tablename__ = "deals"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    restaurant_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("restaurants.id", ondelete="CASCADE"), nullable=True
    )  # null = chain-wide / unlinked
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    valid_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    valid_until: Mapped[date | None] = mapped_column(Date, nullable=True)
    item_keywords: Mapped[str | None] = mapped_column(
        Text, nullable=True
    )  # comma-separated keywords for the item-level pick bonus
    source: Mapped[str | None] = mapped_column(String(32), nullable=True, default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    restaurant: Mapped["Restaurant | None"] = relationship("Restaurant", back_populates="deals")


class Setting(Base):
    """Key-value app settings (Gmail creds for the deal scanner, scan schedule, …)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
