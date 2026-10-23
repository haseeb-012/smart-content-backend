"""Foundation: profiles, workspaces, memberships, credit ledger, audit log, waitlist.

On Postgres (Supabase) it also:
- links profiles.id → auth.users(id) and creates a profile row on signup (trigger),
- enables RLS on every product table with NO policies, so browsers using the anon/publishable
  key can't read or write tables directly — all access goes through FastAPI (service connection).

Revision ID: 0001
Revises:
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES = ["profiles", "workspaces", "memberships", "credit_ledger", "audit_log", "waitlist"]


def _ts() -> sa.Column:
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now())


def upgrade() -> None:
    op.create_table(
        "profiles",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(320)),
        sa.Column("full_name", sa.String(120)),
        sa.Column("plan", sa.String(20), nullable=False, server_default="pro"),
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("mcp_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("credit_anchor", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        _ts(),
        sa.CheckConstraint("plan in ('pro','visionary','titan')", name="plan_valid"),
    )
    op.create_table(
        "workspaces",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(80), nullable=False),
        sa.Column("owner_id", sa.Uuid(), sa.ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        _ts(),
    )
    op.create_index("ix_workspaces_owner_id", "workspaces", ["owner_id"])
    op.create_table(
        "memberships",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("role", sa.String(16), nullable=False, server_default="owner"),
        _ts(),
        sa.UniqueConstraint("workspace_id", "user_id", name="uq_memberships_workspace_user"),
        sa.CheckConstraint("role in ('owner','editor','guest')", name="role_valid"),
    )
    op.create_index("ix_memberships_workspace_id", "memberships", ["workspace_id"])
    op.create_index("ix_memberships_user_id", "memberships", ["user_id"])
    op.create_table(
        "credit_ledger",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("user_id", sa.Uuid(), sa.ForeignKey("profiles.id", ondelete="CASCADE"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), sa.ForeignKey("workspaces.id", ondelete="SET NULL")),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("amount", sa.Integer(), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(200), nullable=False),
        sa.Column("period_key", sa.String(10), nullable=False),
        _ts(),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_credit_ledger_user_key"),
        sa.CheckConstraint("kind in ('usage','adjustment')", name="kind_valid"),
    )
    op.create_index("ix_credit_ledger_user_id", "credit_ledger", ["user_id"])
    op.create_index("ix_credit_ledger_period_key", "credit_ledger", ["period_key"])
    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("actor_id", sa.Uuid(), sa.ForeignKey("profiles.id", ondelete="SET NULL")),
        sa.Column("target_user_id", sa.Uuid()),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        _ts(),
    )
    op.create_index("ix_audit_log_actor_id", "audit_log", ["actor_id"])
    op.create_index("ix_audit_log_target_user_id", "audit_log", ["target_user_id"])
    op.create_table(
        "waitlist",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("plan", sa.String(20), nullable=False),
        sa.Column("source", sa.String(32), nullable=False, server_default="landing"),
        _ts(),
        sa.UniqueConstraint("email", "plan", name="uq_waitlist_email_plan"),
    )

    if op.get_bind().dialect.name != "postgresql":
        return

    for t in TABLES:
        op.execute(f"ALTER TABLE public.{t} ENABLE ROW LEVEL SECURITY")

    # Supabase only: link to auth.users and auto-create profiles on signup.
    op.execute(
        """
        DO $$
        BEGIN
          IF EXISTS (SELECT 1 FROM information_schema.tables
                     WHERE table_schema = 'auth' AND table_name = 'users') THEN
            ALTER TABLE public.profiles
              ADD CONSTRAINT fk_profiles_auth_users
              FOREIGN KEY (id) REFERENCES auth.users(id) ON DELETE CASCADE;

            CREATE OR REPLACE FUNCTION public.handle_new_user()
            RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $fn$
            BEGIN
              INSERT INTO public.profiles (id, email, full_name)
              VALUES (NEW.id, NEW.email, NEW.raw_user_meta_data ->> 'full_name')
              ON CONFLICT (id) DO NOTHING;
              RETURN NEW;
            END;
            $fn$;

            DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
            CREATE TRIGGER on_auth_user_created
              AFTER INSERT ON auth.users
              FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();
          END IF;
        END $$;
        """
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users")
        op.execute("DROP FUNCTION IF EXISTS public.handle_new_user()")
    for t in reversed(TABLES):
        op.drop_table(t)
