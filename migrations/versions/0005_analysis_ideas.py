"""Deep Analysis results per workspace + ideas.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.create_table(
        "video_analyses",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("video_id", sa.Uuid(), sa.ForeignKey("videos.id", ondelete="CASCADE"), nullable=False),
        sa.Column("requested_by", sa.Uuid(), sa.ForeignKey("profiles.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("error", sa.String(300)),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("provider", sa.String(32)),
        sa.Column("model", sa.String(64)),
        sa.Column("summary", sa.Text()),
        sa.Column("transcript", sa.JSON(), nullable=False),
        sa.Column("hook", sa.Text()),
        sa.Column("hook_category", sa.String(32)),
        sa.Column("format", sa.Text()),
        sa.Column("visual", sa.Text()),
        sa.Column("idea", sa.Text()),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        _ts(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "video_id", name="uq_video_analyses_ws_video"),
        sa.CheckConstraint(
            "status in ('queued','processing','complete','failed')", name="analysis_status_valid"
        ),
    )
    op.create_index("ix_video_analyses_workspace_id", "video_analyses", ["workspace_id"])
    op.create_index("ix_video_analyses_video_id", "video_analyses", ["video_id"])
    op.create_index("ix_video_analyses_requested_by", "video_analyses", ["requested_by"])
    op.create_table(
        "ideas",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("video_id", sa.Uuid(), sa.ForeignKey("videos.id", ondelete="SET NULL")),
        sa.Column("analysis_id", sa.Uuid(), sa.ForeignKey("video_analyses.id", ondelete="SET NULL")),
        sa.Column("source", sa.String(16), nullable=False, server_default="analysis"),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("positioning", sa.Text(), nullable=False, server_default=""),
        sa.Column("format", sa.Text(), nullable=False, server_default=""),
        sa.Column("contrast", sa.Text(), nullable=False, server_default=""),
        sa.Column("personalization", sa.Text(), nullable=False, server_default=""),
        sa.Column("shortlisted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("created_by", sa.Uuid(), sa.ForeignKey("profiles.id", ondelete="SET NULL")),
        _ts(),
        sa.UniqueConstraint("analysis_id", name="uq_ideas_analysis_id"),
    )
    op.create_index("ix_ideas_workspace_id", "ideas", ["workspace_id"])
    op.create_index("ix_ideas_video_id", "ideas", ["video_id"])
    if op.get_bind().dialect.name == "postgresql":
        for t in ("video_analyses", "ideas"):
            op.execute(f"ALTER TABLE public.{t} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("ideas")
    op.drop_table("video_analyses")
