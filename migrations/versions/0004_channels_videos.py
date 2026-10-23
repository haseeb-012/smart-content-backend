"""Channels (global), workspace watchlists, and ingested videos with outlier scores.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.create_table(
        "channels",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("handle", sa.String(100), nullable=False),
        sa.Column("external_id", sa.String(100)),
        sa.Column("display_name", sa.String(200), nullable=False, server_default=""),
        sa.Column("avatar_url", sa.String(1000)),
        sa.Column("followers", sa.BigInteger()),
        sa.Column("total_views", sa.BigInteger()),
        sa.Column("video_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("sync_status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("sync_error", sa.String(300)),
        sa.Column("last_synced_at", sa.DateTime(timezone=True)),
        _ts(),
        sa.UniqueConstraint("platform", "handle", name="uq_channels_platform_handle"),
        sa.CheckConstraint("platform in ('tiktok','instagram','youtube')", name="platform_valid"),
    )
    op.create_table(
        "workspace_channels",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("channel_id", sa.Uuid(), sa.ForeignKey("channels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("added_by", sa.Uuid(), sa.ForeignKey("profiles.id", ondelete="SET NULL")),
        _ts(),
        sa.UniqueConstraint("workspace_id", "channel_id", name="uq_workspace_channels_ws_channel"),
    )
    op.create_index("ix_workspace_channels_workspace_id", "workspace_channels", ["workspace_id"])
    op.create_index("ix_workspace_channels_channel_id", "workspace_channels", ["channel_id"])
    op.create_table(
        "videos",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("channel_id", sa.Uuid(), sa.ForeignKey("channels.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("external_id", sa.String(100), nullable=False),
        sa.Column("url", sa.String(1000), nullable=False),
        sa.Column("caption", sa.Text(), nullable=False, server_default=""),
        sa.Column("thumbnail_url", sa.String(1000)),
        sa.Column("posted_at", sa.DateTime(timezone=True)),
        sa.Column("duration_sec", sa.Integer()),
        sa.Column("views", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("likes", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("comments", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("shares", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("engagement_rate", sa.Float(), nullable=False, server_default="0"),
        sa.Column("outlier_score", sa.Float()),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        _ts(),
        sa.UniqueConstraint("platform", "external_id", name="uq_videos_platform_external_id"),
    )
    op.create_index("ix_videos_channel_id", "videos", ["channel_id"])
    op.create_index("ix_videos_posted_at", "videos", ["posted_at"])
    op.create_index("ix_videos_outlier_score", "videos", ["outlier_score"])
    if op.get_bind().dialect.name == "postgresql":
        for t in ("channels", "workspace_channels", "videos"):
            op.execute(f"ALTER TABLE public.{t} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("videos")
    op.drop_table("workspace_channels")
    op.drop_table("channels")
