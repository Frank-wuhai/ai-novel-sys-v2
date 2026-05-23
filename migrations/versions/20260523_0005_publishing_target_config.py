"""Add publishing target work config fields.

Revision ID: 20260523_0005
Revises: 20260522_0004
Create Date: 2026-05-23
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260523_0005"
down_revision = "20260522_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("publishing_targets", sa.Column("work_identifier", sa.String(length=255), nullable=False, server_default=""))
    op.add_column("publishing_targets", sa.Column("config_json", sa.Text(), nullable=False, server_default="{}"))
    with op.batch_alter_table("publishing_targets") as batch_op:
        batch_op.create_unique_constraint(
            "uq_publishing_targets_platform_account_work",
            ["platform", "account_label", "work_identifier"],
        )


def downgrade() -> None:
    with op.batch_alter_table("publishing_targets") as batch_op:
        batch_op.drop_constraint("uq_publishing_targets_platform_account_work", type_="unique")
    op.drop_column("publishing_targets", "config_json")
    op.drop_column("publishing_targets", "work_identifier")
