# lineage: REQ-GF-001 / implement.foundation, REQ-BF-002 / implement.migration_0002,
#          REQ-AMBIGUOUS / implement.auth_migration
"""Persistence model."""

from datetime import date, datetime

from sqlalchemy import JSON, Boolean, Date, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from service.db import Base, UTCDateTime, utcnow


class Link(Base):
    __tablename__ = "links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    target_url: Mapped[str] = mapped_column(Text)
    is_custom: Mapped[bool] = mapped_column(Boolean, default=False)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, onupdate=utcnow)
    deleted_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(String(128), unique=True, nullable=True)
    request_fingerprint: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)
    owner_key_id: Mapped[int | None] = mapped_column(
        ForeignKey("api_keys.id", ondelete="SET NULL"), nullable=True, index=True
    )


class ApiKey(Base):
    """An API key. Only a SHA-256 hash of the full key is stored; the prefix is for lookup."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(16), unique=True, index=True)
    key_hash: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(UTCDateTime, nullable=True)


class Click(Base):
    """One redirect. Stores no raw IP or user agent: only a daily-salted visitor hash."""

    __tablename__ = "clicks"
    __table_args__ = (Index("ix_clicks_link_day", "link_id", "day"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    link_id: Mapped[int] = mapped_column(ForeignKey("links.id", ondelete="CASCADE"))
    occurred_at: Mapped[datetime] = mapped_column(UTCDateTime, default=utcnow, index=True)
    day: Mapped[date] = mapped_column(Date)
    visitor_hash: Mapped[str] = mapped_column(String(64))
    referrer_host: Mapped[str | None] = mapped_column(String(255), nullable=True)
    ua_family: Mapped[str] = mapped_column(String(32))
