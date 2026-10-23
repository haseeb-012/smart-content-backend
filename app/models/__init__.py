"""SQLAlchemy models. Product tables live in `public` and reference Supabase `auth.users(id)`
via `profiles.id` (the FK to auth.users is added in the Postgres migration only)."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, UniqueConstraint, Uuid
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


class WaitlistEntry(Timestamped, Base):
    __tablename__ = "waitlist"
    __table_args__ = (UniqueConstraint("email", "plan", name="uq_waitlist_email_plan"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    plan: Mapped[str] = mapped_column(String(20), nullable=False)
    source: Mapped[str] = mapped_column(String(32), default="landing", nullable=False)
