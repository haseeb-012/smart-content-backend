"""Keep our own copy of video thumbnails (TikTok/Instagram links expire after a few days).

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("videos") as b:
        b.add_column(sa.Column("thumb_stored_at", sa.DateTime(timezone=True), nullable=True))
    op.create_table(
        "video_thumbnails",
        sa.Column("video_id", sa.Uuid(), sa.ForeignKey("videos.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("mime", sa.String(40), nullable=False),
        sa.Column("data", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    # Supabase: the Data API must not expose raw tables (same as every other table here).
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE video_thumbnails ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("video_thumbnails")
    with op.batch_alter_table("videos") as b:
        b.drop_column("thumb_stored_at")
