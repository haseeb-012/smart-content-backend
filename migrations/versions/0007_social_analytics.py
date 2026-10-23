"""Social verification (bio code) + daily channel stats for own-account analytics.

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "social_accounts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("handle", sa.String(100), nullable=False),
        sa.Column("code", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("channel_id", sa.Uuid(), sa.ForeignKey("channels.id", ondelete="SET NULL")),
        sa.Column("last_checked_at", sa.DateTime(timezone=True)),
        sa.Column("verified_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "platform", "handle", name="uq_social_accounts_user_platform_handle"),
        sa.CheckConstraint("platform in ('tiktok','instagram','youtube')", name="social_platform_valid"),
        sa.CheckConstraint("status in ('pending','verified')", name="social_status_valid"),
    )
    op.create_index("ix_social_accounts_user_id", "social_accounts", ["user_id"])
    op.create_index("ix_social_accounts_channel_id", "social_accounts", ["channel_id"])
    op.create_table(
        "channel_stats",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("channel_id", sa.Uuid(), sa.ForeignKey("channels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("captured_on", sa.Date(), nullable=False),
        sa.Column("followers", sa.BigInteger()),
        sa.Column("total_views", sa.BigInteger()),
        sa.Column("video_count", sa.Integer(), nullable=False, server_default="0"),
        sa.UniqueConstraint("channel_id", "captured_on", name="uq_channel_stats_channel_day"),
    )
    op.create_index("ix_channel_stats_channel_id", "channel_stats", ["channel_id"])
    if op.get_bind().dialect.name == "postgresql":
        for t in ("social_accounts", "channel_stats"):
            op.execute(f"ALTER TABLE public.{t} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("channel_stats")
    op.drop_table("social_accounts")
