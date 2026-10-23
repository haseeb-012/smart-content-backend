"""Create module: hook library, saved hooks, scripts + versions, projects + items, export history.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _ts() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def _ws() -> sa.Column:
    return sa.Column(
        "workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )


def _by(name: str = "created_by") -> sa.Column:
    return sa.Column(name, sa.Uuid(), sa.ForeignKey("profiles.id", ondelete="SET NULL"))


TABLES = ("hook_library", "saved_hooks", "scripts", "script_versions", "projects", "project_items", "exports")


def upgrade() -> None:
    op.create_table(
        "hook_library",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("text", sa.String(300), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("niche", sa.String(60), nullable=False, server_default="General"),
        sa.Column("views", sa.BigInteger()),
        sa.Column("source_url", sa.String(500)),
        sa.Column("is_published", sa.Boolean(), nullable=False, server_default=sa.true()),
        _ts(),
        sa.UniqueConstraint("text", name="uq_hook_library_text"),
    )
    op.create_index("ix_hook_library_category", "hook_library", ["category"])
    op.create_table(
        "saved_hooks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _ws(),
        sa.Column("text", sa.String(300), nullable=False),
        sa.Column("category", sa.String(32)),
        sa.Column("source", sa.String(16), nullable=False, server_default="library"),
        _by(),
        _ts(),
        sa.UniqueConstraint("workspace_id", "text", name="uq_saved_hooks_ws_text"),
    )
    op.create_index("ix_saved_hooks_workspace_id", "saved_hooks", ["workspace_id"])
    op.create_table(
        "scripts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _ws(),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("topic", sa.String(500), nullable=False, server_default=""),
        sa.Column("blocks", sa.JSON(), nullable=False),
        sa.Column("expanded", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("request_key", sa.String(80)),
        sa.Column("source_video_id", sa.Uuid(), sa.ForeignKey("videos.id", ondelete="SET NULL")),
        sa.Column("source_idea_id", sa.Uuid(), sa.ForeignKey("ideas.id", ondelete="SET NULL")),
        _by(),
        _ts(),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("workspace_id", "request_key", name="uq_scripts_ws_request_key"),
    )
    op.create_index("ix_scripts_workspace_id", "scripts", ["workspace_id"])
    op.create_table(
        "script_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("script_id", sa.Uuid(), sa.ForeignKey("scripts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("blocks", sa.JSON(), nullable=False),
        sa.Column("note", sa.String(32), nullable=False, server_default="edit"),
        _ts(),
    )
    op.create_index("ix_script_versions_script_id", "script_versions", ["script_id"])
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _ws(),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        _by(),
        _ts(),
    )
    op.create_index("ix_projects_workspace_id", "projects", ["workspace_id"])
    op.create_table(
        "project_items",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _ws(),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("ref_id", sa.Uuid(), nullable=False),
        _ts(),
        sa.UniqueConstraint("workspace_id", "kind", "ref_id", name="uq_project_items_ws_kind_ref"),
        sa.CheckConstraint("kind in ('video','idea','script')", name="project_item_kind_valid"),
    )
    op.create_index("ix_project_items_workspace_id", "project_items", ["workspace_id"])
    op.create_index("ix_project_items_project_id", "project_items", ["project_id"])
    op.create_table(
        "exports",
        sa.Column("id", sa.Uuid(), primary_key=True),
        _ws(),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("format", sa.String(16), nullable=False),
        _by(),
        _ts(),
    )
    op.create_index("ix_exports_workspace_id", "exports", ["workspace_id"])
    if op.get_bind().dialect.name == "postgresql":
        for t in TABLES:
            op.execute(f"ALTER TABLE public.{t} ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    for t in reversed(TABLES):
        op.drop_table(t)
