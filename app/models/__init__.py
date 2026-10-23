"""SQLAlchemy models. Product tables live in `public` and reference Supabase `auth.users(id)`
via `profiles.id` (the FK to auth.users is added in the Postgres migration only)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, Timestamped, utcnow


class Profile(Timestamped, Base):
    """One row per Supabase auth user (created by DB trigger on signup, or lazily on first request)."""

    __tablename__ = "profiles"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)  # = auth.users.id
    email: Mapped[str | None] = mapped_column(String(320))
    full_name: Mapped[str | None] = mapped_column(String(120))
    # Plan attribute read by all gating code. Changed only by admins in v1 or by billing webhooks.
    plan: Mapped[str] = mapped_column(String(20), default="pro", nullable=False)
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mcp_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    # Credits reset on this day-of-month (account anniversary, or Stripe period start later).
    credit_anchor: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    # Billing (Stripe test mode). Plan changes from webhooks write `plan` above.
    stripe_customer_id: Mapped[str | None] = mapped_column(String(64), unique=True)
    stripe_subscription_id: Mapped[str | None] = mapped_column(String(64))
    subscription_status: Mapped[str | None] = mapped_column(String(24))
    billing_cycle: Mapped[str | None] = mapped_column(String(10))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )


class Workspace(Timestamped, Base):
    __tablename__ = "workspaces"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    owner_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"), index=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    memberships: Mapped[list["Membership"]] = relationship(
        back_populates="workspace", cascade="all, delete-orphan"
    )


class Membership(Timestamped, Base):
    __tablename__ = "memberships"
    __table_args__ = (UniqueConstraint("workspace_id", "user_id", name="uq_memberships_workspace_user"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"), index=True)
    role: Mapped[str] = mapped_column(String(16), default="owner", nullable=False)  # owner | editor | guest

    workspace: Mapped[Workspace] = relationship(back_populates="memberships")


class CreditLedger(Timestamped, Base):
    """Append-only credit events. Usage rows are negative; admin adjustments can be +/-.

    `(user_id, idempotency_key)` is unique, so retries of the same action can never charge twice.
    Balance for a period = plan allowance + sum(amount) where period_key = current period.
    """

    __tablename__ = "credit_ledger"
    __table_args__ = (UniqueConstraint("user_id", "idempotency_key", name="uq_credit_ledger_user_key"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("profiles.id", ondelete="CASCADE"), index=True)
    workspace_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("workspaces.id", ondelete="SET NULL"))
    kind: Mapped[str] = mapped_column(String(16), nullable=False)  # usage | adjustment
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    period_key: Mapped[str] = mapped_column(String(10), index=True, nullable=False)  # YYYY-MM-DD period start


class AuditLog(Timestamped, Base):
    """Audit trail for credit, plan, permission and admin changes (PRD §13.3)."""

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("profiles.id", ondelete="SET NULL"), index=True
    )
    target_user_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class StripeEvent(Timestamped, Base):
    """Processed webhook event IDs — Stripe retries deliveries, so each event is applied once."""

    __tablename__ = "stripe_events"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)  # evt_…
    type: Mapped[str] = mapped_column(String(80), nullable=False)


class Channel(Timestamped, Base):
    """A creator account on one platform. Global (shared across workspaces) so each channel is
    fetched once no matter how many workspaces track it."""

    __tablename__ = "channels"
    __table_args__ = (UniqueConstraint("platform", "handle", name="uq_channels_platform_handle"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)  # tiktok | instagram | youtube
    handle: Mapped[str] = mapped_column(String(100), nullable=False)  # lowercase, no @
    external_id: Mapped[str | None] = mapped_column(String(100))
    display_name: Mapped[str] = mapped_column(String(200), default="", nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(String(1000))
    followers: Mapped[int | None] = mapped_column(BigInteger)
    total_views: Mapped[int | None] = mapped_column(BigInteger)
    video_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    sync_status: Mapped[str] = mapped_column(
        String(16), default="pending", nullable=False
    )  # pending|ok|error
    sync_error: Mapped[str | None] = mapped_column(String(300))
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WorkspaceChannel(Timestamped, Base):
    """A channel on a workspace's watchlist (FR-CH-02). Plan limits the count (FR-CH-04)."""

    __tablename__ = "workspace_channels"
    __table_args__ = (
        UniqueConstraint("workspace_id", "channel_id", name="uq_workspace_channels_ws_channel"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    channel_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    added_by: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("profiles.id", ondelete="SET NULL"))


class Video(Timestamped, Base):
    """A short-form video from a tracked channel. `outlier_score` = views ÷ median views of the
    channel's previous 30 videos (needs ≥ 10 earlier videos, else NULL)."""

    __tablename__ = "videos"
    __table_args__ = (UniqueConstraint("platform", "external_id", name="uq_videos_platform_external_id"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    channel_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("channels.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)
    external_id: Mapped[str] = mapped_column(String(100), nullable=False)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    caption: Mapped[str] = mapped_column(Text, default="", nullable=False)
    thumbnail_url: Mapped[str | None] = mapped_column(String(1000))
    posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    duration_sec: Mapped[int | None] = mapped_column(Integer)
    views: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    likes: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    comments: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    shares: Mapped[int] = mapped_column(BigInteger, default=0, nullable=False)
    engagement_rate: Mapped[float] = mapped_column(Float, default=0.0, nullable=False)  # percent
    outlier_score: Mapped[float | None] = mapped_column(Float, index=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class Collection(Timestamped, Base):
    """Team-curated group of outlier videos sharing one pattern (PRD §9.10). Free for everyone.
    Users can't create their own in v1 (FR-COL-07) — only admins write."""

    __tablename__ = "collections"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    slug: Mapped[str] = mapped_column(String(80), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    # formats | editing_styles | visual_hooks | signature_series
    category: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    sub: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    breakdown: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    cover_hue: Mapped[str] = mapped_column(String(160), default="", nullable=False)
    is_published: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False
    )

    videos: Mapped[list["CollectionVideo"]] = relationship(
        back_populates="collection",
        cascade="all, delete-orphan",
        order_by="CollectionVideo.position",
    )


class CollectionVideo(Timestamped, Base):
    """One example inside a collection. Stores public metadata + our own breakdown notes —
    never the creator's video file. (Links to the ingested `videos` table come with Phase 5 ingestion.)"""

    __tablename__ = "collection_videos"
    __table_args__ = (UniqueConstraint("collection_id", "url", name="uq_collection_videos_collection_url"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    collection_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"), index=True
    )
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    platform: Mapped[str] = mapped_column(String(16), nullable=False)  # tiktok | instagram | youtube
    url: Mapped[str] = mapped_column(String(500), nullable=False)
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    creator_handle: Mapped[str] = mapped_column(String(100), default="", nullable=False)
    views: Mapped[int | None] = mapped_column(BigInteger)
    outlier_score: Mapped[float | None] = mapped_column(Float)
    transcript: Mapped[str] = mapped_column(Text, default="", nullable=False)
    notes: Mapped[str] = mapped_column(Text, default="", nullable=False)

    collection: Mapped[Collection] = relationship(back_populates="videos")


class WaitlistEntry(Timestamped, Base):
    __tablename__ = "waitlist"
    __table_args__ = (UniqueConstraint("email", "plan", name="uq_waitlist_email_plan"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    plan: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="landing", nullable=False)
