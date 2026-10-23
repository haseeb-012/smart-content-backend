"""Collections (team-curated, free) + their example videos.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("slug", sa.String(80), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("sub", sa.String(80), nullable=False, server_default=""),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("breakdown", sa.JSON(), nullable=False),
        sa.Column("cover_hue", sa.String(160), nullable=False, server_default=""),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("slug", name="uq_collections_slug"),
        sa.CheckConstraint(
            "category in ('formats','editing_styles','visual_hooks','signature_series')",
            name="category_valid",
        ),
    )
    op.create_index("ix_collections_category", "collections", ["category"])
    op.create_table(
        "collection_videos",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "collection_id", sa.Uuid(), sa.ForeignKey("collections.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("platform", sa.String(16), nullable=False),
        sa.Column("url", sa.String(500), nullable=False),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("creator_handle", sa.String(100), nullable=False, server_default=""),
        sa.Column("views", sa.BigInteger()),
        sa.Column("outlier_score", sa.Float()),
        sa.Column("transcript", sa.Text(), nullable=False, server_default=""),
        sa.Column("notes", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("collection_id", "url", name="uq_collection_videos_collection_url"),
        sa.CheckConstraint("platform in ('tiktok','instagram','youtube')", name="platform_valid"),
    )
    op.create_index("ix_collection_videos_collection_id", "collection_videos", ["collection_id"])
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE public.collections ENABLE ROW LEVEL SECURITY")
        op.execute("ALTER TABLE public.collection_videos ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("collection_videos")
    op.drop_table("collections")
