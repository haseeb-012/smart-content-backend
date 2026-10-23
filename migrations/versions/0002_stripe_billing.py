"""Stripe test billing: subscription fields on profiles + processed webhook events.

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-23
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("profiles") as b:
        b.add_column(sa.Column("stripe_customer_id", sa.String(64)))
        b.add_column(sa.Column("stripe_subscription_id", sa.String(64)))
        b.add_column(sa.Column("subscription_status", sa.String(24)))
        b.add_column(sa.Column("billing_cycle", sa.String(10)))
        b.create_unique_constraint("uq_profiles_stripe_customer_id", ["stripe_customer_id"])

    op.create_table(
        "stripe_events",
        sa.Column("id", sa.String(80), primary_key=True),
        sa.Column("type", sa.String(80), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute("ALTER TABLE public.stripe_events ENABLE ROW LEVEL SECURITY")


def downgrade() -> None:
    op.drop_table("stripe_events")
    with op.batch_alter_table("profiles") as b:
        b.drop_constraint("uq_profiles_stripe_customer_id", type_="unique")
        b.drop_column("billing_cycle")
        b.drop_column("subscription_status")
        b.drop_column("stripe_subscription_id")
        b.drop_column("stripe_customer_id")
