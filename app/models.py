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
    track_visits: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )  # receipt scanner skip-list: False = never auto-log visits (e.g. kid's McDonald's)
    yelp_id: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )  # Yelp business id, set when added from Discover
    yelp_rating: Mapped[float | None] = mapped_column(
        Float, nullable=True
    )  # Yelp star rating (1-5), used as a tiebreak in "somewhere new" mode
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
    exclude_from_picks: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )  # per-trip picker opt-out: the visit stays in History + Spending but the
    # picker ignores it for weighting, 7-day rule, cuisine rotation, and cards
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    restaurant: Mapped["Restaurant"] = relationship("Restaurant", back_populates="visits")


class EmailAccount(Base):
    """A Gmail account the deal/receipt scanner polls over IMAP.

    Replaces the old single-account gmail_address/gmail_app_password settings
    keys (migrated on startup, see app.deal_scan.migrate_legacy_gmail_settings).
    """

    __tablename__ = "email_accounts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str | None] = mapped_column(String(120), nullable=True)
    address: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    app_password: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


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
    source_url: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )  # link to the origin: Reddit post, Gmail search, …
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    restaurant: Mapped["Restaurant | None"] = relationship("Restaurant", back_populates="deals")


class Setting(Base):
    """Key-value app settings (Gmail creds for the deal scanner, scan schedule, …)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(120), primary_key=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


class DiscoverCache(Base):
    """Cached nearby-search results — keyed by provider + rounded location + radius.

    Repeat views of the same search are served from here for CACHE_TTL_HOURS
    instead of hitting the provider API.
    """

    __tablename__ = "discover_cache"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str | None] = mapped_column(String(16), nullable=True)  # "yelp"|"osm" (NULL = legacy yelp rows)
    lat: Mapped[float] = mapped_column(Float, nullable=False)  # rounded to 3 decimals
    lon: Mapped[float] = mapped_column(Float, nullable=False)  # rounded to 3 decimals
    radius_km: Mapped[float] = mapped_column(Float, nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)  # JSON: list of business dicts
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
